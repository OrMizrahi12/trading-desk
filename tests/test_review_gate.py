"""The review gate: no new entry until the last closed trade has its post-trade review (a written look back at the trade).

The gate reads the journal: a `trade_closed` record without a `trade_reviewed` record after it holds back every buy. Sells
and closes are never held back. The trader is told why, in the verdict of the refused bar and in the next snapshot.
"""

import shutil

import pandas as pd
import pytest

from conftest import CLOSE, TAG, buy, sell
from desk import execute, trades
from desk.decision import Decision
from desk.scribe import Scribe
from test_scribe import REAL_VAULT, EXIT_BAR, FakeClaude, leg, open_, trade_answer


def orders(r, kind=None):
    return [o for _, o in r.ib.placed if kind is None or o.orderType == kind]


def bar_records(r):
    return [x for x in r.records() if x.get("bar")]


def next_bar(r, minutes=5):
    r.feed.shown += 1
    r.now = r.now + pd.Timedelta(minutes=minutes)
    r.ib.market_price = float(r.bars["close"].iloc[r.feed.shown - 1])


def end_a_trade(journal, **k):
    """Journal the end of a trade by hand, as the heartbeat does when the desk is flat again."""
    journal.append(trades.closed_record({**open_(entry_bar="2026-10-21T16:00:00+00:00", **k), "exits": [leg()]},
                                        protection_fills=[], now=EXIT_BAR))


def review(journal, **k):
    journal.append({"trade_reviewed": {"key": "r1", "note": "n", "classification": "good trade good result", "lesson": None, **k},
                    "at": EXIT_BAR})


# -- the journal's answer ------------------------------------------------------------------------

def test_a_trade_is_pending_until_its_review_is_journaled(rig):
    j = rig().journal
    assert j.pending_review() is None and execute.review_block(j) is None  # no trade yet: nothing to review
    end_a_trade(j)
    assert j.pending_review()["ref"] == "r1" and "post-trade review" in execute.review_block(j)
    review(j)
    assert j.pending_review() is None and execute.review_block(j) is None


def test_only_the_last_closed_trade_holds_entries_and_a_review_of_another_trade_does_not_open_the_gate(rig):
    j = rig().journal
    end_a_trade(j, ref="old")
    review(j, key="old")
    end_a_trade(j, ref="new")
    assert j.pending_review()["ref"] == "new"
    review(j, key="old")  # again: still not the new trade's
    assert j.pending_review()["ref"] == "new"
    review(j, key="new")
    assert j.pending_review() is None


def test_a_trade_without_a_ref_is_keyed_by_its_entry_bar(rig):
    j = rig().journal
    end_a_trade(j, ref=None)
    review(j, key="2026-10-21T16:00:00+00:00")
    assert j.pending_review() is None


# -- the heartbeat refuses the buy and says why ----------------------------------------------------

def test_a_buy_is_refused_while_the_last_trade_has_no_review_and_the_trader_is_told_why(rig):
    r = rig(answers=[buy()])
    end_a_trade(r.journal)
    r.hb.tick()
    rec = r.records()[-1]
    assert rec["verdict"]["allowed"] is False and "post-trade review" in rec["verdict"]["reason"]
    assert "result" not in rec and not orders(r)
    assert rec["seatbelts"][0]["name"] == "post_trade_review"
    gate = r.model.calls[0]["entry_review_gate"]  # told before it even answers
    assert gate["entries_open"] is False and "post-trade review" in gate["why_closed"]
    next_bar(r)
    r.hb.tick()
    seen = r.model.calls[1]  # a resumed conversation gets the refusal of the last bar in recent_journal
    assert any("post-trade review" in j["verdict"]["reason"] for j in seen["recent_journal"] if j.get("verdict"))


def test_the_gate_is_open_in_the_snapshot_when_nothing_is_pending(rig):
    r = rig()
    r.hb.tick()
    assert r.model.calls[0]["entry_review_gate"] == {"entries_open": True, "why_closed": None}


def test_the_buy_goes_through_once_the_review_is_journaled(rig):
    r = rig(answers=[buy()])
    end_a_trade(r.journal)
    review(r.journal)
    r.hb.tick()
    assert r.records()[-1]["result"]["status"] == "bought" and orders(r)


def test_a_stale_bar_is_still_refused_for_being_stale_not_for_the_review(rig):
    r = rig(answers=[buy()])
    end_a_trade(r.journal)
    r.now = r.now + pd.Timedelta(minutes=40)
    r.hb.tick()
    assert "old" in r.records()[-1]["verdict"]["reason"]


