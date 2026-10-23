"""ClaudeModel: how `claude -p` is called and every way it can fail. A fake subprocess runner, no real claude."""

import json
import subprocess
from types import SimpleNamespace

import pytest

from desk.decision import SCHEMA
from desk import model as model_module
from desk.model import (INSTRUCTIONS, SKILLS_DIR, ClaudeModel, ModelFailure, Session, context_usage, instruction_files,
                        instructions_hash)


def runner(stdout="", returncode=0, stderr="", raises=None):
    seen = {}

    def run(cmd, **kw):
        seen.update(cmd=cmd, **kw)
        if raises:
            raise raises
        return SimpleNamespace(stdout=stdout, returncode=returncode, stderr=stderr)

    run.seen = seen
    return run


def reply(**kw):
    return json.dumps({"is_error": False, "result": "x", "structured_output": {"action": "wait"}, **kw})


def test_call_shape():
    run = runner(reply())
    assert ClaudeModel(run=run).decide({"bar": 1}, 60) == {"action": "wait"}
    cmd = run.seen["cmd"]
    assert cmd[:2] == ["claude", "-p"]
    assert json.loads(cmd[cmd.index("--json-schema") + 1]) == SCHEMA
    assert cmd[cmd.index("--tools") + 1] == "Skill"  # the only tool: loading the desk's skills
    assert "--no-session-persistence" in cmd
    assert cmd[cmd.index("--system-prompt") + 1] == INSTRUCTIONS.read_text()
    assert cmd[cmd.index("--setting-sources") + 1] == ""  # no user/project/local files: CLAUDE.md reaches the model once, alone
    assert cmd[cmd.index("--plugin-dir") + 1] == str(run.seen["cwd"])  # how the skills in .claude/skills reach the trader
    assert json.loads(run.seen["input"]) == {"bar": 1}  # the snapshot goes in on stdin
    assert run.seen["timeout"] == 60
    assert run.seen["cwd"].name == "trading-desk"


@pytest.mark.parametrize("run, kind", [
    (runner(raises=subprocess.TimeoutExpired("claude", 5)), "timeout"),
    (runner(raises=FileNotFoundError("claude")), "not_started"),
    (runner(returncode=1, stderr="boom"), "exit_code"),
    (runner("not json"), "bad_output"),
    (runner(json.dumps({"is_error": True, "result": "overloaded"})), "error"),
    (runner(json.dumps({"is_error": False, "result": "text only"})), "bad_output"),
])
def test_failures_raise_model_failure(run, kind):
    with pytest.raises(ModelFailure) as err:
        ClaudeModel(run=run).decide({}, 10)
    assert err.value.kind == kind


def test_no_time_left_is_a_timeout_without_calling_claude():
    run = runner(reply())
    with pytest.raises(ModelFailure) as err:
        ClaudeModel(run=run).decide({}, 0)
    assert err.value.kind == "timeout" and not run.seen


SKILLS = ["market-read", "entry-checklist", "trade-management", "post-trade-review"]


@pytest.mark.parametrize("name", SKILLS)
def test_skill_file_is_short_and_says_when_to_use_it(name):
    text = (SKILLS_DIR / name / "SKILL.md").read_text()
    head, _, body = text.removeprefix("---\n").partition("\n---\n")
    fields = dict(line.split(": ", 1) for line in head.splitlines())
    assert fields["name"] == name  # the folder name, so the trader loads it as desk:<name>
    assert fields["description"].startswith("Use ") and len(fields["description"]) > 80
    assert text.count("\n") < 80 and body.strip()


def test_entry_checklist_has_the_size_math_in_r():
    text = (SKILLS_DIR / "entry-checklist" / "SKILL.md").read_text()
    assert "0.01" in text and "floor(" in text and "1.005" in text and "size_r" in text


def test_claude_md_is_short_and_points_to_every_skill():
    text = INSTRUCTIONS.read_text()
    assert text.count("\n") < 60
    assert all(f"desk:{name}" in text for name in SKILLS)


def test_plugin_manifest_points_at_the_skills_folder():
    manifest = json.loads(model_module.PLUGIN_MANIFEST.read_text())
    assert manifest["name"] == "desk" and (model_module.DESK_DIR / manifest["skills"]).resolve() == SKILLS_DIR.resolve()


