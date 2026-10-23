"""A trade from its buy to the moment the desk is flat again, and the `trade_closed` journal record that ends it.

The record is the one place the vault writer reads a trade from. Every number in it comes from the broker's fills, the
journal or arithmetic on those two; a value nobody recorded stays `None` and is never estimated.

Terms: stop = a resting order that sells at the loss limit; target = a resting order that sells at the profit goal;
R = the money lost if the stop is hit, so `result_r` = (exit - entry) / (entry - stop).
"""

from .journal import bar_day


def open_trade(record: dict, result: dict, decision: dict) -> dict:
    """The state kept while the long is open, made from the bar record of the buy."""
    return {"ref": result.get("ref"), "entry_bar": record["bar"], "entry_seen_at": record.get("seen_at"),
            "entry_price": result.get("price"), "shares": result.get("shares"), "stop": decision.get("stop"),
            "target": decision.get("target"), "size_r": decision.get("size_r"), "entry_reason": decision.get("reason"),
            "exits": []}


def add_exit(trade: dict, *, shares, price, kind: str, at: str) -> None:
    """Note a sale the desk itself made (`close`, `sell`, or the `seatbelt` that flattens before the close)."""
    trade["exits"].append({"shares": shares, "price": price, "type": kind, "at": at})


def result_r(entry, exit_, stop) -> float | None:
    if None in (entry, exit_, stop) or entry <= stop:
        return None
    return round((exit_ - entry) / (entry - stop), 2)


def closed_record(trade: dict, *, protection_fills: list[dict], now: str) -> dict:
    """The `trade_closed` record. `protection_fills` = sales by the resting stop/target as the broker reports them
    ({"shares", "price", "time"}); they are used only when the desk made no sale of its own, otherwise the split between
    the two kinds of sale is unknown and the exit price stays empty rather than guessed."""
    legs = list(trade["exits"])
    kind = None
    if not legs and protection_fills:
        legs = [{"shares": f["shares"], "price": f["price"], "type": "resting_order", "at": f.get("time")}
                for f in protection_fills]
    sold = sum(leg["shares"] or 0 for leg in legs)
    known = bool(legs) and all(leg["price"] is not None for leg in legs) and sold == trade["shares"]
    exit_price = round(sum(leg["price"] * leg["shares"] for leg in legs) / sold, 4) if known else None
    kinds = {leg["type"] for leg in legs}
    if len(kinds) == 1:
        kind = kinds.pop()
    elif kinds:
        kind = "mixed"
    if kind == "resting_order" and exit_price is not None:  # the price says which of the two resting orders filled
        if trade["stop"] is not None and exit_price <= trade["stop"]:
            kind = "stop"
        elif trade["target"] is not None and exit_price >= trade["target"]:
            kind = "target"
    last_at = max((leg["at"] for leg in legs if leg.get("at")), default=None)
    return {"trade_closed": {**{k: v for k, v in trade.items() if k != "exits"}, "day": bar_day(trade["entry_bar"]),
                             "exit_price": exit_price, "exit_type": kind, "exit_at": last_at, "exits": legs,
                             "result_r": result_r(trade["entry_price"], exit_price, trade["stop"])},
            "at": now}

