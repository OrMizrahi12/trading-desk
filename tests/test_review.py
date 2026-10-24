"""The 50-trade review: blocks, groups, honest uncertainty, and when a note may be written."""

import json
import random
import subprocess

import pytest

from desk import lesson_demo, review, trades
from desk.journal import Journal
from desk.scribe import note_name


def make(n, seed=3, **kw):
    rng = random.Random(seed)
    out = []
    for i in range(n):
        day = f"2026-08-{1 + i // 3:02d}" if i < 90 else f"2026-09-{1 + i // 3 - 30:02d}"
        hour = (14, 16, 18)[i % 3]  # UTC: 10:xx, 12:xx, 14:xx New York
        out.append({"ref": f"t{i}", "entry_bar": f"{day}T{hour}:05:00+00:00", "day": day, "result_r": round(rng.gauss(0.0, 1.0), 2),
                    "regime": ("up", "down")[i % 2], "lessons": [], **kw})
    return out


def test_time_buckets_use_new_york_time():
    assert review.time_bucket("2026-08-03T13:50:00+00:00") == "morning (before 11:00)"  # 09:50 in summer
    assert review.time_bucket("2026-12-03T16:00:00+00:00") == "midday (11:00 to 13:00)"  # 11:00 in winter
    assert review.time_bucket("2026-08-03T17:00:00+00:00").startswith("afternoon")


def test_wilson_range_brackets_the_share_and_is_wide_for_few_trades():
    lo, hi = review.wilson(5, 10)
    assert lo < 0.5 < hi and hi - lo > 0.5
    assert review.wilson(0, 0) is None and review.wilson(10, 10)[1] == 1.0


def test_small_groups_are_never_judged_and_a_clear_gap_is_flagged():
    ts = make(40)
    for t in ts[:12]:
        t["result_r"] = round(abs(t["result_r"]) + 2.0, 2)  # a group that is truly better
    setups = {t["ref"]: ("good" if i < 12 else "other" if i < 38 else "tiny") for i, t in enumerate(ts)}
    groups = {g["label"]: g for g in review.breakdown(ts, setups)["dimensions"]["Setup"]}
    assert groups["good"]["reading"] == "clearly better than the rest"
    assert groups["tiny"]["n"] == 2 and groups["tiny"]["reading"].startswith("too few to say") and groups["tiny"]["diff"] is None
    assert groups["good"]["mean_interval"][0] < groups["good"]["mean"] < groups["good"]["mean_interval"][1]


def test_pure_noise_is_mostly_not_flagged_and_results_are_reproducible():
    ts = make(50)
    setups = {t["ref"]: ("a", "b")[i % 2] for i, t in enumerate(ts)}
    one, two = review.breakdown(ts, setups), review.breakdown(ts, setups)
    assert one == two
    assert all(g["reading"] == "no clear difference" for g in one["dimensions"]["Setup"])


def test_trades_without_a_result_or_regime_are_counted_not_guessed():
    ts = make(20)
    ts[0]["result_r"] = None
    for t in ts[1:6]:
        t.pop("regime")
    out = review.breakdown(ts, {})
    assert (out["scored"], out["unscored"]) == (19, 1)
    assert {g["label"] for g in out["dimensions"]["Market regime (15-minute trend at entry)"]} == {"up", "down", "not recorded"}
    assert [g["label"] for g in out["dimensions"]["Setup"]] == ["unknown"]


def test_the_regime_is_computed_from_the_fifteen_minute_bars():
    bar = lambda c, f, s: {"bars_15min": [{"close": c, "ema9": f, "ema21": s}]}
    assert trades.regime(bar(101, 100.5, 100)) == "up" and trades.regime(bar(99, 99.5, 100)) == "down"
    assert trades.regime(bar(101, 99, 100)) == "mixed" and trades.regime({}) == "not recorded"
    assert trades.regime({"bars_15min": [{"close": 1, "ema9": None, "ema21": 1}]}) == "not recorded"
    t = trades.open_trade({"bar": "2026-08-03T14:00:00+00:00"}, {"price": 1, "shares": 1}, {"stop": 0.5}, "up")
    assert trades.closed_record(t, protection_fills=[], now="n")["trade_closed"]["regime"] == "up"


def write_journal(desk, ts):
    desk.mkdir(parents=True, exist_ok=True)
    (desk / "journal.jsonl").write_text("".join(json.dumps({"trade_closed": t, "at": t["entry_bar"]}) + "\n" for t in ts))


def note(vault, t, setup="vwap-reclaim"):
    (vault / "Trades").mkdir(parents=True, exist_ok=True)
    (vault / "Trades" / f"{note_name(t)}.md").write_text(f"---\ntype: trade\nsetup: {setup}\n---\n")


def test_a_review_is_due_every_fifty_trades_written_once_and_waits_for_trade_notes(tmp_path):
    desk, vault = tmp_path / "desk", tmp_path / "vault"
    ts = make(120)
    write_journal(desk, ts[:49])
    assert review.due_blocks(Journal(desk).closed_trades(), vault) == []
    write_journal(desk, ts[:105])
    assert review.due_blocks(Journal(desk).closed_trades(), vault) == [50, 100]
    assert review.write_due(Journal(desk), vault) == []  # no trade notes yet: the setup would be wrong
    for t in ts[:50]:
        note(vault, t)
    (first,) = review.write_due(Journal(desk), vault)
    text = first.read_text()
    assert first.name == "Review after 50 trades.md" and "trades: 50" in text and "## Setup" in text and "seeded" not in text
    first.write_text("edited by hand")
    assert review.write_due(Journal(desk), vault) == [] and first.read_text() == "edited by hand"  # never overwritten
    for t in ts[50:100]:
        note(vault, t)
    (second,) = review.write_due(Journal(desk), vault)
    assert second.name == "Review after 100 trades.md" and "last_day: " + ts[99]["day"] in second.read_text()


def test_the_review_covers_exactly_the_first_n_trades(tmp_path):
    desk, vault = tmp_path / "desk", tmp_path / "vault"
    ts = make(60)
    for t in ts[50:]:
        t["result_r"] = 99.0
    write_journal(desk, ts)
    for t in ts:
        note(vault, t)
    (path,) = review.write_due(Journal(desk), vault)
    assert "99.00" not in path.read_text() and "+99" not in path.read_text()


def test_seeded_reviews_are_labelled_and_never_written_to_the_real_vault(tmp_path):
    desk, vault = tmp_path / "desk", tmp_path / "vault"
    ts = make(50, seeded=True)
    write_journal(desk, ts)
    for t in ts:
        note(vault, t)
    (path,) = review.write_due(Journal(desk), vault)
    assert "seeded: true" in path.read_text() and "DEMO (seeded, made-up data)" in path.read_text()
    with pytest.raises(SystemExit):
        review.write_due(Journal(desk), review.VAULT)
    write_journal(desk, make(25, seeded=True) + make(25, seed=9))
    (path).unlink()
    with pytest.raises(SystemExit):
        review.write_due(Journal(desk), vault)


def test_the_demo_vault_gets_one_labelled_review_and_the_lesson_story_is_unchanged(tmp_path):
    desk, vault = lesson_demo.build(tmp_path / "demo")
    (path,) = review.write_due(Journal(desk), vault)
    text = path.read_text()
    assert path.parent.name == "Reviews" and "DEMO (seeded, made-up data)" in text and "opening-range-breakout" in text
    assert "## Market regime" in text and "## Time of day" in text and "afternoon (from 13:00)" in text
