"""Run the heartbeat on the paper Gateway: `uv run -m desk [--once] [--dry-run]`. Paper trading only."""

import argparse
import logging

from . import lab  # noqa: F401
from ib_async import IB, Stock
from live.broker import Broker, connect_paper
from live.config import JOURNAL_PATH
from live.execution import Executor
from live.feed import BarFeed
from live.journal import Journal as LabJournal
from live.risk import Risk

from . import limits_pin
from .heartbeat import Heartbeat
from .journal import Journal, State
from .model import ClaudeModel
from .snapshot import fetch_ib_daily

CLIENT_ID = 8  # 1-7 are taken in the lab (see its AGENTS.md)
SYMBOL = "SPY"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="poll once, handle the new bars, exit")
    parser.add_argument("--dry-run", action="store_true", help="ask the model and gate, but send no orders")
    parser.add_argument("--model", default=None, help="Claude model for claude -p (default: the CLI's default)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    ib = connect_paper(IB, client_id=CLIENT_ID)  # refuses non-paper port, host or account
    try:
        contract = Stock(SYMBOL, "SMART", "USD")
        ib.qualifyContracts(contract)
        risk = Risk.from_file()
        broker = Broker(ib, f"live:desk:{SYMBOL}", dry_run=args.dry_run, risk=risk)
        Heartbeat(ib=ib, contract=contract, feed=BarFeed(ib, contract), model=ClaudeModel(model=args.model),
                  executor=Executor(ib, broker, contract), broker=broker, risk=risk, journal=Journal(),
                  state=State.load(), symbol=SYMBOL, fetch_daily=lambda: fetch_ib_daily(ib, contract),
                  lab_journal=LabJournal(JOURNAL_PATH), limits_check=limits_pin.verify).run(once=args.once)
    finally:
        ib.disconnect()


if __name__ == "__main__":
    main()
