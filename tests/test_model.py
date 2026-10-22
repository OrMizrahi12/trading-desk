"""ClaudeModel: how `claude -p` is called and every way it can fail. A fake subprocess runner, no real claude."""

import json
import subprocess
from types import SimpleNamespace

import pytest

from desk.decision import SCHEMA
from desk import model as model_module
from desk.model import INSTRUCTIONS, SKILLS_DIR, ClaudeModel, ModelFailure, instruction_files, instructions_hash


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
