"""The MCP server's read tools and its tool list, against a fake IB (no network)."""

import json
import math

import pandas as pd
import pytest
from ib_async import BarData

from mcp_fakes import anyio_backend, data, mcp_rig, to_bar_data  # noqa: F401
import indicators
from live.broker import PaperGuardError, connect_paper

pytestmark = pytest.mark.anyio

READ_TOOLS = {"get_quote", "get_bars", "get_indicators", "get_positions", "get_open_orders", "get_account_summary",
              "search_journal"}
TRADE_TOOLS = {"place_bracket", "modify_order", "cancel_order", "close_position"}


async def test_every_tool_has_a_description_and_the_delay_is_stated(mcp_rig):
    rig = mcp_rig()
    async with rig.client() as c:
        tools = {t.name: t for t in (await c.list_tools()).tools}
    assert set(tools) == READ_TOOLS | TRADE_TOOLS
    for name, tool in tools.items():
        assert tool.description and len(tool.description) > 80, name
        if name != "search_journal":  # reads local files only
            assert "PAPER" in tool.description, name
        if name not in ("search_journal", "get_positions", "get_open_orders", "get_account_summary", "cancel_order"):
            assert "DELAYED" in tool.description, name
    for name in READ_TOOLS:
        assert tools[name].annotations.read_only_hint is True
    for name in TRADE_TOOLS:
        assert tools[name].annotations.read_only_hint is False


async def test_read_only_server_has_no_trade_tools(mcp_rig):
    rig = mcp_rig(read_only=True)
    async with rig.client() as c:
        names = {t.name for t in (await c.list_tools()).tools}
    assert names == READ_TOOLS


async def test_get_bars_returns_closed_bars_only_and_says_they_are_delayed(mcp_rig):
    rig = mcp_rig()
    forming = rig.bars.iloc[[195]].copy()
    forming.index = [rig.now - pd.Timedelta(minutes=2)]  # started 2 minutes ago: not closed yet
    forming.iloc[0, forming.columns.get_loc("close")] = 999.0
    rig.ib.history["5 mins"] = to_bar_data(pd.concat([rig.bars.iloc[:196], forming]))
    async with rig.client() as c:
        out = data(await c.call_tool("get_bars", {"bar_size": "5 mins", "duration": "1 D", "max_rows": 50}))
    assert out["delayed"] is True and "DELAYED" in out["note"]
    assert out["bars_available"] == 196 and out["bars_returned"] == 50 and out["truncated"] is True
    assert all(b["close"] != 999.0 for b in out["bars"])
    assert out["newest_bar_age_minutes"] == 12.0
    assert out["bars"][-1]["start"].endswith("-04:00")  # New York time
    assert rig.ib.history_requests[-1][:3] == ("SPY", "1 D", "5 mins")


async def test_get_bars_daily_and_bad_arguments(mcp_rig):
    rig = mcp_rig()
    days = pd.DataFrame({"open": [1.0, 2.0], "high": [2, 3], "low": [0.5, 1.5], "close": [1.5, 2.5], "volume": [10, 20]})
    import datetime
    rig.ib.history["1 day"] = [BarData(date=datetime.date(2026, 10, 20 + i), open=r.open, high=r.high, low=r.low,
                                       close=r.close, volume=r.volume, average=0, barCount=1) for i, r in days.iterrows()]
    async with rig.client() as c:
        out = data(await c.call_tool("get_bars", {"bar_size": "1 day", "duration": "2 D"}))
        assert [b["start"] for b in out["bars"]] == ["2026-10-20", "2026-10-21"]
        bad = await c.call_tool("get_bars", {"bar_size": "7 mins"})
        assert bad.is_error and "1 min" in bad.content[0].text
        bad = await c.call_tool("get_bars", {"duration": "yesterday"})
        assert bad.is_error and "duration" in bad.content[0].text
        bad = await c.call_tool("get_bars", {"symbol": "SPY; DROP"})
        assert bad.is_error


async def test_get_bars_reports_ibs_reason_when_no_bars_come_back(mcp_rig):
    rig = mcp_rig()
    rig.ib.history["1 min"] = []
    rig.ib.history_errors = [(162, "Historical Market Data Service error message: no permission")]
    async with rig.client() as c:
        out = await c.call_tool("get_bars", {"bar_size": "1 min"})
    assert out.is_error and "no permission" in out.content[0].text


