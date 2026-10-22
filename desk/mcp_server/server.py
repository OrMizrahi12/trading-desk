"""The MCP server: tool registration and the process entry point. All the logic lives in read.py / trade.py.

Read tools and trade tools are registered by two separate functions, so a read-only server (`--read-only`) simply
never registers the second one.
"""

import argparse
import asyncio
import logging
import os
import sys
from contextlib import asynccontextmanager
from typing import Annotated

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from .. import lab  # noqa: F401
from live.broker import PaperGuardError

from . import journal_search, read, trade
from .core import MCP_TAG, SYMBOL, Desk, Refused
from .ibworker import CLIENT_ID, IBWorker, Unavailable

log = logging.getLogger("desk.mcp")
WATCH_S = 30  # how often the wall-clock seatbelt looks at our position

PAPER = "PAPER account only (simulated money). "
DELAYED = ("Data is DELAYED: this paper account has no real-time market data subscription, so bars arrive about 15-20 "
           "minutes after they close and quotes are delayed. ")

INSTRUCTIONS = (
    "Tools for the paper trading desk. Everything is simulated money on the IB paper Gateway (client id "
    f"{CLIENT_ID}). Market data is DELAYED by about 15-20 minutes: never treat a price as live. Read tools are safe; "
    "trade tools place real (paper) orders through the same gates and risk limits as the desk's heartbeat, only for SPY "
    "and only long. The desk's style (see CLAUDE.md): mostly wait, risk at most 1R per trade, every buy has a stop, be flat "
    "before the close. Explain every trading term you use to the user.")

READ = ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=False)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False)
WRITE_DESTRUCTIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=False)


async def _run(desk: Desk, fn, **kwargs):
    """Run a tool function on the IB thread; every failure reaches the caller as a readable ToolError."""
    try:
        return await desk.worker.call(fn, desk, **kwargs)
    except ToolError:
        raise
    except (Refused, Unavailable, PaperGuardError) as exc:
        raise ToolError(str(exc)) from exc
    except Exception as exc:
        log.exception("%s failed", getattr(fn, "__name__", fn))
        raise ToolError(f"{type(exc).__name__}: {exc}") from exc


