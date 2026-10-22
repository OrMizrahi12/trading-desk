"""The heartbeat: on every closed 5-minute bar of SPY, snapshot -> Claude -> validate -> gates -> paper orders -> journal.

Python never filters or decides for the model: every closed bar is sent to it. Python only (a) refuses a decision
that is invalid or unsafe and (b) fails safe to `wait` when the model gives no usable answer. Paper trading only.
"""

import dataclasses
import hashlib
import json
import logging
import time

import pandas as pd

from . import lab  # noqa: F401
from live.broker import PaperGuardError
from live.execution import DuplicateEntry, ExecutionHalt, OrderRejected
from live.refs import owns
from live.risk import RiskRefused, net_liquidation

from . import execute, gates, seatbelts, snapshot
from .budget import Deadline
from .decision import Decision, InvalidDecision, parse
from .journal import Journal, State
from .model import Model, ModelFailure, instructions_hash

log = logging.getLogger("desk")

BUDGET_S = 120.0  # everything for one bar must fit in this
MODEL_TIMEOUT_S = 75.0  # the longest the model may take; the rest of the budget is kept for the orders
MIN_EXEC_S = 20.0  # a buy/sell is not started with less than this left (a close always is: it reduces risk)
BAR = pd.Timedelta(minutes=5)


class Halted(RuntimeError):
    """The desk stopped itself (paper guard failed, or an order could not be confirmed): a person must look."""


def utc_now() -> pd.Timestamp:
    return pd.Timestamp.now(tz="UTC")