async def test_indicators_match_the_lab_module(mcp_rig):
    rig = mcp_rig()
    close = rig.bars["close"].iloc[:196]
    async with rig.client() as c:
        out = data(await c.call_tool("get_indicators", {"rows": 2}))
    last = out["rows"][-1]
    assert last["ema9"] == round(float(indicators.ema(close, 9).iloc[-1]), 4)
    assert last["ema21"] == round(float(indicators.ema(close, 21).iloc[-1]), 4)
    assert last["rsi14"] == round(float(indicators.rsi(close, 14).iloc[-1]), 4)
    assert last["atr14"] is not None and last["bb_lower"] < last["close"] < last["bb_upper"] and last["vwap"] is not None
    assert len(out["rows"]) == 2 and "relative strength" in out["glossary"]["rsi14"] and out["delayed"] is True


async def test_indicators_are_null_with_too_few_bars(mcp_rig):
    rig = mcp_rig()
    rig.ib.history["5 mins"] = to_bar_data(rig.bars.iloc[180:196])  # 16 bars
    async with rig.client() as c:
        last = data(await c.call_tool("get_indicators", {"rows": 1}))["rows"][-1]
    assert last["ema21"] is None and last["bb_lower"] is None and last["ema9"] is not None


async def test_quote_from_a_delayed_tick_and_the_bar_fallback(mcp_rig):
    rig = mcp_rig()
    async with rig.client() as c:
        fallback = data(await c.call_tool("get_quote", {}))
        assert fallback["source"] == "last_closed_5min_bar" and fallback["last"] == pytest.approx(rig.ref)
        rig.ib.ticker.bid, rig.ib.ticker.ask, rig.ib.ticker.last = 600.1, 600.2, 600.15
        quote = data(await c.call_tool("get_quote", {}))
    assert quote["source"] == "delayed_quote" and (quote["bid"], quote["ask"], quote["last"]) == (600.1, 600.2, 600.15)
    assert quote["delayed"] is True and "DELAYED" in quote["note"]
    assert rig.ib.mkt_cancelled == ["SPY", "SPY"]  # no market-data line is left open


async def test_account_positions_and_orders_say_who_owns_what(mcp_rig):
    rig = mcp_rig()
    rig.ib.holdings["SPY"] = 3
    rig.heartbeat_order()
    async with rig.client() as c:
        acct = data(await c.call_tool("get_account_summary", {}))
        pos = data(await c.call_tool("get_positions", {}))
        orders = data(await c.call_tool("get_open_orders", {}))
    assert acct["paper_account"] is True and acct["NetLiquidation"] == 100000.0 and acct["one_R_dollars"] == 1000.0
    assert pos["positions"][0]["shares"] == 3 and pos["positions"][0]["manageable_here"] is False
    assert orders["orders"][0]["owner"] == "heartbeat" and orders["orders"][0]["manageable_here"] is False


async def test_connects_as_client_20_and_refuses_a_non_paper_account(mcp_rig, monkeypatch):
    from mcp_fakes import McpFakeIB
    from desk.mcp_server import ibworker
    seen = []
    live_like = McpFakeIB(accounts=("U1234567",))  # no DU prefix: a real-money account
    live_like.net_liquidation = 5.0

    def fake_connect_paper(factory, **kw):
        seen.append(kw)
        return connect_paper(lambda: live_like, **kw)

    monkeypatch.setattr(ibworker, "connect_paper", fake_connect_paper)
    rig = mcp_rig()
    rig.worker = ibworker.IBWorker()  # the default connection path
    rig.desk.worker = rig.worker
    async with rig.client() as c:
        out = await c.call_tool("get_account_summary", {})
    assert seen == [{"client_id": 20}] and out.is_error
    assert live_like.placed == [] and not live_like.isConnected()  # it hung up
    assert ibworker.CLIENT_ID == 20


async def test_gateway_down_is_a_readable_error(mcp_rig):
    rig = mcp_rig()

    def refuse():
        raise ConnectionRefusedError("[Errno 111] Connect call failed")

    rig.worker._connect = refuse
    rig.worker.ib = None
    rig.ib.disconnect()
    async with rig.client() as c:
        out = await c.call_tool("get_quote", {})
    assert out.is_error and "paper Gateway" in out.content[0].text and "client 20" in out.content[0].text


async def test_nothing_printed_by_lab_helpers_reaches_stdout(mcp_rig, capsys):
    rig = mcp_rig()
    orig = rig.ib.reqMktData

    def noisy(*a, **k):
        print("throttle: waiting 15s")  # the lab's Throttle does exactly this
        return orig(*a, **k)

    rig.ib.reqMktData = noisy
    async with rig.client() as c:
        await c.call_tool("get_quote", {})
    captured = capsys.readouterr()
    assert "throttle" not in captured.out and "throttle" in captured.err