def register_read_tools(mcp: MCPServer, desk: Desk) -> None:
    @mcp.tool(title="Quote", annotations=READ, description=(
        PAPER + DELAYED + "Latest price snapshot for a US stock (default SPY): bid (best price a buyer offers), ask (best "
        "price a seller asks), last (latest trade), previous close, volume, and the time of the reading. If IB sends no "
        "delayed quote, falls back to the close of the newest closed 5-minute bar and says so in `source`. Reading only."))
    async def get_quote(symbol: Annotated[str, Field(description="US stock symbol, e.g. SPY")] = SYMBOL) -> dict:
        return await _run(desk, read.get_quote, symbol=symbol)

    @mcp.tool(title="Historical bars", annotations=READ, description=(
        PAPER + DELAYED + "Closed OHLCV bars (open, high, low, close prices and volume for one time slice) for any IB bar size "
        "and duration. The bar still forming is left out; oldest first; times are New York. bar_size is one of: "
        + ", ".join(read.BAR_SIZES) + ". duration is IB's form: a number, a space, then S (seconds), D (days), W, M or Y, "
        "e.g. '1 D', '2 W'; IB caps what it will send for small bar sizes (e.g. 1 sec bars: 30 minutes), and refuses "
        "repeated identical requests within 15 seconds, so a call may wait. At most "
        f"{read.MAX_ROWS} rows come back (the newest). Reading only."))
    async def get_bars(
            symbol: Annotated[str, Field(description="US stock symbol")] = SYMBOL,
            bar_size: Annotated[str, Field(description="IB bar size, e.g. '5 mins', '1 hour', '1 day'")] = "5 mins",
            duration: Annotated[str, Field(description="How far back, IB form, e.g. '1 D', '2 W'")] = "1 D",
            what_to_show: Annotated[str, Field(description="TRADES, MIDPOINT, BID or ASK")] = "TRADES",
            regular_hours_only: Annotated[bool, Field(description="True = only regular trading hours (9:30-16:00 New York)")] = True,
            max_rows: Annotated[int, Field(description="Newest rows to return", ge=1, le=read.MAX_ROWS)] = 100) -> dict:
        return await _run(desk, read.get_bars, symbol=symbol, bar_size=bar_size, duration=duration,
                          what_to_show=what_to_show, use_rth=regular_hours_only, max_rows=max_rows)

    @mcp.tool(title="Indicators", annotations=READ, description=(
        PAPER + DELAYED + "Technical indicators from the lab's indicators module, on closed bars of any size: ema9/ema21 "
        "(exponential moving averages, smoothed prices; price above both = uptrend), rsi14 (relative strength index, "
        "0-100 momentum gauge), atr14 (average true range, typical bar size in dollars), bb_lower/bb_upper (Bollinger "
        "bands, 2 standard deviations around a 20-bar average) and, for intraday bars, vwap (volume-weighted average price "
        "since the open). An indicator is null when there are too few bars. Returns the newest `rows` rows plus a glossary. "
        "Reading only."))
    async def get_indicators(
            symbol: Annotated[str, Field(description="US stock symbol")] = SYMBOL,
            bar_size: Annotated[str, Field(description="IB bar size, e.g. '5 mins', '15 mins', '1 day'")] = "5 mins",
            duration: Annotated[str, Field(description="History to compute on, IB form; needs 21+ bars for ema21")] = "2 D",
            rows: Annotated[int, Field(description="Newest rows to return", ge=1, le=100)] = 5) -> dict:
        return await _run(desk, read.get_indicators, symbol=symbol, bar_size=bar_size, duration=duration, rows=rows)

    @mcp.tool(title="Positions", annotations=READ, description=(
        PAPER + "Open positions as IB reports them (long shares and average cost per share; not delayed). Each is marked with "
        "its owner: only a position whose stop this server placed can be managed by the trade tools; the heartbeat's cannot. Reading only."))
    async def get_positions() -> dict:
        return await _run(desk, read.get_positions)

    @mcp.tool(title="Open orders", annotations=READ, description=(
        PAPER + "Working orders from every client, with the owner of each (mcp = placed by this server, heartbeat, other) "
        "and whether it can be modified or cancelled here (`manageable_here`). A stop is a resting order that sells at your "
        "loss limit; a target is a resting sell at your profit goal. Reading only."))
    async def get_open_orders() -> dict:
        return await _run(desk, read.get_open_orders)

    @mcp.tool(title="Account summary", annotations=READ, description=(
        PAPER + "Account id (must start with DU: paper), net liquidation (total account value, cash plus positions), cash, buying "
        "power, P&L, the dollar size of 1R (1% of net liquidation), and SPY shares held. Reading only."))
    async def get_account_summary() -> dict:
        return await _run(desk, read.get_account_summary)

    @mcp.tool(title="Search journal", annotations=READ, description=(
        "Search the desk's journals: the heartbeat's decisions (data/desk) and this server's own trade calls (data/mcp), "
        "newest first, as compact records (action, size in R, stop, target, reason, gate verdict, result). Filter by text, "
        "action (buy/sell/close/wait), tool (place_bracket, modify_order, cancel_order, close_position, seatbelt), seatbelt "
        "name (stop_file, risk_limits, entry_window, flatten_before_close...), result status (bought, sold, refused...), "
        "and a time range (ISO; a bare date means New York time). Reads local files only; no IB connection, no delay."))
    def search_journal(
            text: Annotated[str | None, Field(description="Case-insensitive text to find anywhere in a record")] = None,
            action: Annotated[str | None, Field(description="buy, sell, close or wait (heartbeat decisions)")] = None,
            tool: Annotated[str | None, Field(description="An MCP trade tool name, or 'seatbelt'")] = None,
            seatbelt: Annotated[str | None, Field(description="A seatbelt name that acted")] = None,
            status: Annotated[str | None, Field(description="Result status or outcome, e.g. bought, sold, refused, done")] = None,
            since: Annotated[str | None, Field(description="ISO date/time lower bound")] = None,
            until: Annotated[str | None, Field(description="ISO date/time upper bound")] = None,
            source: Annotated[str, Field(description="all, desk (heartbeat) or mcp (this server)")] = "all",
            limit: Annotated[int, Field(description="Max records", ge=1, le=journal_search.MAX_LIMIT)] = 20) -> dict:
        try:
            return journal_search.search(text=text, action=action, tool=tool, seatbelt=seatbelt, status=status, since=since,
                                         until=until, source=source, limit=limit, desk_dir=desk.heartbeat_dir, mcp_dir=desk.root)
        except Refused as exc:
            raise ToolError(str(exc)) from exc


