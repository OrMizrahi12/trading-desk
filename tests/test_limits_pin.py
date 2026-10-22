"""The pinned fingerprint of the limits file: detection, because this machine cannot make the file immutable."""

import functools

import pytest

from conftest import LIMITS
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
