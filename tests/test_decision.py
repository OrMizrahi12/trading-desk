"""The decision schema: strict, never repairing."""

import pytest

from desk.decision import MAX_SIZE_R, InvalidDecision, check_prices, parse


def ok(**kw):
    base = {"action": "buy", "size_r": 1.0, "stop": 99.0, "target": 105.0, "reason": "because", "lessons": []}
    return {**base, **kw}


def test_valid_answers_of_every_action():
    assert parse(ok()).action == "buy"
    assert parse(ok(target=None)).target is None
    assert parse(ok(action="sell", stop=None, target=None)).action == "sell"
    assert parse(ok(action="close", size_r=0, stop=None, target=None)).action == "close"
    assert parse(ok(action="wait", size_r=0, stop=None, target=None)).action == "wait"


@pytest.mark.parametrize("bad", [
    "wait", None, [], {},
    ok(action="short"), ok(action="BUY"),
    ok(size_r=0), ok(size_r=-1), ok(size_r=MAX_SIZE_R + 0.1), ok(size_r=True), ok(size_r="1"), ok(size_r=float("nan")),
    ok(stop=None), ok(stop=0), ok(stop=-5), ok(stop="99"), ok(target=0), ok(target=float("inf")),
    ok(reason=""), ok(reason="   "), ok(reason="x" * 281), ok(reason=None),
    ok(action="sell", size_r=0),
])
def test_invalid_answers_are_rejected(bad):
    with pytest.raises(InvalidDecision):
        parse(bad)


def test_extra_or_missing_keys_are_rejected():
    with pytest.raises(InvalidDecision):
        parse({**ok(), "leverage": 5})
    raw = ok()
    del raw["target"]
    with pytest.raises(InvalidDecision):
        parse(raw)


def test_buy_prices_must_straddle_the_price():
    check_prices(parse(ok()), 100.0)
    with pytest.raises(InvalidDecision):
        check_prices(parse(ok(stop=101.0)), 100.0)
    with pytest.raises(InvalidDecision):
        check_prices(parse(ok(target=99.5)), 100.0)
    check_prices(parse(ok(action="wait", size_r=0, stop=None, target=None)), 100.0)  # only buys have prices to check