def register_trade_tools(mcp: MCPServer, desk: Desk) -> None:
    common = (PAPER + DELAYED + f"{SYMBOL} only, long only. The decision is checked by the same validator and risk limits "
              "as the heartbeat (the STOP file kill switch, session open, no entries in the last 10 minutes, no adding to a "
              "position, size in R, the lab's max shares / position value / trades per day / daily loss). The prices it checks "
              "against are the close of the newest closed 5-minute bar, which is 15-20 minutes old. ")

    @mcp.tool(title="Place bracket", annotations=WRITE, description=(
        common + "Buys SPY with a protective stop and a profit target together (a bracket: when one of stop/target fills the "
        "other is cancelled). Shares are sized from size_r: 1R is 1% of net liquidation lost if the stop is hit, so shares = "
        "size_r * 1% * net liquidation / (price - stop); the risk limits may shrink it. size_r above 1 is refused; typical 0.5. "
        "stop must be below the price and target above it; aim for reward-to-risk (target distance / stop distance) of at least "
        "2. A target is expected: pass no_target_reason instead only with a real explanation. Refused while the heartbeat "
        "has a position or orders on SPY. The server sells any position of its own 15 minutes before the close. Places a paper order."))
    async def place_bracket(
            size_r: Annotated[float, Field(description="Risk in R (1R = 1% of net liquidation). 0 < size_r <= 1; typical 0.5", gt=0, le=1)],
            stop: Annotated[float, Field(description="Stop price: below the last close, where the idea is plainly wrong", gt=0)],
            reason: Annotated[str, Field(description="One short sentence a beginner can follow (max 280 chars); explain terms", min_length=1, max_length=280)],
            target: Annotated[float | None, Field(description="Profit target price above the last close", gt=0)] = None,
            no_target_reason: Annotated[str | None, Field(description="Why there is no target (only when target is omitted)")] = None) -> dict:
        return await _run(desk, trade.place_bracket, size_r=size_r, stop=stop, target=target, reason=reason,
                          no_target_reason=no_target_reason)

    @mcp.tool(title="Modify stop or target", annotations=WRITE, description=(
        PAPER + DELAYED + f"Changes the price of a resting stop or target that this server placed (orders tagged {MCP_TAG}; "
        "the heartbeat's cannot be touched, IB only lets the placing client change an order). Find order_id with "
        "get_open_orders. A stop only ever moves UP and must stay below the last close (never moved down or widened; if the "
        "idea is wrong use close_position). A target must stay above the last close and keep reward-to-risk (distance to "
        "target / distance to stop) at least 2. Entries cannot be modified: cancel and place a new bracket. Changes a paper order."))
    async def modify_order(
            order_id: Annotated[int, Field(description="IB order id from get_open_orders")],
            new_price: Annotated[float, Field(description="New stop price or new target price", gt=0)]) -> dict:
        return await _run(desk, trade.modify_order, order_id=order_id, new_price=float(new_price))

    @mcp.tool(title="Cancel order", annotations=WRITE_DESTRUCTIVE, description=(
        PAPER + f"Cancels an order this server placed (tag {MCP_TAG}). Cancelling an unfilled entry also drops its stop and "
        "target. Refused for the protective stop of an open position or pending entry (that would leave it unprotected: use "
        "modify_order or close_position), for a partly filled entry, and for any order that is not this server's. Cancels a paper order."))
    async def cancel_order(order_id: Annotated[int, Field(description="IB order id from get_open_orders")]) -> dict:
        return await _run(desk, trade.cancel_order, order_id=order_id)

    @mcp.tool(title="Close position", annotations=WRITE_DESTRUCTIVE, description=(
        PAPER + DELAYED + f"Sells the whole {SYMBOL} long position this server opened: cancels its stop and target, then sells with "
        "a marketable limit (a limit order priced slightly through the market so it fills at once but never at a wild price), "
        "repricing up to 3 times. Needs the market to be open. Refused if the shares are the heartbeat's (selling them here "
        "could leave the heartbeat's stop live on shares already sold). Sells paper shares."))
    async def close_position(reason: Annotated[str, Field(description="One short sentence on why (max 280 chars)", min_length=1, max_length=280)]) -> dict:
        return await _run(desk, trade.close_position, reason=reason)


async def _watchdog(desk: Desk, every_s: float) -> None:
    """Wall-clock seatbelt: nothing here waits for a tool call, so a bracket is never left running into the close."""
    while True:
        await asyncio.sleep(every_s)
        try:
            if desk.state.entry_day is not None or desk.worker.connected:
                await desk.worker.call(trade.seatbelt_tick, desk)
        except Exception as exc:
            log.warning("seatbelt check failed: %s: %s", type(exc).__name__, exc)


def create_server(desk: Desk, *, read_only: bool = False, watch_s: float = WATCH_S) -> MCPServer:
    @asynccontextmanager
    async def lifespan(_server):
        task = asyncio.create_task(_watchdog(desk, watch_s)) if watch_s and not read_only else None
        try:
            yield desk
        finally:
            if task:
                task.cancel()
            await desk.worker.close()

    mcp = MCPServer("trading-desk", instructions=INSTRUCTIONS, lifespan=lifespan, log_level="INFO")
    register_read_tools(mcp, desk)
    if not read_only:
        register_trade_tools(mcp, desk)
    return mcp


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(prog="python -m desk.mcp_server", description="Paper desk MCP server (stdio)")
    parser.add_argument("--read-only", action="store_true", default=os.environ.get("DESK_MCP_READONLY") == "1",
                        help="register only the read tools (also: DESK_MCP_READONLY=1)")
    args = parser.parse_args(argv)
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    desk = Desk(IBWorker())
    create_server(desk, read_only=args.read_only).run()
