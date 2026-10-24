"""State shared by the MCP tools: who owns which order, the latest closed bar, and the per-connection helpers.

Terms:
- orderRef: a text tag on every order. The heartbeat's are `live:desk:SPY...`, this server's `live:mcp:SPY...`.
- owner: IB only lets the client that placed an order change or cancel it, so this server manages ONLY its own
  orders. A position or resting order that belongs to the heartbeat is never touched from here.
"""

import logging
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

from .. import lab  # noqa: F401
from live.broker import Broker
from live.execution import Executor
from live.feed import BarFeed, utc_now
from live.reconcile import ib_shares, open_orders
from live.refs import owns
from live.risk import Risk
from ib_async import Stock

from .. import snapshot
from ..execute import MCP_TAG_PREFIX
from ..journal import DESK_DIR, Journal, State
from .ibworker import IBWorker

log = logging.getLogger("desk.mcp")

SYMBOL = "SPY"  # the only symbol the trade tools touch (the lab's risk limits are default-deny for the rest)
MCP_TAG = f"{MCP_TAG_PREFIX}{SYMBOL}"
HEARTBEAT_TAG = f"live:desk:{SYMBOL}"
MCP_DIR = DESK_DIR.parent / "mcp"  # its own state file and journal: never shares `state.json` with the heartbeat

DELAY_NOTE = ("DELAYED DATA: this paper account has no real-time market data subscription. Bars arrive about 15-20 "
              "minutes after they close and quotes are delayed too, so every price here is old. Never read it as live.")


class Refused(RuntimeError):
    """A tool call that was not carried out, with the reason (shown to the caller)."""


def owner_of(ref: str | None) -> str:
    ref = ref or ""
    return "mcp" if owns(MCP_TAG, ref) else "heartbeat" if owns(HEARTBEAT_TAG, ref) else "other"


class Desk:
    """What the tool functions need besides `ib`: the worker, the risk limits, the state/journal, the clock."""

    def __init__(self, worker: IBWorker, *, risk: Risk | None = None, root: Path = MCP_DIR, clock=utc_now,
                 delay_log: Path | None = None, executor_kwargs: dict | None = None, heartbeat_dir: Path = DESK_DIR, settle_min: float = snapshot.SETTLE_MIN):
        self.worker, self.root, self.clock = worker, Path(root), clock
        self.heartbeat_dir = Path(heartbeat_dir)  # the heartbeat's journal folder (read by search_journal only)
        self.executor_kwargs = executor_kwargs or {}  # e.g. {"timeout_s": 2} in tests
        # The real limits file is checked against its pinned fingerprint before each entry (trade.place_bracket); a Risk
        # handed in (tests) is the caller's own. Not checked here: a mismatch must block entries, not reads or exits.
        self.pinned = risk is None
        self.risk = risk or Risk.from_file()
        self.journal = Journal(self.root)
        self.state = State.load(self.root)
        self.delay_log = delay_log
        self.settle_min = settle_min  # bars newer than this are still being filled by IB (snapshot.settled_bars)
        self._parts: tuple | None = None
        self._contracts: dict[str, object] = {}

    def contract(self, ib, symbol: str):
        key = (id(ib), symbol)
        if key not in self._contracts:
            contract = Stock(symbol, "SMART", "USD")
            if not ib.qualifyContracts(contract):
                raise Refused(f"IB does not know the stock {symbol!r}")
            self._contracts[key] = contract
        return self._contracts[key]

    def parts(self, ib) -> SimpleNamespace:
        """The broker / executor / bar feed for this connection (built once: a Broker hooks IB's error events)."""
        if self._parts is None or self._parts[0] is not ib:
            contract = self.contract(ib, SYMBOL)
            broker = Broker(ib, MCP_TAG, risk=self.risk)
            self._parts = (ib, SimpleNamespace(
                contract=contract, broker=broker, executor=Executor(ib, broker, contract, **self.executor_kwargs),
                feed=BarFeed(ib, contract, clock=self.clock, delay_log=self.delay_log)))
        return self._parts[1]

    def market(self, ib) -> SimpleNamespace:
        """The newest CLOSED 5-minute bar of SPY, and the session facts around it (what the heartbeat's gates read)."""
        now = self.clock()
        raw = self.parts(ib).feed.poll()
        closed = snapshot.settled_bars(raw, now, self.settle_min)
        if closed.empty:
            raise Refused("IB returned no finished 5-minute bars for SPY (market holiday, no data, or the newest bar is "
                          "still being filled: delayed data settles about 16 minutes after a bar's close)" if len(raw)
                          else "IB returned no closed 5-minute bars for SPY (market holiday or no data)")
        bar_start = closed.index[-1]
        return SimpleNamespace(closed=closed, bar_start=bar_start, ref_price=float(closed["close"].iloc[-1]), now=now,
                               session=snapshot.session_info(now, bar_start, bar_start))

    def ownership(self, ib, symbol: str = SYMBOL) -> SimpleNamespace:
        """Who holds / rests what on `symbol`: state is flat | mine | foreign (see the module docstring)."""
        shares = ib_shares(ib, symbol)
        trades = [t for t in open_orders(ib, symbol) if not t.isDone()]
        mine = [t for t in trades if owner_of(t.order.orderRef) == "mcp"]
        foreign = [t for t in trades if owner_of(t.order.orderRef) != "mcp"]
        protection = [t for t in mine if t.order.action == "SELL"]
        if shares > 0 and (foreign or not protection):
            state = "foreign"  # someone else's order rests, or the shares have no stop of ours: not ours to manage
        elif shares > 0 or mine:
            state = "mine"
        else:
            state = "flat"
        return SimpleNamespace(state=state, shares=shares, mine=mine, foreign=foreign)

    def log(self, tool: str, request: dict, outcome: str, **extra) -> None:
        self.journal.append({"at": self.now_iso(), "tool": tool, "request": request, "outcome": outcome, **extra})

    def now_iso(self) -> str:
        return self.clock().isoformat()
