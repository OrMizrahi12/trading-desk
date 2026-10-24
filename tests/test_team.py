"""The team (desk/team.py): roles, protocol order, the count, the veto/tie rule, and the journal. A fake `claude`, no network."""

import json
import threading
from itertools import product
from types import SimpleNamespace

import pytest

from conftest import WAIT, FakeModel, buy, sell, CLOSE
from desk import team as team_module
from desk.decision import parse
from desk.model import ModelFailure, Session
from desk.team import (ANALYST, DEVIL, JUDGES, RISK, ROLES_DIR, InvalidView, Team, TeamModel, agents_hash, check_view,
                       enforce, load_role, schema, tally)

BUY_VIEW = {"proposal": "buy", "size_r": 0.5, "stop": 597.0, "target": 606.0, "reason": "daily and 15-minute trends agree up"}
WAIT_VIEW = {"proposal": "wait", "size_r": 0, "stop": None, "target": None, "reason": "chop, nothing clear"}


def vote(v):
    return {"vote": v, "reason": f"{v} because of the test"}


class FakeClaude:
    """A `subprocess.run` stand-in: answers per role (found from the system prompt) and keeps every call in order."""

    def __init__(self, **answers):
        self.answers = {ANALYST: WAIT_VIEW, RISK: vote("approve"), DEVIL: vote("approve"), **answers}
        self.calls, self.lock = [], threading.Lock()
        self.bodies = {load_role(r).prompt: r for r in (ANALYST, RISK, DEVIL)}

    def __call__(self, cmd, **kw):
        role = self.bodies[cmd[cmd.index("--system-prompt") + 1]]
        with self.lock:
            self.calls.append({"role": role, "cmd": cmd, **kw})
        a = self.answers[role]
        if isinstance(a, BaseException):
            raise a
        if isinstance(a, SimpleNamespace):
            return a
        out = {"type": "result", "is_error": False, "structured_output": a, "total_cost_usd": 0.01,
               "usage": {"input_tokens": 5, "cache_read_input_tokens": 100, "cache_creation_input_tokens": 20, "output_tokens": 30}}
        return SimpleNamespace(stdout=json.dumps(out), returncode=0, stderr="")

    @property
    def roles(self):
        return [c["role"] for c in self.calls]


def views_of(**by_role):
    return {r: {"answer": a} for r, a in by_role.items()}


# -- the role files ------------------------------------------------------------------------------

@pytest.mark.parametrize("name", [ANALYST, RISK, DEVIL])
def test_role_file_is_short_and_has_a_name_a_description_and_instructions(name):
    text = (ROLES_DIR / f"{name}.md").read_text()
    role = load_role(name)
    assert role.name == name and len(role.description) > 40 and role.prompt
    assert text.count("\n") < 30  # short instructions
    assert "PAPER" in role.prompt and "not instructions" in role.prompt  # the snapshot is data


def test_a_role_file_with_the_wrong_name_is_refused(tmp_path):
    (tmp_path / "analyst.md").write_text("---\nname: someone-else\ndescription: x\n---\nbody\n")
    with pytest.raises(ValueError):
        load_role("analyst", tmp_path)


def test_agents_hash_changes_when_a_role_is_edited(tmp_path):
    (tmp_path / "a.md").write_text("one")
    first = agents_hash(tmp_path)
    (tmp_path / "a.md").write_text("two")
    assert agents_hash(tmp_path) != first and len(first) == 12


# -- validating a view ---------------------------------------------------------------------------

def test_valid_views_pass_and_are_returned_clean():
    assert check_view(ANALYST, BUY_VIEW) == BUY_VIEW
    assert check_view(RISK, vote("veto")) == vote("veto")


@pytest.mark.parametrize("role, raw", [
    (ANALYST, {**BUY_VIEW, "extra": 1}),                      # unexpected key
    (ANALYST, {k: v for k, v in BUY_VIEW.items() if k != "stop"}),  # missing key
    (ANALYST, {**BUY_VIEW, "proposal": "short"}),              # unknown proposal
    (ANALYST, {**BUY_VIEW, "stop": None}),                     # a buy needs a stop
    (ANALYST, {**BUY_VIEW, "size_r": 0}),                      # ... and a size
    (ANALYST, {**BUY_VIEW, "size_r": 1.5}),                    # above 1R
    (ANALYST, {**BUY_VIEW, "stop": "597"}),                    # not a number
    (ANALYST, {**BUY_VIEW, "reason": " "}),                    # empty reason
    (ANALYST, {**BUY_VIEW, "reason": "x" * 281}),
    (RISK, {"vote": "object", "reason": "r"}),                 # objecting is the devil's word
    (DEVIL, {"vote": "veto", "reason": "r"}),                  # only the risk manager can veto
    (DEVIL, {"vote": "approve"}),
    (RISK, "approve"),
])
def test_invalid_views_are_refused(role, raw):
    with pytest.raises(InvalidView):
        check_view(role, raw)


