"""Shared fixtures: the lab's FakeIB, a fake model, a scripted bar feed and a fixed clock. No network, no real IB."""

import sys

import pandas as pd
import pytest

from desk import lab

sys.path.insert(0, str(lab.LAB / "tests"))  # the lab's fake_ib.py (a fake ib_async.IB)

from fake_ib import FakeIB  # noqa: E402
from live.broker import Broker  # noqa: E402
from live.execution import Executor  # noqa: E402
from live.risk import Risk, load_limits  # noqa: E402
from types import SimpleNamespace  # noqa: E402

from desk.heartbeat import Heartbeat  # noqa: E402
from desk.journal import Journal, State  # noqa: E402

LIMITS = """
symbols:
  SPY: {max_shares: 10, max_position_value: 8000}
max_trades_per_day: 4
daily_loss_limit: 500
"""
TAG = "live:desk:SPY"


def make_bars(days=("2026-10-19", "2026-10-20", "2026-10-21"), price=600.0) -> pd.DataFrame:
    """78 regular-session 5-minute bars (09:30-16:00 New York) per day, price drifting up a little."""
    rows, idx, p = [], [], price
    for day in days:
        start = pd.Timestamp(f"{day} 09:30", tz="America/New_York")
        for i in range(78):
            p += 0.05 if i % 3 else -0.04
            idx.append((start + pd.Timedelta(minutes=5 * i)).tz_convert("UTC"))
            rows.append({"open": p - 0.02, "high": p + 0.1, "low": p - 0.1, "close": p, "volume": 1000 + i})
    return pd.DataFrame(rows, index=pd.DatetimeIndex(idx))


class FakeFeed:
    """poll() returns the first `shown` bars: tests raise `shown` to make new bars 'close'."""

    def __init__(self, bars, shown):
        self.bars, self.shown = bars, shown

    def poll(self):
        return self.bars.iloc[:self.shown]


class FakeModel:
    """Answers from a list (last one repeats) or a function of the snapshot. Records every call."""

    def __init__(self, answers=None):
        self.answers, self.calls, self.timeouts = answers or [WAIT], [], []

    def decide(self, snapshot, timeout_s):
        self.calls.append(snapshot)
        self.timeouts.append(timeout_s)
        a = self.answers(snapshot) if callable(self.answers) else self.answers[min(len(self.calls), len(self.answers)) - 1]
        if isinstance(a, Exception):
            raise a
        return a


WAIT = {"action": "wait", "size_r": 0, "stop": None, "target": None, "reason": "nothing to do"}


def buy(size_r=1.0, stop=597.0, target=606.0):
    return {"action": "buy", "size_r": size_r, "stop": stop, "target": target, "reason": "test buy"}


def sell(size_r=0.3):
    return {"action": "sell", "size_r": size_r, "stop": None, "target": None, "reason": "test sell"}


CLOSE = {"action": "close", "size_r": 0, "stop": None, "target": None, "reason": "test close"}


class Rig:
    """A heartbeat wired to FakeIB, FakeModel and FakeFeed, in a temp folder."""

    def __init__(self, tmp_path, answers=None, *, shown=None, accounts=("DU1234567",), now=None, **kw):
        self.bars = make_bars()
        self.ib = FakeIB(accounts=accounts)
        self.ib.connect("127.0.0.1", 4002)
        self.ib.net_liquidation = 100_000.0
        self.lab_root = tmp_path / "lab"
        self.lab_root.mkdir(parents=True)
        self.risk = Risk(load_limits(LIMITS), root=self.lab_root)
        self.broker = Broker(self.ib, TAG, risk=self.risk)
        self.contract = SimpleNamespace(symbol="SPY", conId=756733)
        self.executor = Executor(self.ib, self.broker, self.contract, timeout_s=2)
        self.model = FakeModel(answers)
        self.feed = FakeFeed(self.bars, shown or 196)  # 196 = two full days + 40 bars: 12:50 on day 3
        self.ib.market_price = float(self.bars["close"].iloc[self.feed.shown - 1])
        self.state = State.load(tmp_path / "desk")
        self.journal = Journal(tmp_path / "desk")
        last = self.bars.index[self.feed.shown - 1]
        self.now = now or last + pd.Timedelta(minutes=12)  # the delayed feed shows a bar ~12 min after it closed
        self.mono = [0.0]
        self.kw = kw
        self.hb = self.make_heartbeat()

    def make_heartbeat(self):
        return Heartbeat(ib=self.ib, contract=self.contract, feed=self.feed, model=self.model, executor=self.executor,
                         broker=self.broker, risk=self.risk, journal=self.journal, state=self.state,
                         clock=lambda: self.now, mono=lambda: self.mono[0], **self.kw)

    def stop_file(self):
        (self.lab_root / "STOP").write_text("kill switch test")

    def records(self):
        import json
        return [json.loads(l) for l in self.journal.path.read_text().splitlines()]


@pytest.fixture
def rig(tmp_path):
    made = []  # a test may build several rigs: each gets its own folder

    def build(*a, **k):
        made.append(1)
        return Rig(tmp_path / f"rig{len(made)}", *a, **k)

    return build
