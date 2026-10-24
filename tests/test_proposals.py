"""Proposals: risk files and non-skill files are refused, approval mode blocks an apply, the commit title has no doubled id."""

from desk import proposals

SKILL = ".claude/skills/market-read/SKILL.md"


def make_patch(path, name: str):
    patch = path / "p.patch"
    patch.write_text(f"diff --git a/{name} b/{name}\n--- a/{name}\n+++ b/{name}\n@@ -1 +1 @@\n-a\n+b\n")
    return patch


def test_patch_files_lists_every_changed_file(tmp_path):
    patch = tmp_path / "p.patch"
    patch.write_text(f"diff --git a/{SKILL} b/{SKILL}\n...\ndiff --git a/x.md b/x.md\n...\n")
    assert proposals.patch_files(patch) == [SKILL, "x.md"]


def test_title_drops_the_leading_id(tmp_path):
    note = tmp_path / "0007-x.md"
    note.write_text("---\nid: 0007\n---\n# 0007: make it better\n")
    assert proposals.title_of(note, "0007") == "make it better"
    note.write_text("# No id here\n")
    assert proposals.title_of(note, "0007") == "No id here"


def test_risk_files_are_refused(tmp_path):
    for risky in ("CLAUDE.md", "desk/hook.py", "desk/limits.sha256", "vault/Rules/Lab risk limits.md", ".mcp.json"):
        results = dict((name, ok) for name, ok, _ in proposals.checks(make_patch(tmp_path, risky)))
        assert results["risk files untouched"] is False, risky
        assert "tests pass (on a copy)" not in results  # refused before anything is run


def test_only_skills_and_playbook_are_allowed(tmp_path):
    results = dict((name, ok) for name, ok, _ in proposals.checks(make_patch(tmp_path, "desk/budget.py")))
    assert results["risk files untouched"] is True
    assert results["only skills / playbook change"] is False


def test_approval_mode_blocks_apply_and_changes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(proposals, "find", lambda pid: tmp_path / "0001-x.patch")
    monkeypatch.setattr(proposals, "checks", lambda patch: [("everything", True, "ok")])
    calls = []
    monkeypatch.setattr(proposals, "run", lambda *a, **k: calls.append(a))
    assert proposals.apply("0001", approved=False, approval_mode=True) == 2
    assert calls == []


def test_failed_checks_block_apply_even_when_approved(tmp_path, monkeypatch):
    monkeypatch.setattr(proposals, "find", lambda pid: tmp_path / "0001-x.patch")
    monkeypatch.setattr(proposals, "checks", lambda patch: [("tests pass", False, "1 failed")])
    calls = []
    monkeypatch.setattr(proposals, "run", lambda *a, **k: calls.append(a))
    assert proposals.apply("0001", approved=True, approval_mode=False) == 1
    assert calls == []
