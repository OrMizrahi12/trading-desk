"""Journal search over the heartbeat's journal and the MCP server's own, with the odd lines both writers leave."""

import json

import pytest

from mcp_fakes import anyio_backend, data, mcp_rig  # noqa: F401

pytestmark = pytest.mark.anyio

DESK_LINES = [
    {"bar": "2026-10-21T09:45:00-04:00", "decision": {"action": "wait", "size_r": 0, "stop": None, "target": None,
                                                         "reason": "first 15 minutes are wild"}, "verdict": {"allowed": True, "reason": "wait"}},
    {"bar": "2026-10-21T10:30:00-04:00", "decision": {"action": "buy", "size_r": 0.5, "stop": 597.0, "target": 606.0,
                                                         "reason": "pullback to ema9 in an uptrend"},
     "verdict": {"allowed": True, "reason": "buy", "shares": 8, "seatbelt": "risk_limits", "note": "size cut from 12 to 8 shares"},
     "result": {"status": "bought", "shares": 8, "price": 600.1}, "seatbelts": [{"name": "risk_limits", "detail": "cut"}]},
    {"seatbelt": "flatten_before_close", "at": "2026-10-21T15:50:00-04:00", "held": 8, "result": {"status": "sold", "shares": 8, "price": 601.0}},
    {"bar": "2026-10-22T09:50:00-04:00", "decision": {"action": "buy", "size_r": 1.0, "stop": 590.0, "target": None, "reason": "chase"},
     "verdict": {"allowed": False, "reason": "STOP file present: no new buys", "seatbelt": "stop_file"}, "seatbelts": [{"name": "stop_file"}]},
]


def write(folder, lines, junk=False):
    folder.mkdir(parents=True, exist_ok=True)
    text = "\n".join(json.dumps(l) for l in lines)
    if junk:
        text = "not json at all\n" + text + "\n[1, 2]\n\n"
    (folder / "journal.jsonl").write_text(text + "\n")


async def search(rig, **args):
    async with rig.client() as c:
        out = await c.call_tool("search_journal", args)
    return out.content[0].text if out.is_error else data(out)


async def test_search_filters_and_orders_newest_first(mcp_rig, tmp_path):
    rig = mcp_rig()
    desk_dir = tmp_path / "desk_journal"
    write(desk_dir, DESK_LINES, junk=True)
    write(rig.desk.root, [{"at": "2026-10-21T11:00:00-04:00", "tool": "place_bracket", "request": {"reason": "mcp test buy"},
                           "outcome": "done", "result": {"status": "bought", "shares": 5}}])
    rig.desk.heartbeat_dir = desk_dir

    everything = await search(rig, limit=50)
    assert everything["records_scanned"] == 5 and everything["matches"] == 5  # junk lines skipped
    assert [r["when"] for r in everything["records"]] == [
        "2026-10-22T09:50:00-04:00", "2026-10-21T15:50:00-04:00", "2026-10-21T11:00:00-04:00",
        "2026-10-21T10:30:00-04:00", "2026-10-21T09:45:00-04:00"]

    buys = await search(rig, action="buy")
    assert [r["when"][:10] for r in buys["records"]] == ["2026-10-22", "2026-10-21"] and buys["records"][1]["reason"].startswith("pullback")
    assert (await search(rig, seatbelt="stop_file"))["records"][0]["verdict"]["reason"].startswith("STOP file")
    assert (await search(rig, seatbelt="flatten_before_close"))["records"][0]["result"]["status"] == "sold"
    assert (await search(rig, status="bought"))["matches"] == 2  # one from each journal
    assert (await search(rig, text="PULLBACK"))["matches"] == 1
    assert (await search(rig, source="mcp"))["records"][0]["tool"] == "place_bracket"
    assert (await search(rig, tool="place_bracket"))["matches"] == 1
    day = await search(rig, since="2026-10-21", until="2026-10-21T12:00:00-04:00")
    assert day["matches"] == 3 and all(r["when"].startswith("2026-10-21") for r in day["records"])
    assert len((await search(rig, limit=2))["records"]) == 2


async def test_search_with_no_journals_and_a_bad_date(mcp_rig, tmp_path):
    rig = mcp_rig()
    out = await search(rig, source="mcp")
    assert out["matches"] == 0 and out["records"] == []
    bad = await search(rig, since="last tuesday-ish")
    assert "not a date" in bad
    assert "source must be" in await search(rig, source="lab")
