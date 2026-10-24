"""The decision Claude must return for each bar, and its strict validation. Paper trading only.

Terms:
- R: the dollars risked on one trade if the stop is hit. The desk fixes 1R at the lab's RISK_PCT (1%) of the
  account's net liquidation (total account value). `size_r` is the position size in multiples of R, so
  `size_r` 0.5 risks half of 1R.
- stop: a resting sell order below the entry price that closes the trade at a loss limit.
- target: a resting sell order above the entry price that closes the trade at a profit goal.
- buy: open a long position (profit if the price rises) with a stop and optional target.
- sell: sell part of an existing long position (never a short sale: the desk does not short).
- close: sell the whole long position.
- wait: do nothing.

Validation never repairs an answer: anything off is rejected and the bar is treated as `wait`.
"""

import math
from dataclasses import dataclass

ACTIONS = ("buy", "sell", "close", "wait")
MAX_SIZE_R = 2.0
MAX_REASON = 280
MAX_LESSONS = 5  # lessons one decision may name
MAX_LESSON_NAME = 80  # characters of a lesson's title

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["action", "size_r", "stop", "target", "reason", "lessons"],
    "properties": {
        "action": {"type": "string", "enum": list(ACTIONS)},
        "size_r": {"type": "number", "minimum": 0, "maximum": MAX_SIZE_R,
                   "description": "position size in R (1R = 1% of the account lost if the stop is hit); 0 for wait/close"},
        "stop": {"type": ["number", "null"], "description": "stop price, below the entry; required for buy"},
        "target": {"type": ["number", "null"], "description": "profit target price, above the entry; null for none"},
        "reason": {"type": "string", "maxLength": MAX_REASON, "description": "short reason"},
        "lessons": {"type": "array", "maxItems": MAX_LESSONS, "uniqueItems": True,
                    "items": {"type": "string", "maxLength": MAX_LESSON_NAME},
                    "description": "titles of the memory-brief lessons that really shaped this decision, exactly as "
                                   "written in the brief; [] when none"},
    },
}


class InvalidDecision(ValueError):
    """The model's answer does not fit the schema or the rules."""


@dataclass(frozen=True)
class Decision:
    action: str
    size_r: float
    stop: float | None
    target: float | None
    reason: str
    lessons: tuple[str, ...] = ()  # titles of the lessons applied, for the lesson tracker (desk/lessons.py)


def _number(value, name: str, *, allow_none: bool = False) -> float | None:
    if value is None and allow_none:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise InvalidDecision(f"{name} must be a finite number, got {value!r}")
    return float(value)


def _lessons(value) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > MAX_LESSONS:
        raise InvalidDecision(f"lessons must be a list of at most {MAX_LESSONS} titles")
    names = []
    for name in value:
        if not isinstance(name, str) or not name.strip() or len(name) > MAX_LESSON_NAME:
            raise InvalidDecision(f"a lesson title must be a non-empty string of at most {MAX_LESSON_NAME} characters")
        if name.strip() not in names:
            names.append(name.strip())
    return tuple(names)


def parse(raw) -> Decision:
    """Turn the model's answer (a dict) into a Decision, or raise InvalidDecision."""
    if not isinstance(raw, dict):
        raise InvalidDecision(f"answer is not a JSON object: {type(raw).__name__}")
    extra, missing = set(raw) - set(SCHEMA["properties"]), set(SCHEMA["required"]) - set(raw)
    if extra or missing:
        raise InvalidDecision(f"unexpected keys {sorted(extra)} / missing keys {sorted(missing)}")
    action = raw["action"]
    if action not in ACTIONS:
        raise InvalidDecision(f"action must be one of {ACTIONS}, got {action!r}")
    size_r = _number(raw["size_r"], "size_r")
    stop = _number(raw["stop"], "stop", allow_none=True)
    target = _number(raw["target"], "target", allow_none=True)
    reason = raw["reason"]
    if not isinstance(reason, str) or not reason.strip():
        raise InvalidDecision("reason must be a non-empty string")
    if len(reason) > MAX_REASON:
        raise InvalidDecision(f"reason is {len(reason)} characters, the limit is {MAX_REASON}")
    if not 0 <= size_r <= MAX_SIZE_R:
        raise InvalidDecision(f"size_r must be between 0 and {MAX_SIZE_R}, got {size_r}")
    if action in ("buy", "sell") and size_r <= 0:
        raise InvalidDecision(f"{action} needs size_r above 0")
    if action == "buy":
        if stop is None or stop <= 0:
            raise InvalidDecision("buy needs a positive stop")
        if target is not None and target <= 0:
            raise InvalidDecision("target must be positive or null")
    return Decision(action, size_r, stop, target, reason.strip(), _lessons(raw["lessons"]))


def check_prices(decision: Decision, ref_price: float) -> None:
    """A buy's stop must sit below the price and its target above it. `ref_price` is the last bar's close."""
    if decision.action != "buy":
        return
    if not decision.stop < ref_price:
        raise InvalidDecision(f"stop {decision.stop} is not below the price {ref_price}")
    if decision.target is not None and not decision.target > ref_price:
        raise InvalidDecision(f"target {decision.target} is not above the price {ref_price}")
