"""The pinned fingerprint of the limits file: detection, because this machine cannot make the file immutable."""

import functools

import pandas as pd
import pytest

from conftest import CLOSE, LIMITS, WAIT, buy, rig, sell  # noqa: F401
from mcp_fakes import anyio_backend, data, mcp_rig  # noqa: F401
from desk import limits_pin
from desk.limits_pin import LimitsTampered, verify


pytestmark = pytest.mark.anyio


@pytest.fixture
def pinned(tmp_path):
    limits, pin = tmp_path / "risk_limits.yaml", tmp_path / "limits.sha256"
    limits.write_text(LIMITS)
    pin.write_text(f"{limits_pin.fingerprint(limits)}  risk_limits.yaml\n")
    return limits, pin


def test_an_untouched_file_matches_its_pin(pinned):
    verify(*pinned)


def test_an_edited_file_is_caught(pinned):
    limits, pin = pinned
    limits.write_text(LIMITS.replace("max_shares: 10", "max_shares: 1000"))
    with pytest.raises(LimitsTampered, match="no longer matches"):
        verify(limits, pin)


def test_a_file_swapped_by_rename_is_caught(pinned, tmp_path):
    """The hole chmod/chown cannot close here: the folder is ours, so a root-owned file can still be replaced."""
    limits, pin = pinned
    other = tmp_path / "other.yaml"
    other.write_text(LIMITS.replace("daily_loss_limit: 500", "daily_loss_limit: 500000"))
    other.replace(limits)
    with pytest.raises(LimitsTampered):
        verify(limits, pin)


def test_a_missing_file_or_pin_fails_closed(pinned):
    limits, pin = pinned
    pin.write_text("")
    with pytest.raises(LimitsTampered, match="cannot compare"):
        verify(limits, pin)
    pin.unlink()
    with pytest.raises(LimitsTampered, match="cannot compare"):
        verify(limits, pin)


def test_the_committed_pin_matches_the_real_limits_file():
    """If a person changed the limits on purpose, this fails until they run `python -m desk.limits_pin --update`."""
    verify()


async def test_the_server_refuses_entries_but_not_exits_when_the_file_no_longer_matches(mcp_rig, monkeypatch, pinned):
    from desk.mcp_server import trade
    limits, pin = pinned
    rig = mcp_rig()
    rig.desk.pinned = True
    monkeypatch.setattr(trade.limits_pin, "verify", functools.partial(verify, limits, pin))
    args = {"size_r": 0.5, "stop": round(rig.ref - 3, 2), "target": round(rig.ref + 8, 2), "reason": "test"}
    async with rig.client() as c:
        first = await c.call_tool("place_bracket", args)
        assert not first.is_error and data(first)["status"] == "bought"  # the file matches: the entry goes through
        limits.write_text(LIMITS.replace("max_shares: 10", "max_shares: 1000"))  # someone swaps the limits
        blocked = await c.call_tool("place_bracket", args)
        assert blocked.is_error and "no longer matches its pin" in blocked.content[0].text
        closed = await c.call_tool("close_position", {"reason": "get out"})
        assert not closed.is_error and data(closed)["status"] == "sold"  # exits never depend on the pin
    assert [r["outcome"] for r in rig.journal()] == ["done", "refused", "done"]


# -- the heartbeat: same rule, entries refused and exits never blocked -------------------------------------


class Pin:
    """A stand-in for limits_pin.verify whose answer a test can flip."""

    def __init__(self, ok=True):
        self.ok, self.calls = ok, 0

    def __call__(self):
        self.calls += 1
        if not self.ok:
            raise LimitsTampered("the limits file no longer matches its pin (test)")


def belts(r):
    return {b["name"]: b["detail"] for rec in r.records() for b in rec.get("seatbelts", [])}


def next_bar(r):
    r.feed.shown += 1
    r.now = r.now + pd.Timedelta(minutes=5)


def test_heartbeat_refuses_a_buy_when_the_limits_file_no_longer_matches(rig):
    r = rig(answers=[buy()], limits_check=Pin(ok=False))
    r.hb.tick()
    rec = r.records()[0]
    assert len(r.model.calls) == 1  # the model still saw the bar: Python only refuses
    assert rec["verdict"]["allowed"] is False and rec["verdict"]["seatbelt"] == "limits_pin"
    assert "no longer matches" in rec["verdict"]["reason"] and "result" not in rec
    assert "limits_pin" in belts(r) and r.ib.placed == [] and r.ib.holdings.get("SPY", 0) == 0


def test_heartbeat_buys_when_the_limits_file_matches(rig):
    pin = Pin(ok=True)
    r = rig(answers=[buy()], limits_check=pin)
    r.hb.tick()
    assert pin.calls == 1 and r.ib.holdings["SPY"] > 0 and "limits_pin" not in belts(r)


def test_heartbeat_exits_and_waits_never_depend_on_the_pin(rig):
    for answer in (CLOSE, sell(0.01), WAIT):  # 0.01R is a few shares: a real partial sale
        pin = Pin(ok=True)
        r = rig(answers=[buy(size_r=1.0), answer], limits_check=pin)
        r.hb.tick()
        held = r.ib.holdings["SPY"]
        assert held > 0
        pin.ok, pin.calls = False, 0  # the limits file is swapped after the entry
        next_bar(r)
        r.hb.tick()
        assert pin.calls == 0 and "limits_pin" not in belts(r), answer["action"]  # not even consulted
        if answer["action"] == "close":
            assert r.ib.holdings["SPY"] == 0
        elif answer["action"] == "sell":
            assert 0 < r.ib.holdings["SPY"] < held


def test_the_flatten_seatbelt_still_sells_when_the_limits_file_no_longer_matches(rig):
    pin = Pin(ok=True)
    r = rig(answers=[buy(size_r=1.0), WAIT], limits_check=pin)
    r.hb.tick()
    pin.ok = False
    r.now = pd.Timestamp("2026-10-21 15:50", tz="America/New_York").tz_convert("UTC")
    r.hb.tick()
    assert r.ib.holdings["SPY"] == 0


def test_the_real_run_passes_the_check_to_the_heartbeat():
    """desk/__main__.py builds the real Heartbeat; without this argument the pin would silently not apply there."""
    import inspect
    from desk import __main__ as entry
    assert "limits_check=limits_pin.verify" in inspect.getsource(entry.main)
