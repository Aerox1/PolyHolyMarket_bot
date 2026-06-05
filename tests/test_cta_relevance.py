"""Regression lock for news→bet CTA matching precision.

Anchors the real-world false positive — "Mark Cuban Sells Most of His Bitcoin"
got stapled to the "Democratic Presidential Nominee 2028" event purely via that
event's "Mark Cuban" candidate sub-market — and asserts it stays rejected at BOTH
entry points (render-time resolve_cta and crawl-time trending_matches), while
genuinely topical headlines still match.

If a future loosening of the matcher regresses precision, the MUST-NOT-MATCH block
fails loudly. (Known accepted limitation: a name-ONLY political headline such as
"Newsom leads the primary" is also dropped — the safe direction, "a missing bet
beats a wrong one"; the planned LLM relevance backstop will recover those.)
"""

import pytest

from bot.news import cta as news_cta
from bot.news.cta import MIN_TOKEN_OVERLAP


def _mk(cond, question, yes, no, *, group=None):
    m = {"conditionId": cond, "question": question, "outcomes": '["Yes","No"]',
         "clobTokenIds": f'["{cond}-y","{cond}-n"]', "outcomePrices": f'["{yes}","{no}"]',
         "closed": False, "active": True, "volume24hr": "100"}
    if group is not None:
        m["groupItemTitle"] = group
    return m


def _ev(title, mks):
    return {"title": title, "markets": mks}


# The exact production false positive: a crypto story vs a generic-titled multi-
# candidate election event that merely lists the person as a candidate.
CUBAN_HEADLINE = "Mark Cuban Sells Most of His Bitcoin, Says It 'Lost the Plot'"
NOMINEE_EVENT = _ev("Democratic Presidential Nominee 2028", [
    _mk("0xcuban", "Will Mark Cuban win the 2028 Democratic nomination?", "0.04", "0.96", group="Mark Cuban"),
    _mk("0xnewsom", "Will Gavin Newsom win the 2028 Democratic nomination?", "0.22", "0.78", group="Gavin Newsom"),
    _mk("0xaoc", "Will AOC win the 2028 Democratic nomination?", "0.10", "0.90", group="Alexandria Ocasio-Cortez"),
])

# Topical headline+event pairs that MUST keep matching (the event TITLE or a
# non-name shared token carries real relevance).
TOPICAL_MATCHES = [
    ("Democrats eye the Iowa governor race in 2026",
     _ev("Iowa Governor Election Winner", [
         _mk("0xd", "Will the Democrats win the Iowa governor race?", "0.6", "0.4", group="Democrats"),
         _mk("0xr", "Will the Republicans win the Iowa governor race?", "0.4", "0.6", group="Republicans")])),
    ("Bitcoin surges toward the $100k milestone",
     _ev("Bitcoin price", [_mk("0xbtc", "Will Bitcoin close above $100k in 2026?", "0.3", "0.7")])),
    ("US and Iran intensify attacks, peace deal in doubt",
     _ev("US–Iran", [_mk("0xiran", "Will the US and Iran reach a peace deal by June?", "0.12", "0.88")])),
]


# ── precision lock on the scorer (pure, no mocking) ───────────────────────────

def test_event_relevance_rejects_name_only_entity_collision():
    score = news_cta._event_relevance(CUBAN_HEADLINE, NOMINEE_EVENT)
    assert score < MIN_TOKEN_OVERLAP, (
        f"Cuban/Bitcoin must NOT match the nominee event (got score {score}); "
        "the only overlap is the candidate name.")


@pytest.mark.parametrize("headline,event", TOPICAL_MATCHES)
def test_event_relevance_keeps_topical_matches(headline, event):
    assert news_cta._event_relevance(headline, event) >= MIN_TOKEN_OVERLAP


# ── precision lock end-to-end at BOTH entry points ────────────────────────────

async def test_resolve_cta_rejects_cuban_bitcoin(monkeypatch):
    # the nominee event reaches the candidate pool via search OR trending — either
    # way the entity-collision guard must drop it → no CTA.
    monkeypatch.setattr(news_cta.markets, "search_events", lambda q, n=20: [])
    monkeypatch.setattr(news_cta.markets, "trending_events", lambda n=40: [NOMINEE_EVENT])
    assert await news_cta.resolve_cta(title=CUBAN_HEADLINE) is None


async def test_resolve_cta_still_matches_topical(monkeypatch):
    headline, event = TOPICAL_MATCHES[1]  # Bitcoin
    monkeypatch.setattr(news_cta.markets, "search_events", lambda q, n=20: [event])
    monkeypatch.setattr(news_cta.markets, "trending_events", lambda n=40: [])
    cta = await news_cta.resolve_cta(title=headline)
    assert cta and cta["market_id"] == "0xbtc"


