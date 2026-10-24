"""The ORB veto: the lab strategy proposes, Claude can only approve or veto. Fake claude, fake IB, no network."""

import json
import subprocess
from types import SimpleNamespace

import pytest

from conftest import WAIT
from desk import orb_veto
from desk.model import ModelFailure, Session
from desk.orb_veto import OrbVetoModel, Signal, Vetoer, check_verdict, signal

SIGNAL_SHOWN = 161  # make_bars: the first ORB break of day 3 closes bar 160 (09:50 New York) at 603.19, range low 602.98


def rows_of(rig_obj, shown):
    df = rig_obj.bars.iloc[:shown]
    ny = df.index.tz_convert("America/New_York")
    return [{"start": ny[i].isoformat(), **{c: float(df[c].iloc[i]) for c in ("open", "high", "low", "close", "volume")}}
            for i in range(len(df))]


class FakeVetoer:
    def __init__(self, answer=None, failure=None):
        self.answer, self.failure, self.calls, self.role = answer, failure, [], orb_veto.ROLE

    def ask(self, snapshot, sig, timeout_s):
        self.calls.append((snapshot, sig))
        return {"answer": self.answer, "failure": self.failure, "cost_usd": 0.01, "elapsed_s": 0.1}


def snap_at(rig, shown=SIGNAL_SHOWN, **extra):
    return {"bars_5min": rows_of(rig, shown), "positions": [], "open_orders": [], **extra}


def approve(reason="trend and volume agree"):
    return {"verdict": "approve", "reason": reason}


def veto(reason="the bar is 40 minutes old"):
    return {"verdict": "veto", "reason": reason}


# -- the signal is the lab strategy's, unchanged ------------------------------------------------

def test_signal_comes_from_the_lab_strategy_with_its_own_stop(rig):
    r = rig(shown=SIGNAL_SHOWN)
    s = signal(snap_at(r))
    assert s and s.reason == "opening_range_breakout" and s.stop == pytest.approx(602.98) and s.entry == pytest.approx(603.19)
    assert s.range_high < s.entry
    assert signal(snap_at(r, shown=SIGNAL_SHOWN - 1)) is None  # the bar before the break: no signal
    assert signal({"bars_5min": []}) is None


def test_the_strategy_is_the_labs_class_with_default_settings():
    from strategies.opening_range_breakout import OpeningRangeBreakout, OpeningRangeBreakoutParams
    m = OrbVetoModel(FakeVetoer())
    assert type(m.strategy) is OpeningRangeBreakout and m.strategy.p == OpeningRangeBreakoutParams()


# -- the model: Claude can only approve or veto ----------------------------------------------------

def test_no_signal_is_a_wait_and_claude_is_not_asked(rig):
    v = FakeVetoer(approve())
    out = OrbVetoModel(v).decide(snap_at(rig(shown=SIGNAL_SHOWN - 1), shown=SIGNAL_SHOWN - 1), 60)
    assert out["action"] == "wait" and not v.calls


def test_approve_becomes_the_strategys_buy_with_fixed_size_and_no_target(rig):
    v = FakeVetoer(approve())
    out = OrbVetoModel(v).decide(snap_at(rig(shown=SIGNAL_SHOWN)), 60)
    assert (out["action"], out["size_r"], out["stop"], out["target"]) == ("buy", orb_veto.SIZE_R, pytest.approx(602.98), None)
    assert out["lessons"] == [] and "approved" in out["reason"] and len(v.calls) == 1
    assert v.calls[0][1].stop == pytest.approx(602.98)


@pytest.mark.parametrize("answer, failure", [(veto(), None), (None, "timeout: no answer within 45s"),
                                             (None, "invalid: keys must be exactly")])
def test_veto_or_no_valid_answer_is_a_wait(rig, answer, failure):
    out = OrbVetoModel(FakeVetoer(answer, failure)).decide(snap_at(rig(shown=SIGNAL_SHOWN)), 60)
    assert out["action"] == "wait" and out["stop"] is None and out["size_r"] == 0


def test_a_veto_cannot_change_size_stop_or_target():
    for extra in ({"verdict": "approve", "reason": "x", "size_r": 1, "stop": 1.0}, {"verdict": "approve"},
                  {"verdict": "maybe", "reason": "x"}, {"verdict": "veto", "reason": " "}, {"verdict": "veto", "reason": "x" * 281},
                  "buy"):
        with pytest.raises(orb_veto.InvalidVerdict):
            check_verdict(extra)
    assert check_verdict({"verdict": "veto", "reason": " too thin "}) == {"verdict": "veto", "reason": "too thin"}


@pytest.mark.parametrize("held", [{"positions": [{"symbol": "SPY", "position": 5}]}, {"open_orders": [{"orderId": 1}]}])
def test_a_held_position_or_open_order_is_never_sent_to_claude_and_never_acted_on(rig, held):
    v = FakeVetoer(approve())
    out = OrbVetoModel(v).decide(snap_at(rig(shown=SIGNAL_SHOWN), **held), 60)
    assert out["action"] == "wait" and not v.calls  # claude cannot close or sell: the stop and the flatten manage it