def test_instructions_hash_covers_the_skills(monkeypatch, tmp_path):
    assert {p.name for p in instruction_files()} >= {"CLAUDE.md", "plugin.json", "SKILL.md"}
    before = instructions_hash()
    assert before == instructions_hash()  # stable
    desk = tmp_path  # a desk folder of its own, with one skill
    (desk / ".claude" / "skills" / "x").mkdir(parents=True)
    skill = desk / ".claude" / "skills" / "x" / "SKILL.md"
    (desk / "CLAUDE.md").write_text("constitution")
    (desk / ".claude-plugin").mkdir()
    (desk / ".claude-plugin" / "plugin.json").write_text("{}")
    skill.write_text("v1")
    for name, value in [("DESK_DIR", desk), ("INSTRUCTIONS", desk / "CLAUDE.md"), ("SKILLS_DIR", desk / ".claude" / "skills"),
                        ("PLUGIN_MANIFEST", desk / ".claude-plugin" / "plugin.json")]:
        monkeypatch.setattr(model_module, name, value)
    first = instructions_hash()
    skill.write_text("v2")
    assert instructions_hash() != first  # editing a skill changes the fingerprint


# -- one conversation per day -----------------------------------------------------------------

USAGE = {"input_tokens": 3, "cache_read_input_tokens": 20_000, "cache_creation_input_tokens": 1_500, "output_tokens": 90}


def session_reply(**kw):
    return reply(session_id="abc-1", usage={**USAGE, "iterations": [USAGE]}, total_cost_usd=0.0123,
                 modelUsage={"claude-x": {"contextWindow": 1_000_000}}, **kw)


def test_new_session_is_started_with_its_id_and_the_intro_in_the_first_message():
    run = runner(session_reply())
    uid = "11111111-1111-4111-8111-111111111111"
    ClaudeModel(run=run).decide({}, 60, Session(uid, intro="Today is Friday 2026-10-23."))
    cmd = run.seen["cmd"]
    assert cmd[cmd.index("--session-id") + 1] == uid and "--resume" not in cmd
    assert "--no-session-persistence" not in cmd
    assert cmd[2].startswith("Today is Friday 2026-10-23.\n\nDecide for the closed bar")


def test_later_bar_resumes_the_same_session_without_the_intro():
    run = runner(session_reply())
    ClaudeModel(run=run).decide({}, 60, Session("abc-1", resume=True))
    cmd = run.seen["cmd"]
    assert cmd[cmd.index("--resume") + 1] == "abc-1" and "--session-id" not in cmd
    assert cmd[2].startswith("Decide for the closed bar")


def test_the_instructions_are_sent_again_on_every_bar_and_the_reported_hash_is_of_the_text_sent():
    # Without --system-prompt-snapshot off, claude keeps the first request's system prompt on resume: the hash would lie.
    run = runner(session_reply())
    s = Session("abc-1", resume=True)
    ClaudeModel(run=run).decide({}, 60, s)
    cmd = run.seen["cmd"]
    assert cmd[cmd.index("--system-prompt-snapshot") + 1] == "off"
    assert cmd[cmd.index("--system-prompt") + 1] == INSTRUCTIONS.read_text()
    assert s.instructions == instructions_hash()


def test_context_size_is_input_plus_cached_input_of_the_last_request():
    run = runner(session_reply())
    s = Session("abc-1", resume=True)
    ClaudeModel(run=run).decide({}, 60, s)
    assert s.context == {"tokens": 21_503, "input_tokens": 3, "cache_read_input_tokens": 20_000,
                         "cache_creation_input_tokens": 1_500, "output_tokens": 90, "window": 1_000_000, "cost_usd": 0.0123}
    assert context_usage({})["tokens"] == 0  # a reply without usage does not crash


def test_context_is_reported_even_when_the_answer_is_unusable_and_the_reported_session_id_wins():
    s = Session("mine")
    with pytest.raises(ModelFailure):
        ClaudeModel(run=runner(json.dumps({"is_error": False, "session_id": "theirs", "usage": USAGE}))).decide({}, 5, s)
    assert s.context["tokens"] == 21_503 and s.id == "theirs"