class Heartbeat:
    def __init__(self, *, ib, contract, feed, model: Model, executor, broker, risk, journal: Journal, state: State,
                 symbol: str = "SPY", clock=utc_now, mono=time.monotonic, budget_s: float = BUDGET_S,
                 model_timeout_s: float = MODEL_TIMEOUT_S, min_exec_s: float = MIN_EXEC_S, fetch_daily=None,
                 lab_journal=None):
        self.ib, self.contract, self.feed, self.model = ib, contract, feed, model
        self.executor, self.broker, self.risk = executor, broker, risk
        self.journal, self.state, self.symbol, self.tag = journal, state, symbol, broker.tag
        self.clock, self.mono, self.budget_s = clock, mono, budget_s
        self.model_timeout_s, self.min_exec_s = model_timeout_s, min_exec_s
        self.fetch_daily, self.lab_journal = fetch_daily, lab_journal
        self._daily: tuple[str, pd.DataFrame | None] | None = None
        self._last_close: float | None = None  # newest known price, for the flatten seatbelt
        self._missed_day: str | None = None

    # -- one poll ------------------------------------------------------------------------

    def tick(self) -> list[dict]:
        """Poll the feed once and run the heartbeat for every closed bar not handled yet, oldest first."""
        if self.state.halted:
            raise Halted(self.state.halted)
        try:
            closed, poll_error = self.feed.poll(), None
        except Exception as exc:  # the flatten seatbelt must still run when the feed is down
            closed, poll_error = None, exc
        if closed is not None and not closed.empty:
            self._last_close = float(closed["close"].iloc[-1])
        self._seatbelt_flatten(self.clock())
        if poll_error is not None:
            raise poll_error
        if closed.empty:
            return []
        if self.state.last_bar_ts is None:  # first run: start from the newest bar, do not replay the history
            new = closed.iloc[-1:]
        else:
            new = closed[closed.index > pd.Timestamp(self.state.last_bar_ts)]
        return [self.process_bar(closed, ts) for ts in new.index]

    def run(self, once: bool = False) -> None:
        while True:
            self.tick()
            if once:
                return
            self.ib.sleep(60)  # the lab's POLL_SECONDS

    # -- one bar -------------------------------------------------------------------------

    def _daily_history(self, now: pd.Timestamp):
        day = str(now.tz_convert(snapshot.NY).date())
        if self._daily is None or self._daily[0] != day:  # completed days change once a day
            try:
                self._daily = (day, self.fetch_daily() if self.fetch_daily else None)
            except Exception as exc:
                log.warning("daily bars unavailable (%s); using the 5-minute bars", exc)
                self._daily = (day, None)
        return self._daily[1]

    def process_bar(self, closed: pd.DataFrame, bar_start: pd.Timestamp) -> dict:
        deadline = Deadline(self.budget_s, self.mono)
        now = self.clock()
        record: dict = {"bar": bar_start.isoformat(), "seen_at": now.isoformat(), "instructions": instructions_hash()}
        belts: list[dict] = []  # every seatbelt that acted on this bar
        ref_price = float(closed.loc[bar_start, "close"])
        try:
            snap = snapshot.build(
                closed=closed, bar_start=bar_start, ib=self.ib, tag=self.tag, symbol=self.symbol, now=now,
                daily_history=self._daily_history(now), journal_tail=self.journal.tail(10),
                stop_file=gates.stop_file_present(self.risk.root), limits=dataclasses.asdict(self.risk.limits))
            text = json.dumps(snap, sort_keys=True, default=str)
            record["snapshot_sha1"] = hashlib.sha1(text.encode()).hexdigest()[:12]
            record["snapshot_file"] = str(self.journal.write_snapshot(bar_start.strftime("%Y%m%dT%H%MZ"), snap))
            decision = self._ask(snap, deadline, record)
            record["decision"] = dataclasses.asdict(decision)
            verdict = self._gate(decision, snap, ref_price, deadline)
            record["verdict"] = dataclasses.asdict(verdict)
            if verdict.seatbelt:
                belts.append({"name": verdict.seatbelt, "detail": verdict.note or verdict.reason})
            if verdict.allowed and decision.action != "wait":
                record["result"] = self._execute(decision, verdict, bar_start, ref_price)
                if record["result"].get("status") == "bought":
                    self.state.entry_day = str(now.tz_convert(snapshot.NY).date())
                if record["result"].get("status") == "refused_by_risk_limits":
                    belts.append({"name": "risk_limits", "detail": record["result"]["reason"]})
        except PaperGuardError as exc:
            belts.append({"name": "paper_guard", "detail": str(exc)})
            self._halt(record, f"paper guard: {exc}")
        except ExecutionHalt as exc:
            self._halt(record, f"execution halt: {exc}")
        except Exception as exc:  # the bar is journaled and the loop goes on: the next bar still reaches the model
            log.exception("bar %s failed", bar_start)
            record["error"] = f"{type(exc).__name__}: {exc}"
        if belts:
            record["seatbelts"] = belts
            for b in belts:
                log.warning("SEATBELT %s acted on bar %s: %s", b["name"], bar_start, b["detail"])
        record["elapsed_s"] = round(deadline.elapsed(), 2)
        record["over_budget"] = deadline.expired
        self.journal.append(record)
        self.state.last_bar_ts = bar_start.isoformat()
        self.state.save()
        self._sync_lab_journal()
        if self.state.halted:
            raise Halted(self.state.halted)
        return record

    def _ask(self, snap: dict, deadline: Deadline, record: dict) -> Decision:
        """The model's decision, or `wait` when it fails or answers badly. Never anything else."""
        timeout = min(self.model_timeout_s, deadline.remaining())
        try:
            raw = self.model.decide(snap, timeout)
            record["raw_answer"] = raw
            return parse(raw)
        except ModelFailure as exc:
            record["model_failure"] = str(exc)
        except InvalidDecision as exc:
            record["invalid"] = str(exc)
        return Decision("wait", 0.0, None, None, "no usable model answer")

    def _gate(self, decision: Decision, snap: dict, ref_price: float, deadline: Deadline) -> gates.Verdict:
        if decision.action == "wait":
            return gates.Verdict(True, "wait")
        theirs = execute.mcp_orders(self.ib, self.symbol)
        if theirs:  # the MCP server holds or is entering the symbol: not ours to trade alongside it
            return gates.Verdict(False, f"the MCP server has {len(theirs)} order(s) on {self.symbol}: the heartbeat does "
                                        f"not trade alongside it", seatbelt="mcp_active")
        if decision.action in ("buy", "sell") and deadline.remaining() < self.min_exec_s:
            return gates.Verdict(False, f"budget exhausted: {deadline.remaining():.0f}s left")
        try:
            net_liq = net_liquidation(self.ib)
        except Exception:
            net_liq = None
        pos = execute.position_from_ib(self.ib, self.tag, self.symbol)
        resting_buys = sum(1 for t in self.ib.openTrades() if t.order.action == "BUY"
                           and t.contract.symbol == self.symbol and not t.isDone())
        return gates.gate(decision, ref_price=ref_price, held=pos.shares if pos else 0, resting_buys=resting_buys,
                          session=snap["session"], net_liq=net_liq, position_stop=pos.stop if pos else None,
                          risk=self.risk, ib=self.ib, symbol=self.symbol,
                          stop_file=gates.stop_file_present(self.risk.root))

    def _execute(self, decision: Decision, verdict: gates.Verdict, bar_start: pd.Timestamp, ref_price: float) -> dict:
        try:
            return execute.run(decision, verdict.shares, executor=self.executor, ib=self.ib, tag=self.tag,
                               symbol=self.symbol, bar_ts=bar_start, ref_price=ref_price, state=self.state)
        except RiskRefused as exc:
            return {"status": "refused_by_risk_limits", "reason": exc.reason}
        except DuplicateEntry as exc:
            if exc.in_ib:  # IB shows an entry the desk has no record of: a person must look
                raise ExecutionHalt(f"entry {exc.ref} is already at IB: {exc.found}") from exc
            return {"status": "duplicate_entry", "ref": exc.ref}
        except OrderRejected as exc:
            return {"status": "order_rejected", "reason": str(exc)}

    def _seatbelt_flatten(self, now: pd.Timestamp) -> None:
        """Wall-clock seatbelt: sell any long position before the close, and any carried overnight. Not the model's call.

        Runs on every poll, before the new bars are handled, whatever the model would say. Like the lab's runner.
        """
        pos = execute.position_from_ib(self.ib, self.tag, self.symbol)
        if pos is None:
            if self.state.entry_day is not None:
                self.state.entry_day = None
                self.state.save()
            return
        why = seatbelts.flatten_reason(now, self.state.entry_day)
        if why is None:
            if seatbelts.market_closed_with_position(now) and self._missed_day != str(now.date()):
                self._missed_day = str(now.date())  # once a day: say that a position is held while nothing can be sold
                self._seatbelt_record({"seatbelt": "flatten_before_close", "held": pos.shares,
                                       "result": {"status": "market_closed", "note": "position held while the market is shut"}}, now)
            return
        if execute.mcp_orders(self.ib, self.symbol):  # the shares are the MCP server's: it flattens its own, earlier
            if self._missed_day != f"mcp:{now.date()}":
                self._missed_day = f"mcp:{now.date()}"
                self._seatbelt_record({"seatbelt": "mcp_active", "held": pos.shares,
                                       "result": {"status": "left_to_mcp", "would_have_been": why}}, now)
            return
        record = {"seatbelt": why, "held": pos.shares, "ref_price": self._last_close}
        try:
            if self._last_close is None:
                raise ExecutionHalt(f"{why}: {pos.shares} {self.symbol} must be sold but no price is known yet")
            record["result"] = execute.reduce_position(self.executor, self.ib, self.tag, self.symbol, pos.shares,
                                                       self._last_close)
            if record["result"].get("status") in ("sold", "nothing_to_sell"):
                self.state.entry_day = None
        except PaperGuardError as exc:
            self._halt(record, f"paper guard: {exc}")
        except ExecutionHalt as exc:
            self._halt(record, f"execution halt while flattening: {exc}")
        except Exception as exc:
            log.exception("flatten failed")
            record["error"] = f"{type(exc).__name__}: {exc}"
        self._seatbelt_record(record, now)
        self.state.save()
        self._sync_lab_journal()
        if self.state.halted:
            raise Halted(self.state.halted)

    def _seatbelt_record(self, record: dict, now: pd.Timestamp) -> None:
        record["at"] = now.isoformat()
        log.warning("SEATBELT %s: %s", record["seatbelt"], {k: v for k, v in record.items() if k != "seatbelt"})
        self.journal.append(record)

    def _halt(self, record: dict, why: str) -> None:
        record["halted"] = why
        self.state.halted = why
        log.error("HALT: %s", why)

    def _sync_lab_journal(self) -> None:
        if self.lab_journal is None:
            return
        try:  # the lab's trade journal gets our orders and fills; a failure here never stops the desk
            self.lab_journal.sync(self.ib, owns=lambda ref: owns(self.tag, ref))
        except Exception:
            log.exception("lab journal sync failed")