def test_the_veto_is_given_the_full_snapshot_plus_the_signal_and_the_record_is_kept(rig):
    r = rig(shown=SIGNAL_SHOWN)
    full, delta = snap_at(r, marker="full"), {"snapshot_kind": "delta"}
    v, session = FakeVetoer(approve()), Session("s", full=full)
    OrbVetoModel(v).decide(delta, 60, session)
    assert v.calls[0][0]["marker"] == "full"
    assert session.orb["signal"]["stop"] == pytest.approx(602.98) and session.orb["final"] == "buy"
    assert session.instructions == orb_veto.prompt_hash()


# -- the real command (fake subprocess) ----------------------------------------------------------

def fake_run(stdout, code=0, seen=None):
    def run(cmd, **kw):
        if seen is not None:
            seen.append((cmd, kw))
        return SimpleNamespace(returncode=code, stdout=stdout, stderr="boom")
    return run


SIG = Signal(entry=603.19, stop=602.98, range_high=603.0, reason="opening_range_breakout")


def test_vetoer_sends_no_tools_a_strict_schema_and_the_signal_and_validates_the_answer():
    seen = []
    out = json.dumps({"structured_output": veto(), "total_cost_usd": 0.02})
    rec = Vetoer(run=fake_run(out, seen=seen)).ask({"bars_5min": []}, SIG, 30)
    cmd, kw = seen[0]
    assert rec["answer"] == veto() and rec["cost_usd"] == 0.02
    assert cmd[cmd.index("--tools") + 1] == "" and "--no-session-persistence" in cmd
    assert json.loads(kw["input"])["orb_signal"]["stop"] == 602.98
    assert json.loads(cmd[cmd.index("--json-schema") + 1])["properties"]["verdict"]["enum"] == ["approve", "veto"]


@pytest.mark.parametrize("run, why", [
    (fake_run("", code=1), "exit_code"),
    (fake_run("not json"), "bad_output"),
    (fake_run(json.dumps({"result": "hi"})), "no structured_output"),
    (fake_run(json.dumps({"structured_output": {"verdict": "approve"}})), "invalid"),
    (fake_run(json.dumps({"is_error": True, "result": "x"})), "error"),
])
def test_vetoer_failures_give_no_answer(run, why):
    rec = Vetoer(run=run).ask({}, SIG, 30)
    assert rec["answer"] is None and why in rec["failure"]


def test_vetoer_timeout_and_missing_binary_give_no_answer():
    def slow(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 1)
    assert "timeout" in Vetoer(run=slow).ask({}, SIG, 30)["failure"]
    assert "not_started" in Vetoer(run=lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("claude"))).ask({}, SIG, 30)["failure"]
    assert "timeout" in Vetoer(run=slow).ask({}, SIG, 0)["failure"]


# -- the heartbeat -----------------------------------------------------------------------------

def test_heartbeat_approved_signal_buys_through_every_desk_gate_and_journals_what_claude_said(rig):
    r = rig(shown=SIGNAL_SHOWN)
    r.hb.model = OrbVetoModel(FakeVetoer(approve("daily and 15-minute trends agree")))
    r.hb.tick()
    rec = r.records()[0]
    assert rec["decision"]["action"] == "buy" and rec["decision"]["target"] is None
    assert rec["orb_veto"]["view"]["answer"]["reason"] == "daily and 15-minute trends agree" and rec["orb_veto"]["final"] == "buy"
    assert rec["instructions"] == orb_veto.prompt_hash()


def test_heartbeat_vetoed_signal_sends_no_order(rig):
    r = rig(shown=SIGNAL_SHOWN)
    r.hb.model = OrbVetoModel(FakeVetoer(veto()))
    r.hb.tick()
    rec = r.records()[0]
    assert rec["decision"]["action"] == "wait" and not r.ib.placed and "vetoed" in rec["decision"]["reason"]
    assert rec["orb_veto"]["final"] == "wait"


def test_heartbeat_a_bar_without_a_signal_costs_nothing_and_keeps_the_usual_journal(rig):
    r = rig(shown=196)
    v = FakeVetoer(approve())
    r.hb.model = OrbVetoModel(v)
    r.hb.tick()
    assert not v.calls and r.records()[0]["decision"]["action"] == "wait" and "orb_veto" not in r.records()[0]


@pytest.mark.parametrize("other", ["--team", "--regime-gate"])
def test_orb_veto_cannot_be_combined_with_the_other_switches(monkeypatch, other):
    from desk import __main__ as entry
    monkeypatch.setattr("sys.argv", ["desk", "--orb-veto", other])
    with pytest.raises(SystemExit) as e:
        entry.main()  # refused while parsing, before any connection
    assert e.value.code == 2
