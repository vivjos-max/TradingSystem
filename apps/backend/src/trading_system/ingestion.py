from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from .models import EventOutcome, PersistedEvent


class IngestionOverflow(RuntimeError):
    pass


class EventStore(Protocol):
    def append_events(self, events: list[PersistedEvent]) -> set[str] | int: ...


@dataclass(frozen=True)
class IngestionItem:
    event: PersistedEvent
    apply: Callable[[], None] | None = None


@dataclass(frozen=True)
class IngestionMetrics:
    queued: int
    peak: int
    persisted: int
    overflowed: int
    last_write_ms: float | None


class IngestionQueue:
    """Persist first, then publish; saturation is fatal and never silently drops state."""

    def __init__(self, store: EventStore, *, capacity: int, batch_size: int) -> None:
        if capacity < 1 or batch_size < 1 or batch_size > capacity:
            raise ValueError("invalid ingestion queue sizing")
        self._store = store
        self._queue: queue.Queue[IngestionItem | None] = queue.Queue(capacity)
        self._batch_size = batch_size
        self._peak = self._persisted = self._overflowed = 0
        self._last_write_ms: float | None = None
        self._failure: BaseException | None = None
        self._closed = False
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, name="event-persistence", daemon=True)
        self._thread.start()

    @property
    def failure(self) -> BaseException | None:
        return self._failure

    @property
    def metrics(self) -> IngestionMetrics:
        with self._lock:
            return IngestionMetrics(
                self._queue.qsize(),
                self._peak,
                self._persisted,
                self._overflowed,
                self._last_write_ms,
            )

    def submit(self, item: IngestionItem) -> None:
        if self._closed:
            self._release_claim(item)
            raise RuntimeError("persistence queue is closed")
        if self._failure is not None:
            self._release_claim(item)
            raise RuntimeError("persistence worker failed") from self._failure
        try:
            self._queue.put_nowait(item)
        except queue.Full:
            with self._lock:
                self._overflowed += 1
            self._release_claim(item)
            raise IngestionOverflow("bounded ingestion queue is full") from None
        with self._lock:
            self._peak = max(self._peak, self._queue.qsize())

    def join(self) -> None:
        self._queue.join()
        if self._failure is not None:
            raise RuntimeError("persistence worker failed") from self._failure

    def close(self) -> None:
        self._closed = True
        if self._thread.is_alive():
            self._queue.put(None)
            self._thread.join(timeout=10)
        if self._thread.is_alive():
            raise RuntimeError("persistence worker did not stop")
        if self._failure is not None:
            raise RuntimeError("persistence worker failed") from self._failure

    def _run(self) -> None:
        while True:
            first = self._queue.get()
            if first is None:
                self._queue.task_done()
                return
            batch = [first]
            while len(batch) < self._batch_size:
                try:
                    item = self._queue.get_nowait()
                except queue.Empty:
                    break
                if item is None:
                    self._queue.task_done()
                    self._queue.put(None)
                    break
                batch.append(item)
            try:
                started = time.perf_counter()
                result = self._store.append_events([item.event for item in batch])
                inserted = (
                    ({item.event.idempotency_key for item in batch} if result else set())
                    if isinstance(result, int)
                    else set(result)
                )
                elapsed = (time.perf_counter() - started) * 1000
                for item in batch:
                    if item.apply is not None and item.event.idempotency_key in inserted:
                        item.apply()
                with self._lock:
                    self._persisted += len(inserted)
                    self._last_write_ms = elapsed
            except BaseException as exc:
                self._failure = exc
                for item in batch:
                    self._release_claim(item)
            finally:
                for _ in batch:
                    self._queue.task_done()
            if self._failure is not None:
                while True:
                    try:
                        item = self._queue.get_nowait()
                    except queue.Empty:
                        break
                    if item is not None:
                        self._release_claim(item)
                    self._queue.task_done()
                return

    def _release_claim(self, item: IngestionItem) -> None:
        if item.event.outcome is EventOutcome.DUPLICATE:
            return
        release = getattr(self._store, "release_event_claim", None)
        if release is not None:
            try:
                release(item.event.event_identity)
            except Exception:
                pass