def test_schemas_list_exactly_the_keys_the_checks_expect():
    assert set(schema(ANALYST)["properties"]) == set(BUY_VIEW)
    assert schema(RISK)["properties"]["vote"]["enum"] == ["approve", "abstain", "veto"]
    assert schema(DEVIL)["properties"]["vote"]["enum"] == ["approve", "abstain", "object"]


# -- the count: veto and tie mean wait ------------------------------------------------------------

@pytest.mark.parametrize("risk, devil, go, why", [
    ("approve", "approve", True, "majority"),
    ("approve", "object", True, "majority"),       # 2 yes, 1 no: the devil's advocate is heard but outvoted
    ("approve", "abstain", True, "majority"),
    ("abstain", "approve", True, "majority"),
    ("abstain", "object", False, "tie"),           # 1 yes, 1 no
    ("abstain", "abstain", False, "only one yes, the others abstained"),
    ("veto", "approve", False, "the risk manager vetoed"),   # a veto beats two yes
    ("veto", "object", False, "the risk manager vetoed"),
    ("veto", "abstain", False, "the risk manager vetoed"),
])
def test_the_count_for_every_pair_of_votes(risk, devil, go, why):
    out = tally(views_of(**{ANALYST: BUY_VIEW, RISK: vote(risk), DEVIL: vote(devil)}))
    assert (out["go"], out["why"]) == (go, why)


def test_every_possible_vote_combination_only_goes_with_a_majority_and_no_veto():
    for r, d in product(("approve", "abstain", "veto"), ("approve", "abstain", "object")):
        out = tally(views_of(**{ANALYST: BUY_VIEW, RISK: vote(r), DEVIL: vote(d)}))
        assert out["go"] == (r != "veto" and out["yes"] >= 2 and out["yes"] > out["no"])


def test_without_a_proposed_buy_or_with_a_missing_view_the_answer_is_no():
    assert not tally(views_of(**{ANALYST: WAIT_VIEW}))["go"]
    assert tally(views_of(**{ANALYST: {**WAIT_VIEW, "proposal": "close"}}))["why"] == "the analyst proposed close, not a buy"
    assert tally({ANALYST: {"answer": None}})["why"] == "no analyst view"
    only_risk = views_of(**{ANALYST: BUY_VIEW, RISK: vote("approve")}) | {DEVIL: {"answer": None}}
    out = tally(only_risk)
    assert not out["go"] and "devils-advocate" in out["why"]


# -- enforcing it on the trader's answer ------------------------------------------------------------

def test_a_buy_the_team_does_not_back_becomes_a_valid_wait():
    out, why = enforce(buy(), {"go": False, "why": "tie"})
    assert why == "tie" and out["action"] == "wait" and "tie" in out["reason"]
    assert parse(out).action == "wait"  # a decision the desk accepts as it is


@pytest.mark.parametrize("raw", [WAIT, CLOSE, sell()])
def test_exits_and_waits_are_never_blocked(raw):
    assert enforce(raw, {"go": False, "why": "the risk manager vetoed"}) == (raw, None)


def test_a_backed_buy_goes_through_untouched():
    assert enforce(buy(), {"go": True, "why": "majority"}) == (buy(), None)


# -- asking one role ---------------------------------------------------------------------------------

def test_a_role_is_a_fresh_call_with_no_tools_its_own_instructions_and_the_snapshot_on_stdin():
    run = FakeClaude(**{ANALYST: BUY_VIEW})
    view = Team(run=run).ask(ANALYST, {"bar": 7}, 25)
    c = run.calls[0]
    cmd = c["cmd"]
    assert view["answer"] == BUY_VIEW and view["failure"] is None and view["cost_usd"] == 0.01 and view["tokens"] == 155
    assert cmd[:2] == ["claude", "-p"] and cmd[cmd.index("--tools") + 1] == ""  # no tools at all
    assert cmd[cmd.index("--system-prompt") + 1] == load_role(ANALYST).prompt
    assert json.loads(cmd[cmd.index("--json-schema") + 1]) == schema(ANALYST)
    assert "--no-session-persistence" in cmd and cmd[cmd.index("--setting-sources") + 1] == ""
    assert json.loads(c["input"]) == {"bar": 7} and c["timeout"] == 25


