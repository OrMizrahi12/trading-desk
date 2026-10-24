"""The memory budget: measured in tokens, fails above the limit, says which note to summarise or move."""

import shutil
from pathlib import Path

from desk import memory

DESK = Path(__file__).parent.parent


def make_desk(tmp_path) -> Path:
    """A copy of the real constitution and skills with an empty vault, so the tests do not depend on real lessons."""
    shutil.copy(DESK / "CLAUDE.md", tmp_path / "CLAUDE.md")
    shutil.copytree(DESK / ".claude" / "skills", tmp_path / ".claude" / "skills")
    (tmp_path / "vault" / "Lessons" / "Archive").mkdir(parents=True)
    return tmp_path


def lesson(desk: Path, name: str, *, status="active", seen=1, last="2026-10-20", summary="", words=0, folder="") -> Path:
    front = f"---\ntype: lesson\nstatus: {status}\nseeded: true\nseen: {seen}\nlast_seen: {last}\n"
    front += f"summary: {summary}\n" if summary else ""
    path = desk / "vault" / "Lessons" / folder / f"{name}.md"
    path.write_text(front + "---\n# " + name + "\n\n" + "word " * words)
    return path


def test_only_active_top_level_lessons_load(tmp_path):
    desk = make_desk(tmp_path)
    lesson(desk, "Loaded", summary="One sentence.")
    for status in ("proposed", "disputed"):
        lesson(desk, f"Not {status}", status=status, summary="x")
    lesson(desk, "Retired", status="archived", summary="x", folder="Archive")
    assert memory.brief(desk / "vault").splitlines()[1:] == ["- Loaded: One sentence."]


def test_no_lesson_means_no_brief_and_no_extra_cost(tmp_path):
    desk = make_desk(tmp_path)
    assert memory.brief(desk / "vault") == ""
    report = memory.measure(desk)
    assert report.header_tokens == 0 and not report.over and report.advice == []


def test_a_lesson_without_summary_costs_its_whole_body_and_is_named(tmp_path):
    desk = make_desk(tmp_path)
    lesson(desk, "Rambling", words=2000)
    report = memory.measure(desk)
    assert report.advice and report.advice[0].startswith("SUMMARISE vault/Lessons/Rambling.md")
    assert "NO SUMMARY" in memory.render(report)


def test_over_budget_fails_names_the_note_and_summarising_brings_it_back(tmp_path):
    desk = make_desk(tmp_path)
    for n in range(8):
        lesson(desk, f"Long {n}", seen=n, words=1000)
    report = memory.measure(desk)
    assert report.over
    assert all(a.startswith("SUMMARISE") for a in report.advice)
    for n in range(8):  # follow the advice: add a summary to each
        path = desk / "vault" / "Lessons" / f"Long {n}.md"
        path.write_text(path.read_text().replace("---\n# ", "summary: Short.\n---\n# ", 1))
    assert not memory.measure(desk).over


def test_when_summaries_are_not_enough_the_least_proven_lesson_is_moved_first(tmp_path):
    desk = make_desk(tmp_path)
    lesson(desk, "Proven", seen=9, last="2026-10-22", summary="A" * 200)
    lesson(desk, "Newer once", seen=1, last="2026-10-22", summary="B" * 200)
    lesson(desk, "Oldest once", seen=1, last="2026-10-01", summary="C" * 200)
    base = memory.measure(desk).total
    report = memory.measure(desk, budget=base - 1)
    assert report.over and report.advice[0].startswith("MOVE vault/Lessons/Oldest once.md")
    assert len(report.advice) == 1  # moving one is enough


def test_the_limit_is_6000_and_a_seeded_note_is_warned_about(tmp_path):
    assert memory.BUDGET_TOKENS == 6000
    desk = make_desk(tmp_path)
    lesson(desk, "Fake", summary="x")
    assert "seeded" in memory.render(memory.measure(desk))


def test_fixed_load_over_budget_is_not_blamed_on_the_vault(tmp_path):
    desk = make_desk(tmp_path)
    advice = memory.measure(desk, budget=10).advice
    assert advice and "CLAUDE.md or a skill" in advice[-1]


def test_the_real_desk_is_within_budget_with_no_seeded_note_active():
    report = memory.measure()
    assert not report.over
    assert not [i for i in report.items if i.seeded]


def test_main_exits_1_over_budget_and_0_under(tmp_path):
    vault = make_desk(tmp_path) / "vault"
    assert memory.main(["--vault", str(vault)]) == 0  # real constitution, empty vault
    assert memory.main(["--vault", str(vault), "--budget", "10"]) == 1
