"""A time budget for one bar: everything the heartbeat does for a bar must fit inside it."""

import time


class Deadline:
    """`seconds` from now. `clock` is injectable so tests need no real waiting."""

    def __init__(self, seconds: float, clock=time.monotonic):
        self.clock, self.seconds = clock, seconds
        self.start = clock()

    def elapsed(self) -> float:
        return self.clock() - self.start

    def remaining(self) -> float:
        return self.seconds - self.elapsed()

    @property
    def expired(self) -> bool:
        return self.remaining() <= 0
