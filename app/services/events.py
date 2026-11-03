"""Pub/sub in memoria per gli aggiornamenti live (Server-Sent Events).

I deploy girano in thread: publish() è thread-safe e consegna gli eventi
alle code asyncio dei client collegati tramite il loop dell'applicazione.
"""
import asyncio
import threading
from collections import defaultdict


class EventBus:
    def __init__(self):
        self._subs: dict[int, set[asyncio.Queue]] = defaultdict(set)
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def subscribe(self, run_id: int) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        with self._lock:
            self._subs[run_id].add(q)
        return q

    def unsubscribe(self, run_id: int, q: asyncio.Queue) -> None:
        with self._lock:
            self._subs[run_id].discard(q)
            if not self._subs[run_id]:
                del self._subs[run_id]

    def publish(self, run_id: int, event: dict) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        with self._lock:
            queues = list(self._subs.get(run_id, ()))
        for q in queues:
            loop.call_soon_threadsafe(q.put_nowait, event)