async def test_trending_matches_excludes_name_only(monkeypatch):
    # crawl-time auto-approval shares the same gate, so it must also reject the
    # Cuban item while still flagging a genuinely on-topic one.
    monkeypatch.setattr(news_cta.markets, "trending_events", lambda n=40: [NOMINEE_EVENT])
    matched = await news_cta.trending_matches([
        (1, CUBAN_HEADLINE),                                  # entity collision → excluded
        (2, "Gavin Newsom and AOC clash over the 2028 Democratic nomination")])  # on-topic
    assert 1 not in matched
    assert 2 in matched


# ── LLM relevance backstop (lexical = recall, LLM = decision) ──────────────────

async def test_llm_pick_event_parses_index_null_and_unavailable(monkeypatch):
    import core.gemini as gemini_mod  # provider defaults to gemini in the test env
    cands = [_ev("A", [_mk("0xa", "qa?", "0.5", "0.5")]), _ev("B", [_mk("0xb", "qb?", "0.5", "0.5")])]

    def _gen(ret):
        async def _f(session, *, prompt, kind="news_text", category_id=None, response_json=False):
            return ret
        return _f

    monkeypatch.setattr(gemini_mod, "generate_text", _gen('{"index": 1}'))
    assert await news_cta._llm_pick_event(object(), "t", "b", cands) == 1
    monkeypatch.setattr(gemini_mod, "generate_text", _gen('{"index": null}'))
    assert await news_cta._llm_pick_event(object(), "t", "b", cands) is None
    monkeypatch.setattr(gemini_mod, "generate_text", _gen(None))         # budget / no provider
    assert await news_cta._llm_pick_event(object(), "t", "b", cands) is news_cta._LLM_UNAVAILABLE
    monkeypatch.setattr(gemini_mod, "generate_text", _gen("not json"))   # unparseable
    assert await news_cta._llm_pick_event(object(), "t", "b", cands) is news_cta._LLM_UNAVAILABLE
    monkeypatch.setattr(gemini_mod, "generate_text", _gen('{"index": 9}'))  # out of range
    assert await news_cta._llm_pick_event(object(), "t", "b", cands) is news_cta._LLM_UNAVAILABLE


async def test_resolve_cta_llm_rejects_topical_lookalike(monkeypatch):
    # a candidate that PASSES the lexical gate but the judge deems off-topic → no CTA
    # (the same-category miss class the deterministic guard can't catch).
    ev = _ev("Bitcoin price", [_mk("0xbtc", "Will Bitcoin close above $100k in 2026?", "0.3", "0.7")])
    monkeypatch.setattr(news_cta.markets, "search_events", lambda q, n=20: [ev])
    monkeypatch.setattr(news_cta.markets, "trending_events", lambda n=40: [])

    async def _reject(session, title, body, candidates):
        return None
    monkeypatch.setattr(news_cta, "_llm_pick_event", _reject)
    assert await news_cta.resolve_cta(title="Bitcoin price recap", body="x", session=object()) is None


async def test_resolve_cta_degrades_to_lexical_when_judge_unavailable(monkeypatch):
    ev = _ev("Bitcoin price", [_mk("0xbtc", "Will Bitcoin close above $100k in 2026?", "0.3", "0.7")])
    monkeypatch.setattr(news_cta.markets, "search_events", lambda q, n=20: [ev])
    monkeypatch.setattr(news_cta.markets, "trending_events", lambda n=40: [])

    async def _unavail(session, title, body, candidates):
        return news_cta._LLM_UNAVAILABLE
    monkeypatch.setattr(news_cta, "_llm_pick_event", _unavail)
    cta = await news_cta.resolve_cta(title="Bitcoin surges toward the $100k milestone", body="x", session=object())
    assert cta and cta["market_id"] == "0xbtc"   # kept the lexical best


async def test_resolve_cta_llm_picks_specific_candidate(monkeypatch):
    ev1 = _ev("Bitcoin price", [_mk("0xbtc", "Will Bitcoin close above $100k in 2026?", "0.3", "0.7")])
    ev2 = _ev("Bitcoin dominance", [_mk("0xdom", "Will Bitcoin dominance exceed 60% in 2026?", "0.3", "0.7")])
    monkeypatch.setattr(news_cta.markets, "search_events", lambda q, n=20: [ev1, ev2])
    monkeypatch.setattr(news_cta.markets, "trending_events", lambda n=40: [])

    async def _pick_second(session, title, body, candidates):
        return 1
    monkeypatch.setattr(news_cta, "_llm_pick_event", _pick_second)
    cta = await news_cta.resolve_cta(title="Bitcoin dominance and price both climb", body="x", session=object())
    assert cta and cta["market_id"] == "0xdom"