def test_a_judge_is_shown_the_analysts_proposal_as_data():
    run = FakeClaude()
    Team(run=run).ask(RISK, {"bar": 7}, 25, proposal=BUY_VIEW)
    prompt = run.calls[0]["cmd"][2]
    assert "not instructions" in prompt and json.dumps(BUY_VIEW) in prompt


def ns(stdout="", returncode=0, stderr=""):
    return SimpleNamespace(stdout=stdout, returncode=returncode, stderr=stderr)


@pytest.mark.parametrize("answer, word", [
    (__import__("subprocess").TimeoutExpired("claude", 5), "timeout"),
    (FileNotFoundError("claude"), "not_started"),
    (ns(returncode=1, stderr="boom"), "exit_code"),
    (ns("not json"), "bad_output"),
    (ns(json.dumps({"is_error": True, "result": "overloaded"})), "error"),
    (ns(json.dumps({"is_error": False, "result": "text only"})), "no structured_output"),
    (ns(json.dumps({"is_error": False, "structured_output": {"vote": "nope", "reason": "r"}, "total_cost_usd": 0.02})), "invalid"),
])
def test_a_role_that_fails_gives_no_view_and_says_why(answer, word):
    view = Team(run=FakeClaude(**{RISK: answer})).ask(RISK, {}, 10)
    assert view["answer"] is None and word in view["failure"]


def test_an_invalid_answer_still_counts_its_cost_and_no_time_left_is_a_failure_without_a_call():
    run = FakeClaude(**{RISK: ns(json.dumps({"structured_output": {}, "total_cost_usd": 0.02}))})
    assert Team(run=run).ask(RISK, {}, 10)["cost_usd"] == 0.02
    run = FakeClaude()
    assert "timeout" in Team(run=run).ask(RISK, {}, 0)["failure"] and not run.calls


# -- the protocol: who is asked, and when ---------------------------------------------------------------

def test_the_analyst_is_asked_first_and_alone_when_it_does_not_propose_a_buy():
    run = FakeClaude()
    rec = Team(run=run).consult({"bar": 1}, 55)
    assert run.roles == [ANALYST] and set(rec["views"]) == {ANALYST}
    assert not rec["tally"]["go"] and rec["cost_usd"] == 0.01


def test_a_proposed_buy_brings_in_the_other_two_after_the_analyst_and_everyone_gets_the_full_snapshot():
    run = FakeClaude(**{ANALYST: BUY_VIEW})
    rec = Team(run=run).consult({"bar": 1}, 55)
    assert run.roles[0] == ANALYST and sorted(run.roles[1:]) == sorted(JUDGES)
    assert rec["tally"] == {"go": True, "yes": 3, "no": 0, "why": "majority"}
    assert all(json.loads(c["input"]) == {"bar": 1} for c in run.calls)
    assert rec["cost_usd"] == 0.03 and rec["tokens"] == 465


@pytest.mark.parametrize("proposal", ["sell", "close"])
def test_a_proposed_exit_does_not_bring_in_the_judges(proposal):
    run = FakeClaude(**{ANALYST: {**WAIT_VIEW, "proposal": proposal}})
    Team(run=run).consult({}, 55)
    assert run.roles == [ANALYST]


def test_a_failed_analyst_means_the_judges_are_not_asked_and_there_is_no_go():
    run = FakeClaude(**{ANALYST: ns(returncode=1, stderr="x")})
    rec = Team(run=run).consult({}, 55)
    assert run.roles == [ANALYST] and rec["tally"]["why"] == "no analyst view"


def test_a_failed_judge_is_not_a_vote_so_the_buy_is_not_backed():
    rec = Team(run=FakeClaude(**{ANALYST: BUY_VIEW, DEVIL: ns("junk")})).consult({}, 55)
    assert not rec["tally"]["go"] and "devils-advocate" in rec["tally"]["why"]