def test_selling_and_closing_are_never_held_back(rig):
    r = rig(answers=[buy(size_r=2.0, stop=599.0, target=610.0), sell(0.02), CLOSE])
    r.hb.tick()
    end_a_trade(r.journal, ref="r0")  # a trade ended earlier and is still unreviewed while this long is open
    next_bar(r)
    r.hb.tick()
    assert bar_records(r)[-1]["result"]["status"] == "sold"
    next_bar(r)
    r.hb.tick()
    assert bar_records(r)[-1]["result"]["status"] == "sold" and r.ib.holdings["SPY"] == 0


def test_a_trade_that_ended_but_is_not_journaled_yet_also_holds_the_entry(rig):
    r = rig(answers=[buy()])
    r.state.open_trade = open_()  # the stop filled after the poll began: the desk is flat, `trade_closed` not written yet
    closed = r.feed.poll()
    r.hb.process_bar(closed, closed.index[-1])  # (tick would journal the end first, via _close_out)
    rec = r.records()[0]
    assert rec["verdict"]["allowed"] is False and "not journaled yet" in rec["verdict"]["reason"] and not orders(r)


def test_the_executor_refuses_a_buy_on_its_own_even_if_a_gate_let_it_through(rig):
    r = rig()
    end_a_trade(r.journal)
    px = float(r.bars["close"].iloc[195])
    buy_it = Decision("buy", 0.5, px - 3, px + 6, "x")
    send = lambda: execute.run(buy_it, 5, executor=r.executor, ib=r.ib, tag=TAG, symbol="SPY", bar_ts=r.bars.index[195],  # noqa: E731
                               ref_price=px, state=r.state, journal=r.journal)
    with pytest.raises(execute.ReviewPending, match="post-trade review"):
        send()
    assert not orders(r)
    review(r.journal)
    out = send()
    assert out["status"] == "bought"


def test_the_executor_never_holds_back_a_close(rig):
    r = rig(answers=[buy()])
    r.hb.tick()
    end_a_trade(r.journal)
    out = execute.run(Decision("close", 0, None, None, "x"), 0, executor=r.executor, ib=r.ib, tag=TAG, symbol="SPY",
                      bar_ts=r.bars.index[195], ref_price=float(r.bars["close"].iloc[195]), state=r.state, journal=r.journal)
    assert out["status"] == "sold"


# -- end to end: the trade ends, Claude is woken, the review opens the gate ------------------------------

@pytest.fixture
def vault(tmp_path):
    path = tmp_path / "vault"
    shutil.copytree(REAL_VAULT, path, ignore=shutil.ignore_patterns(".obsidian", "Daily", "Trades", "Weekly"))
    (path / "Trades").mkdir()
    return path


def test_after_a_closed_trade_the_review_is_written_and_only_then_the_desk_may_enter_again(rig, vault):
    r = rig(answers=[buy(), CLOSE, buy()])
    fake = FakeClaude(trade_answer)
    r.hb.scribe = Scribe(r.journal, vault=vault, run=fake, background=False)
    r.hb.tick()
    next_bar(r)
    r.hb.tick()  # the close: the trade ends, the scribe is woken at the end of the tick and reviews it at once
    [t] = r.journal.closed_trades()
    review_call = [f for f in fake.facts if f["kind"] == "review"][0]
    assert review_call["bars_since_entry"], "the review is woken with the bars since the entry"
    assert review_call["bars_since_entry"][0]["start_ny"] == f"{pd.Timestamp(t['entry_bar']).tz_convert('America/New_York'):%H:%M}"
    assert r.journal.pending_review() is None
    assert (vault / "Lessons" / "Close near the target.md").exists()
    next_bar(r)
    r.hb.tick()
    assert r.records()[-1]["result"]["status"] == "bought"


def test_without_a_review_the_desk_stays_out_and_says_so_every_bar(rig):
    r = rig(answers=[buy(), CLOSE, buy()])  # no scribe wired: nobody writes the review
    r.hb.tick()
    next_bar(r)
    r.hb.tick()
    for _ in range(2):
        next_bar(r)
        r.hb.tick()
        rec = r.records()[-1]
        assert rec["verdict"]["allowed"] is False and "post-trade review" in rec["verdict"]["reason"]
    assert len([o for o in orders(r) if o.action == "BUY"]) == 1 and r.ib.holdings.get("SPY", 0) == 0  # only the first buy
