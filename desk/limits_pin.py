"""A pinned fingerprint of the lab's risk limits file, so a swapped or edited file is noticed before an order is sent.

On this machine the limits file cannot be made truly immutable (no `chattr`, and its folder belongs to the same user as
the desk), so any process running as that user could replace it. This does not prevent that; it makes it fail closed:
the MCP server refuses new entries while the file's SHA-256 (a fingerprint of its exact bytes) differs from the one
committed in `limits.sha256`. Changing the limits on purpose therefore means changing the pin too, which shows in git.

A person re-pins after a deliberate change: `.venv/bin/python -m desk.limits_pin --update`.
"""

import hashlib
import sys
from pathlib import Path

from . import lab  # noqa: F401
from live.risk import DEFAULT_LIMITS

PIN_FILE = Path(__file__).with_name("limits.sha256")


class LimitsTampered(RuntimeError):
    """The limits file does not match its pin (or the pin or the file is unreadable). Nothing may be bought."""


def fingerprint(limits: Path = DEFAULT_LIMITS) -> str:
    return hashlib.sha256(Path(limits).read_bytes()).hexdigest()


def verify(limits: Path = DEFAULT_LIMITS, pin: Path = PIN_FILE) -> None:
    try:
        pinned, actual = Path(pin).read_text().split()[0], fingerprint(limits)
    except (OSError, IndexError) as exc:
        raise LimitsTampered(f"cannot compare the limits file with its pin ({type(exc).__name__}: {exc})") from exc
    if pinned != actual:
        raise LimitsTampered(f"the limits file {Path(limits).name} no longer matches its pin (pinned {pinned[:12]}, found "
                             f"{actual[:12]}). A person must review it, then run `python -m desk.limits_pin --update`")


def main(argv: list[str]) -> int:
    if argv == ["--update"]:
        PIN_FILE.write_text(f"{fingerprint()}  {DEFAULT_LIMITS.name}\n")
        print(f"pinned {DEFAULT_LIMITS.name}: {fingerprint()[:12]}")
        return 0
    try:
        verify()
    except LimitsTampered as exc:
        print(exc, file=sys.stderr)
        return 1
    print("limits file matches its pin")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