def test_each_role_is_given_at_most_the_roles_cap_of_the_budget():
    run = FakeClaude(**{ANALYST: BUY_VIEW})
    Team(run=run).consult({}, 55)
    assert [c["timeout"] for c in run.calls] == [team_module.ROLE_TIMEOUT_S] * 3
    run = FakeClaude()
    Team(run=run, mono=lambda: 0.0).consult({}, 12)  # a frozen clock: all 12 s are left for the analyst
    assert run.calls[0]["timeout"] == 12


# -- the trader behind the team ----------------------------------------------------------------------------

def trader_with(*answers):
    return FakeModel(list(answers))


def test_the_trader_sees_the_views_the_count_and_the_rule_inside_the_snapshot():
    trader = trader_with(WAIT)
    TeamModel(trader, Team(run=FakeClaude(**{ANALYST: BUY_VIEW, DEVIL: vote("object")}))).decide({"bar": 1}, 75, Session("s"))
    seen = trader.calls[0]
    assert seen["bar"] == 1 and seen["team"]["views"][DEVIL]["vote"] == "object"
    assert seen["team"]["tally"]["yes"] == 3 - 1 and "tie means wait" in seen["team"]["desk_rule"]


def test_the_roles_get_the_full_snapshot_even_when_the_trader_only_gets_a_delta():
    run = FakeClaude()
    trader = trader_with(WAIT)
    s = Session("s", resume=True)
    s.full = {"bar": 1, "everything": [1, 2, 3]}
    TeamModel(trader, Team(run=run)).decide({"snapshot_kind": "delta"}, 75, s)
    assert json.loads(run.calls[0]["input"]) == s.full and trader.calls[0]["snapshot_kind"] == "delta"


def test_a_veto_turns_the_traders_buy_into_wait_and_everything_is_recorded():
    s = Session("s")
    out = TeamModel(trader_with(buy()), Team(run=FakeClaude(**{ANALYST: BUY_VIEW, RISK: vote("veto")}))).decide({}, 75, s)
    assert out["action"] == "wait"
    t = s.team
    assert t["overridden"] == "the risk manager vetoed" and t["trader_answer"] == buy() and t["final"] == out
    assert set(t["views"]) == {ANALYST, RISK, DEVIL} and t["views"][RISK]["answer"]["vote"] == "veto"


def test_a_tie_turns_the_buy_into_wait():
    s = Session("s")
    out = TeamModel(trader_with(buy()), Team(run=FakeClaude(**{ANALYST: BUY_VIEW, RISK: vote("abstain"), DEVIL: vote("object")}))).decide({}, 75, s)
    assert out["action"] == "wait" and s.team["overridden"] == "tie"


def test_a_backed_buy_reaches_the_desk_unchanged():
    s = Session("s")
    out = TeamModel(trader_with(buy()), Team(run=FakeClaude(**{ANALYST: BUY_VIEW}))).decide({}, 75, s)
    assert out == buy() and s.team["overridden"] is None and s.team["final"] == buy()


def test_a_buy_the_analyst_never_proposed_is_turned_into_wait():
    s = Session("s")
    out = TeamModel(trader_with(buy()), Team(run=FakeClaude())).decide({}, 75, s)
    assert out["action"] == "wait" and "not a buy" in s.team["overridden"]


@pytest.mark.parametrize("raw", [CLOSE, sell(), WAIT])
def test_exits_pass_whatever_the_team_said(raw):
    s = Session("s")
    out = TeamModel(trader_with(raw), Team(run=FakeClaude(**{ANALYST: ns(returncode=1)}))).decide({}, 75, s)
    assert out == raw and s.team["overridden"] is None


def test_a_failing_trader_still_leaves_the_views_and_raises_for_the_heartbeat_to_make_a_wait():
    s = Session("s")
    with pytest.raises(ModelFailure):
        TeamModel(trader_with(ModelFailure("timeout")), Team(run=FakeClaude())).decide({}, 75, s)
    assert s.team["views"][ANALYST]["answer"] == WAIT_VIEW and "elapsed_s" in s.team


def test_the_trader_gets_what_is_left_of_the_time_after_the_team():
    now = [0.0]

    class Slow(FakeClaude):
        def __call__(self, cmd, **kw):
            now[0] += 30  # the analyst takes 30 s
            return super().__call__(cmd, **kw)

    trader = trader_with(WAIT)
    TeamModel(trader, Team(run=Slow(), mono=lambda: now[0]), mono=lambda: now[0]).decide({}, 75, Session("s"))
    assert trader.timeouts[0] == 45


