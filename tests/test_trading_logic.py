"""Pure-logic tests for trading: result parsing, position sizing, intents.
These guard the bits most likely to mishandle real orders."""

from bot.handlers import confirm, trading


def test_make_intent_carries_no_timestamp_in_detail():
    intent = confirm.make_intent("limit", side="buy", token_id="0xabc", price=0.5, size=10)
    assert intent["kind"] == "limit" and intent["side"] == "buy"
    detail = confirm._safe_detail(intent)
    assert "ts" not in detail
    assert detail["token_id"] == "0xabc"


def test_result_ok_variants():
    assert confirm._result_ok({"success": True, "orderID": "x"}) is True
    assert confirm._result_ok({"status": "live"}) is True
    assert confirm._result_ok({"success": False}) is False
    assert confirm._result_ok({"error": "boom"}) is False
    assert confirm._result_ok({"errorMsg": "bad"}) is False
    assert confirm._result_ok("ok-string") is True  # non-dict treated as ok


def test_result_order_id_variants():
    assert confirm._result_order_id({"orderID": "A"}) == "A"
    assert confirm._result_order_id({"orderId": "B"}) == "B"
    assert confirm._result_order_id({"id": "C"}) == "C"
    assert confirm._result_order_id({"nope": 1}) is None
    assert confirm._result_order_id("x") is None


def test_position_row_finds_token_across_shapes():
    rows = [{"asset": "0xTOK", "size": "12.5"}, {"asset": "0xOTHER", "size": "1"}]

    def size(positions, tok):
        return trading._to_float((trading._position_row(positions, tok) or {}).get("size"))

    # list form
    assert size(rows, "0xTOK") == 12.5
    # wrapped under data
    assert size({"data": rows}, "0xTOK") == 12.5
    # wrapped under positions, alt key tokenId
    assert size({"positions": [{"tokenId": "0xZ", "size": 3}]}, "0xZ") == 3.0
    # not found / malformed -> no row -> 0.0
    assert size(rows, "0xMISSING") == 0.0
    assert size("garbage", "0xTOK") == 0.0
    # the row helper returns the full dict (so callers can read title/value too)
    assert trading._position_row(rows, "0xTOK")["size"] == "12.5"


def test_floats_parser():
    assert trading._floats(["0xabc", "0.5", "10"], 2) == [0.5, 10.0]
    assert trading._floats(["0xabc", "notnum", "10"], 2) is None
    assert trading._floats(["0xabc"], 2) is None
