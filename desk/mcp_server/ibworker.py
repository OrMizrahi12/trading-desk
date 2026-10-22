"""One thread that owns the IB connection.

ib_async is tied to the thread (and event loop) that created it, and the MCP SDK runs sync tools on whatever worker
thread it likes. So every IB call is handed to this single thread: IB access is thread-safe and strictly one at a
time, which is also what the lab's Executor assumes.
"""

import asyncio
import contextlib
import functools
import logging
import sys
from concurrent.futures import ThreadPoolExecutor

from .. import lab  # noqa: F401
from ib_async import IB
from live.broker import connect_paper

log = logging.getLogger("desk.mcp")

CLIENT_ID = 20  # lab ids 1-8 are taken (see the lab's AGENTS.md); this server owns 20


class Unavailable(RuntimeError):
    """The paper Gateway cannot be reached (not running, not logged in, or the client id is already in use)."""


class IBWorker:
    def __init__(self, connect=None, *, client_id: int = CLIENT_ID):
        self.client_id = client_id
        self._connect = connect or (lambda: connect_paper(IB, client_id=client_id))
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ib", initializer=_own_event_loop)
        self.ib = None
        self._closed = False

    @property
    def connected(self) -> bool:
        return self.ib is not None and self.ib.isConnected()

    def _ensure(self):
        if self.connected:
            return self.ib
        if self.ib is not None:
            with contextlib.suppress(Exception):
                self.ib.disconnect()
            self.ib = None
        try:
            self.ib = self._connect()
        except (OSError, TimeoutError, asyncio.TimeoutError) as exc:
            raise Unavailable(f"cannot reach the IB paper Gateway as client {self.client_id}: "
                              f"{type(exc).__name__}: {exc}. Is it running, logged in, and client {self.client_id} free?") from exc
        log.info("connected to the IB paper Gateway as client %s", self.client_id)
        return self.ib

    def _run(self, fn, args, kwargs, connect: bool):
        if not connect and not self.connected:
            return None
        ib = self._ensure()
        # The lab's helpers print() progress notes; stdout is the MCP wire, so anything printed goes to stderr.
        with contextlib.redirect_stdout(sys.stderr):
            return fn(ib, *args, **kwargs)

    async def call(self, fn, *args, **kwargs):
        """Run `fn(ib, *args, **kwargs)` on the IB thread, connecting first if needed."""
        return await asyncio.get_running_loop().run_in_executor(
            self._pool, functools.partial(self._run, fn, args, kwargs, True))

    async def call_if_connected(self, fn, *args, **kwargs):
        """Like `call`, but returns None instead of connecting when there is no connection."""
        return await asyncio.get_running_loop().run_in_executor(
            self._pool, functools.partial(self._run, fn, args, kwargs, False))

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True

        def bye():
            if self.ib is not None:
                with contextlib.suppress(Exception):
                    self.ib.disconnect()
        await asyncio.get_running_loop().run_in_executor(self._pool, bye)
        self._pool.shutdown(wait=False)


def _own_event_loop() -> None:
    asyncio.set_event_loop(asyncio.new_event_loop())
