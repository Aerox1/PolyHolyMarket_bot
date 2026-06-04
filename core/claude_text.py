"""News text generation via the **Claude Agent SDK** (NOT the Anthropic API).

Drives the local Claude CLI (subscription auth — no API key, no ``anthropic``
package), so it reaches Anthropic and keeps working even when the VPN blocks
Gemini/Google (see memory: vpn-blocks-egress). Used for news translate+summarize;
image generation stays on Gemini (Claude has no image model).

Contract mirrors ``gemini.generate_text``: budget-gated against the SAME weekly
ledger, charged to ``gemini_usage`` for observability, returns the model text or
None (no CLI / budget reached / failure). Best-effort — never raises into the
render job.
"""

from __future__ import annotations

import logging
import os
import shutil

from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from db.repositories import appconfig, gemini_usage

logger = logging.getLogger(__name__)

# Env vars Claude Code sets when WE run inside it. If the bot was launched from a
# Claude Code session they're inherited, and the CLI the SDK spawns would detect a
# nested/managed session and hang — strip them so it runs as a fresh invocation.
# (A normally-launched detached bot never has these, so this is a harmless no-op.)
_NESTED_ENV = (
    "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "AI_AGENT", "CLAUDE_AGENT_SDK_VERSION",
    "CLAUDE_CODE_ENABLE_TASKS", "CLAUDE_CODE_ENABLE_SDK_FILE_CHECKPOINTING",
)

_SYSTEM = "You are a financial-news editor. Output ONLY valid JSON, no prose, no code fences."


def cli_path() -> str | None:
    """Resolve the Claude CLI: explicit setting → CLAUDE_CODE_EXECPATH → PATH."""
    return (settings.claude_cli_path or os.environ.get("CLAUDE_CODE_EXECPATH")
            or shutil.which("claude"))


def available() -> bool:
    """True if the SDK is importable and a CLI is resolvable."""
    try:
        import claude_agent_sdk  # noqa: F401
    except ImportError:
        return False
    return cli_path() is not None


def _strip_text_fence(s: str) -> str:
    return s.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()


async def _query(prompt: str) -> tuple[str | None, float]:
    """Run ONE headless Claude query (no tools, no project settings). Returns
    (text, cost_usd). Best-effort → (None, 0.0) on any failure."""
    try:
        from claude_agent_sdk import (
            AssistantMessage, ClaudeAgentOptions, ResultMessage, TextBlock, query,
        )
    except ImportError:
        logger.warning("claude-agent-sdk not installed — install it or set NEWS_TEXT_PROVIDER=gemini")
        return None, 0.0

    for k in _NESTED_ENV:  # ensure the spawned CLI runs as a fresh session
        os.environ.pop(k, None)

    opts = ClaudeAgentOptions(
        cli_path=cli_path(),
        allowed_tools=[],          # pure text — no tools
        max_turns=1,
        setting_sources=[],        # don't load project CLAUDE.md / MCP servers
        system_prompt=_SYSTEM,
        permission_mode="bypassPermissions",
        model=(settings.claude_text_model or None),
    )
    chunks: list[str] = []
    cost = 0.0
    try:
        async for msg in query(prompt=prompt, options=opts):
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        chunks.append(block.text)
            elif isinstance(msg, ResultMessage):
                cost = float(getattr(msg, "total_cost_usd", 0) or 0)
                if getattr(msg, "is_error", False):
                    logger.info("Claude text returned is_error=True")
    except Exception as exc:  # noqa: BLE001 — never fail the render on a CLI hiccup
        logger.warning("Claude text failed: %s", type(exc).__name__)
        return None, 0.0
    return ("".join(chunks).strip() or None), cost


async def generate_json(
    session: AsyncSession, *, prompt: str, kind: str = "news_text", category_id: int | None = None,
) -> str | None:
    """Budget-gated Claude text generation. Returns the model text (caller parses
    the JSON) or None (no CLI / budget reached / failure). Charges the shared
    weekly ledger with the SDK-reported notional cost."""
    if cli_path() is None:
        logger.info("no Claude CLI resolvable — skipping (set CLAUDE_CLI_PATH or install `claude`)")
        return None
    budget = await appconfig.get_float(session, appconfig.GEMINI_WEEKLY_BUDGET, settings.gemini_weekly_budget_usd)
    spent = await gemini_usage.weekly_spend(session)
    if spent >= budget:
        logger.info("weekly text budget reached (%.2f/%.2f) — skipping %s", spent, budget, kind)
        return None

    raw, cost = await _query(prompt)
    model = settings.claude_text_model or "claude-cli"
    await gemini_usage.record(session, category_id=category_id, cost_usd=(cost or 0.0),
                              model=model, ok=bool(raw), kind=kind)
    if not raw:
        return None
    return _strip_text_fence(raw)
