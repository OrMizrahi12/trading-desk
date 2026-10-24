"""Self-improvement proposals: a change to the desk's playbook or skills is a patch in `proposals/`, checked, then applied.

`uv run -m desk.proposals check 0001` runs the checks: only skills / playbook files change, the risk files are untouched,
the patch applies, and on a throw-away copy the tests pass and the memory budget holds. `apply 0001` repeats the checks
and, once a person passes `--approved` (approval mode is ON by default; `--no-approval` turns it off), applies the patch,
commits it and adds a new trader version to `experiments/ledger.csv`. Paper trading only; this edits text files, never an account.
"""

import argparse
import csv
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path

from .memory import DESK_DIR
from .model import instructions_hash

PROPOSALS = DESK_DIR / "proposals"
LEDGER = DESK_DIR / "experiments" / "ledger.csv"
PY = DESK_DIR / ".venv" / "bin" / "python"
ALLOWED = (r"^\.claude/skills/[a-z-]+/SKILL\.md$", r"^vault/Playbook/[^/]+\.md$")  # skills and playbook only
# Never changed by a proposal. CLAUDE.md holds the risk rules, so it is a person's file too.
RISK = (r"^CLAUDE\.md$", r"^\.claude/settings", r"^\.mcp\.json$", r"^desk/(hook|gates|seatbelts|limits_pin|lab|decision|execute)\.py$",
        r"^desk/limits\.sha256$", r"^vault/Rules/", r"^\.claude-plugin/")


def run(cmd, cwd=DESK_DIR):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)


def last_line(r) -> str:
    text = (r.stdout.strip() or r.stderr.strip())
    return text.splitlines()[-1] if text else ""


def patch_files(patch: Path) -> list[str]:
    return re.findall(r"^diff --git a/(\S+) b/", patch.read_text(), re.M)


def find(pid: str) -> Path:
    found = sorted(PROPOSALS.glob(f"{pid}-*.patch"))
    if not found:
        sys.exit(f"no proposal {pid}")
    return found[0]


def checks(patch: Path) -> list[tuple[str, bool, str]]:
    files = patch_files(patch)
    bad = [f for f in files if any(re.match(p, f) for p in RISK)]
    off = [f for f in files if not any(re.match(p, f) for p in ALLOWED)]
    out = [("risk files untouched", bool(files) and not bad, f"touches {files}" + (f"; FORBIDDEN {bad}" if bad else "")),
           ("only skills / playbook change", not off, "ok" if not off else f"not allowed: {off}")]
    r = run(["git", "apply", "--check", str(patch.resolve())])
    out.append(("patch applies", r.returncode == 0, r.stderr.strip() or "ok"))
    if out[-1][1] and not bad:
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / DESK_DIR.name  # a test reads the folder name
            shutil.copytree(DESK_DIR, copy, ignore=shutil.ignore_patterns(".git", ".venv", "__pycache__", ".pytest_cache", "data"))
            run(["patch", "-p1", "-i", str(patch.resolve())], cwd=copy)
            t = run([str(PY), "-I", "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider"], cwd=copy)
            out.append(("tests pass (on a copy)", t.returncode == 0, last_line(t)))
            m = run([str(PY), "-m", "desk.memory"], cwd=copy)
            out.append(("memory budget ok (on a copy)", m.returncode == 0, last_line(m)))
    pin = run([str(PY), "-m", "desk.limits_pin"])
    out.append(("limits file matches its pin", pin.returncode == 0, last_line(pin)))
    return out


def report(results) -> bool:
    for name, ok, note in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {note}")
    return all(ok for _, ok, _ in results)


def apply(pid: str, approved: bool, approval_mode: bool) -> int:
    patch = find(pid)
    if not report(checks(patch)):
        print("checks failed: nothing applied")
        return 1
    if approval_mode and not approved:
        print("checks passed, but approval mode is ON: a person must approve (rerun with --approved)")
        return 2
    if run(["git", "status", "--porcelain", "--untracked-files=no"]).stdout.strip():
        print("working tree has uncommitted changes: commit or stash first")
        return 1
    run(["git", "apply", str(patch)])
    title = re.search(r"^# (.+)$", patch.with_suffix(".md").read_text(), re.M).group(1)
    rows = list(csv.DictReader(LEDGER.open()))
    last = rows[-1]
    n = len([r for r in rows if r["version"].startswith("Trader v2.")]) + 1
    new = {**last, "version": f"Trader v2.{n} (proposal {pid})", "start_date": date.today().isoformat(), "end_date": "",
           "instructions_file_hash": instructions_hash(),
           "notes": f"Proposal {title}. Patch proposals/{patch.name}; revert the commit to roll back."}
    with LEDGER.open("a", newline="") as f:
        csv.DictWriter(f, fieldnames=list(last)).writerow(new)
    run(["git", "add", "proposals", "experiments/ledger.csv", "desk/proposals.py", *patch_files(patch)])
    run(["git", "commit", "-m", f"Proposal {pid}: {title}\n\nTrader version {new['version']}, instructions "
         f"{new['instructions_file_hash']}.\n\nCo-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"])
    print(f"applied and committed as {new['version']} (instructions {new['instructions_file_hash']})")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("cmd", choices=["check", "apply"])
    ap.add_argument("id")
    ap.add_argument("--approved", action="store_true", help="a person approved this proposal")
    ap.add_argument("--no-approval", action="store_true", help="turn approval mode off (it is ON by default)")
    a = ap.parse_args(argv)
    if a.cmd == "check":
        return 0 if report(checks(find(a.id))) else 1
    return apply(a.id, a.approved, not a.no_approval)


if __name__ == "__main__":
    sys.exit(main())