# -- in the heartbeat: the journal ---------------------------------------------------------------------------

def orders(r):
    return [o for _, o in r.ib.placed]  # every order the fake IB was sent


def team_rig(rig, trader_answer, **roles):
    r = rig()
    trader = trader_with(trader_answer)
    r.model = r.hb.model = TeamModel(trader, Team(run=FakeClaude(**roles)))
    r.trader = trader
    return r


def test_a_backed_buy_is_executed_and_every_view_and_the_final_decision_are_journaled(rig):
    r = team_rig(rig, buy(), **{ANALYST: BUY_VIEW, DEVIL: vote("object")})
    r.hb.tick()
    rec = r.records()[0]
    team = rec["team"]
    assert rec["result"]["status"] == "bought" and orders(r)
    assert set(team["views"]) == {ANALYST, RISK, DEVIL}
    assert team["views"][DEVIL]["answer"]["vote"] == "object" and team["views"][ANALYST]["answer"]["proposal"] == "buy"
    assert team["tally"] == {"go": True, "yes": 2, "no": 1, "why": "majority"}
    assert team["final"] == rec["raw_answer"] and rec["decision"]["action"] == "buy"
    assert team["agents"] == agents_hash() and team["cost_usd"] == 0.03


def test_a_vetoed_buy_sends_no_order_and_the_journal_says_who_stopped_it(rig):
    r = team_rig(rig, buy(), **{ANALYST: BUY_VIEW, RISK: vote("veto")})
    r.hb.tick()
    rec = r.records()[0]
    assert rec["decision"]["action"] == "wait" and "result" not in rec and not orders(r)
    assert rec["team"]["overridden"] == "the risk manager vetoed" and rec["team"]["trader_answer"]["action"] == "buy"
    assert rec["team"]["views"][RISK]["answer"]["vote"] == "veto"


def test_a_quiet_bar_costs_the_analyst_only_and_is_still_journaled(rig):
    r = team_rig(rig, WAIT)
    r.hb.tick()
    team = r.records()[0]["team"]
    assert list(team["views"]) == [ANALYST] and team["final"] == WAIT and team["overridden"] is None


def test_the_teams_cost_is_added_to_the_traders_in_the_bars_context(rig):
    r = team_rig(rig, WAIT)

    class Costly(FakeModel):
        def decide(self, snapshot, timeout_s, session=None):
            session.context = {"tokens": 1000, "window": 1_000_000, "output_tokens": 50, "session_cost_usd": 0.02}
            return super().decide(snapshot, timeout_s, session)

    r.model = r.hb.model = TeamModel(Costly([WAIT]), Team(run=FakeClaude()))
    r.hb.tick()
    c = r.records()[0]["context"]
    assert (c["bar_cost_usd"], c["team_cost_usd"], c["bar_total_usd"]) == (0.02, 0.01, 0.03)


def test_without_a_team_the_journal_has_no_team_key(rig):
    r = rig()
    r.hb.tick()
    assert "team" not in r.records()[0]


# -- the cost measurement (desk/team_cost.py) -------------------------------------------------------------------

def test_cost_summary_leaves_out_the_first_bar_of_a_session():
    from desk.team_cost import summary
    rows = [{"first": True, "bar_usd": 0.3, "trader_usd": 0.3, "team_usd": 0.0, "wall_s": 9.0, "failure": None},
            {"first": False, "bar_usd": 0.02, "trader_usd": 0.02, "team_usd": 0.0, "wall_s": 4.0, "failure": None},
            {"first": False, "bar_usd": 0.04, "trader_usd": 0.01, "team_usd": 0.03, "wall_s": 6.0, "failure": "x"}]
    s = summary(rows)
    assert (s["steady_bars"], s["usd_per_bar"], s["team_usd"], s["s_per_bar"], s["first_bar_usd"], s["failures"]) == (2, 0.03, 0.015, 5.0, 0.3, 1)


def test_old_snapshots_get_the_review_gate_key_so_a_delta_can_be_built(tmp_path):
    from desk.team_cost import load
    (tmp_path / "20261023T1820Z.json").write_text(json.dumps({"bars_5min": []}))
    (tmp_path / "20261023T1820Z.delta.json").write_text("{}")  # delta files are not snapshots
    ((name, snap),) = load(None, 1, tmp_path)
    assert name == "20261023T1820Z" and snap["entry_review_gate"]["entries_open"] is True
