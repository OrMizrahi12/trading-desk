"""A FakeIB that also serves history and quotes, and a rig that wires the MCP server to it. No network, no real IB."""

import contextlib
from types import SimpleNamespace

import pandas as pd
import pytest
from ib_async import BarData

from conftest import LIMITS, FakeFeed, make_bars
from fake_ib import FakeIB
from live.risk import Risk, load_limits
from mcp import Client

from desk.mcp_server.core import Desk
from desk.mcp_server.ibworker import IBWorker
from desk.mcp_server.server import create_server


class McpFakeIB(FakeIB):
    """FakeIB plus what the read tools ask for: reqHistoricalData / reqMktData / cancelMktData."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.history = {}  # bar size -> list of BarData
        self.history_requests = []
        self.history_errors = []  # (code, text) emitted on the next history request
        self.ticker = SimpleNamespace(bid=float("nan"), ask=float("nan"), last=float("nan"), close=float("nan"),
                                      volume=float("nan"), time=None)
        self.mkt_cancelled = []

    def qualifyContracts(self, *contracts):
        for c in contracts:
            c.conId = 756733
        return list(contracts)

    def reqHistoricalData(self, contract, endDateTime, durationStr, barSizeSetting, whatToShow, useRTH, formatDate=1, **kw):
        self.history_requests.append((contract.symbol, durationStr, barSizeSetting, whatToShow, useRTH, endDateTime))
        for code, text in self.history_errors:
            self.errorEvent.emit(1, code, text, None)
        return list(self.history.get(barSizeSetting, []))

    def reqMktData(self, contract, genericTickList="", snapshot=False, regulatorySnapshot=False):
        return self.ticker

    def cancelMktData(self, contract):
        self.mkt_cancelled.append(contract.symbol)


def to_bar_data(df: pd.DataFrame) -> list[BarData]:
    return [BarData(date=ts.to_pydatetime(), open=r.open, high=r.high, low=r.low, close=r.close, volume=r.volume,
                    average=r.close, barCount=10) for ts, r in df.iterrows()]


class McpRig:
    """Desk + server + FakeIB in a temp folder. `shown` = how many of the 3 days' 5-minute bars IB has closed."""

    def __init__(self, tmp_path, *, shown=196, accounts=("DU1234567",), read_only=False, limits=LIMITS, minutes_after_bar=12):
        self.bars = make_bars()
        self.ib = McpFakeIB(accounts=accounts)
        self.ib.connect("127.0.0.1", 4002, clientId=20)
        self.ib.net_liquidation = 100_000.0
        self.lab_root = tmp_path / "lab"
        self.lab_root.mkdir(parents=True)
        self.risk = Risk(load_limits(limits), root=self.lab_root)
        self.now = self.bars.index[shown - 1] + pd.Timedelta(minutes=5 + minutes_after_bar)  # IB shows a bar ~12 min after it closed
        self.ib.market_price = float(self.bars["close"].iloc[shown - 1])
        self.ib.history["5 mins"] = to_bar_data(self.bars.iloc[:shown])
        self.worker = IBWorker(connect=lambda: self.ib)
        self.desk = Desk(self.worker, risk=self.risk, root=tmp_path / "mcp", clock=lambda: self.now,
                         executor_kwargs={"timeout_s": 2}, heartbeat_dir=tmp_path / "heartbeat")
        self.desk.parts(self.ib).feed = FakeFeed(self.bars, shown)  # the heartbeat's own tests use the same scripted feed
        self.mcp = create_server(self.desk, read_only=read_only, watch_s=0)
        self.ref = self.ib.market_price

    @contextlib.asynccontextmanager
    async def client(self):
        async with Client(self.mcp, raise_exceptions=True) as c:
            yield c

    def journal(self):
        import json
        path = self.desk.journal.path
        return [json.loads(l) for l in path.read_text().splitlines()] if path.exists() else []

    def stop_file(self):
        (self.lab_root / "STOP").write_text("kill switch test")

    def heartbeat_order(self, action="SELL", order_type="STP", price=590.0, shares=3):
        """An order that belongs to the heartbeat (client 8) is resting at IB."""
        from ib_async import LimitOrder, StopOrder
        order = StopOrder(action, shares, price) if order_type == "STP" else LimitOrder(action, shares, price)
        order.orderRef = "live:desk:SPY:2026-10-21:1000"
        return self.ib.placeOrder(SimpleNamespace(symbol="SPY"), order)


def data(result):
    """The dict a tool returned (structured content), or its text when it is an error."""
    import json
    sc = result.structured_content
    if sc is None:
        text = result.content[0].text
        return text if result.is_error else json.loads(text)  # a plain dict comes back as JSON text
    return sc.get("result", sc) if set(sc) == {"result"} else sc


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def mcp_rig(tmp_path):
    made = []

    def build(**kw):
        made.append(1)
        return McpRig(tmp_path / f"rig{len(made)}", **kw)

    return build
