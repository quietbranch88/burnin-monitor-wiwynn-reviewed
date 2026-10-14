"""Buffered batch writer: collect items in memory, write them in one transaction.

A flush happens when the buffer reaches `batch_size` (condition trigger) or
when `interval` seconds pass since the last flush (periodic trigger). The readings, the
per-minute statistics and the system logs each have one.

A flush can split its items into chunks of `chunk_size`, each written in its own
transaction, with up to `concurrency` chunks in flight on separate connections.

Failure handling, per chunk:
- A transient error (database down) keeps the chunk for the next attempt. The writer
  then waits before retrying, doubling the wait up to `BACKOFF_MAX_SECONDS` with jitter,
  and the next attempt writes one chunk first: the rest follows only if that probe
  succeeds, so a database that is coming back is not hit by the whole backlog at once.
  The buffer is bounded; the oldest items are dropped and counted if it overflows.
- A permanent error (the database rejects a row) retries the chunk's items one by
  one, so a single bad row is dropped instead of blocking every later batch.
"""

import asyncio
import contextlib
import logging
import random
import time
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import datetime

from app.core.time import utcnow

# Failures here must not be written back through the DB log handler.
logger = logging.getLogger("app.batch")

DROP_LOG_SECONDS = 60
# After a failed write: the first retry waits 1 s, then 2, 4... up to this, with jitter.
BACKOFF_MAX_SECONDS = 30.0

# Per stage of a shutdown: waiting for the running write, then the final flush. Three writers
# stop one after another, so keep it well inside the container's stop grace period (45 s).
STOP_TIMEOUT_SECONDS = 5


def _never_permanent(_: Exception) -> bool:
    return False


class PartlyWrittenError(Exception):
    """A row-by-row retry stopped on a transient error after storing some items."""

    def __init__(self, written: int, remaining: list, cause: Exception) -> None:
        super().__init__(str(cause))
        self.written, self.remaining, self.cause = written, remaining, cause


class BatchWriter[T]:
    def __init__(
        self,
        name: str,
        write: Callable[[list[T]], Awaitable[None]],
        *,
        batch_size: int,
        interval: float,
        max_pending: int,
        is_permanent: Callable[[Exception], bool] = _never_permanent,
        chunk_size: int | None = None,
        concurrency: int = 1,
    ) -> None:
        self.name = name
        self._write = write
        self._is_permanent = is_permanent
        self._chunk_size = chunk_size
        self._concurrency = asyncio.Semaphore(concurrency)
        self.batch_size = batch_size
        self.interval = interval
        self.max_pending = max_pending
        self._buffer: deque[T] = deque(maxlen=max_pending)
        self.in_flight = 0  # reserve the entire snapshot until its references are released
        self._wake = asyncio.Event()
        self._stopped = asyncio.Event()
        self._lock = asyncio.Lock()
        self._stopping = False
        self.backoff_seconds = 0.0  # > 0 after a failed flush, until one succeeds
        self._task: asyncio.Task | None = None
        self.written = 0
        self.dropped = 0
        self._drop_logged_at = float("-inf")
        self.flushes = 0
        self.last_flush_at: datetime | None = None
        self.last_error: str | None = None

    @property
    def pending(self) -> int:
        return self.queued + self.in_flight

    @property
    def queued(self) -> int:
        return len(self._buffer)

    def add(self, item: T) -> None:
        """Drop oldest queued items when full; in-flight items cannot be evicted."""
        capacity = self.max_pending - self.in_flight
        if capacity == 0:
            self._drop(1)
            return
        if len(self._buffer) >= capacity:
            self._buffer.popleft()
            self._drop(1)
        self._buffer.append(item)
        if len(self._buffer) >= self.batch_size:
            self._wake.set()

    def extend(self, items: list[T]) -> None:
        """Queue many items at once; the oldest are dropped if the buffer overflows."""
        capacity = self.max_pending - self.in_flight
        overflow = len(self._buffer) + len(items) - capacity
        if overflow > 0:
            self._drop(overflow)
        if capacity == 0:
            return
        while len(self._buffer) + len(items) > capacity and self._buffer:
            self._buffer.popleft()
        self._buffer.extend(items[-capacity:])
        if len(self._buffer) >= self.batch_size:
            self._wake.set()

    def _drop(self, count: int) -> None:
        """Count a loss, and say so once a minute: the retry errors alone would hide it."""
        self.dropped += count
        now = time.monotonic()
        if now - self._drop_logged_at >= DROP_LOG_SECONDS:
            self._drop_logged_at = now
            logger.error("%s buffer full (%d items): the oldest are being dropped, %d so far",
                         self.name, self.max_pending, self.dropped)

    def _requeue(self, batch: list[T]) -> None:
        """Put a failed batch back in front of newer items, keeping the newest when full."""
        room = self.max_pending - len(self._buffer)
        keep = batch[-room:] if room > 0 else []
        self.dropped += len(batch) - len(keep)
        self._buffer.extendleft(reversed(keep))

    async def _write_one_by_one(self, batch: list[T], progress: list[int]) -> int:
        """`progress` is [items handled, items written], so a cancelled flush knows what is stored."""
        written = 0
        for index, item in enumerate(batch):
            try:
                await self._write([item])
                written += 1
            except Exception as exc:
                if not self._is_permanent(exc):
                    # Items before this one are committed; only the rest may be retried.
                    raise PartlyWrittenError(written, batch[index:], exc) from exc
                self.dropped += 1
                logger.error("%s dropped one rejected item: %s", self.name, f"{type(exc).__name__}: {exc}"[:300])
            progress[:] = [index + 1, written]
        return written

    async def _write_chunk(self, chunk: list[T], progress: list[int]) -> int:
        async with self._concurrency:
            try:
                await self._write(chunk)
                return len(chunk)
            except Exception as exc:
                if not self._is_permanent(exc):
                    raise
                return await self._write_one_by_one(chunk, progress)

    async def flush(self) -> int:
        """Write everything pending; never raises. Returns the number of items written."""
        async with self._lock:
            if not self._buffer:
                return 0
            batch = list(self._buffer)
            self._buffer.clear()
            self.in_flight = len(batch)
            size = self._chunk_size or len(batch)
            chunks = [batch[i:i + size] for i in range(0, len(batch), size)]
            stored: dict[int, int] = {}  # chunk index -> items written
            progress = [[0, 0] for _ in chunks]  # per chunk, while retried row by row

            async def write(index: int) -> int:
                count = await self._write_chunk(chunks[index], progress[index])
                stored[index] = count
                return count

            async def attempt(indices: range) -> tuple[int, list[T], BaseException | None]:
                try:
                    results = await asyncio.gather(*(write(i) for i in indices), return_exceptions=True)
                except asyncio.CancelledError:
                    # Shutdown stopped waiting: put back what was not confirmed instead of losing it
                    # unseen. Rows already stored one by one are not put back: system logs have no
                    # unique key, so writing them again would store them twice.
                    self.written += sum(stored.values()) + sum(p[1] for i, p in enumerate(progress) if i not in stored)
                    self.in_flight = 0  # release and requeue without yielding
                    self._requeue([item for i, chunk in enumerate(chunks) if i not in stored
                                   for item in chunk[progress[i][0]:]])
                    raise
                written = 0
                failed: list[T] = []
                error: BaseException | None = None
                for index, result in zip(indices, results, strict=True):
                    if isinstance(result, PartlyWrittenError):
                        written += result.written
                        failed += result.remaining
                        error = result.cause
                    elif isinstance(result, BaseException):  # gather also hands back a CancelledError
                        failed += chunks[index]
                        error = result
                    else:
                        written += result
                return written, failed, error

            rest = range(len(chunks))
            written = 0
            failed: list[T] = []
            error: BaseException | None = None
            if self.backoff_seconds:
                # The last flush failed: probe with one chunk. If it fails too, the others go back
                # untouched, in order, instead of each hitting a database that is still down.
                written, failed, error = await attempt(range(1))
                rest = range(1, len(chunks))
                if failed:
                    failed += [item for i in rest for item in chunks[i]]
                    rest = range(0)
            if rest:
                more, failed_more, error_more = await attempt(rest)
                written, failed, error = written + more, failed + failed_more, error_more or error
            self.written += written
            self.in_flight = 0  # nothing below yields; snapshot references die on return
            if failed:
                # Keep the writer alive; only the chunks that failed go back in the buffer.
                self.last_error = f"{type(error).__name__}: {error}"[:500]
                self.backoff_seconds = min(max(1.0, self.backoff_seconds * 2), BACKOFF_MAX_SECONDS)
                logger.error("%s flush failed for %d items, next attempt in about %.0f s: %s",
                             self.name, len(failed), self.backoff_seconds, self.last_error)
                self._requeue(failed)
            else:
                self.flushes += 1
                self.last_flush_at = utcnow()
                self.last_error = None
                self.backoff_seconds = 0.0
            return written

    async def run(self) -> None:
        while not self._stopping:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=self.interval)
            self._wake.clear()
            if self.backoff_seconds and not self._stopping:
                # Wait out the backoff first (a full buffer keeps `_wake` set, so it cannot pace
                # this); jitter spreads the three writers' retries apart. A stop ends the wait.
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stopped.wait(), timeout=self.backoff_seconds * random.uniform(0.5, 1.0))
            await self.flush()

    def start(self) -> None:
        if self._task is None:
            self._stopping = False
            self._stopped.clear()
            self._task = asyncio.create_task(self.run(), name=f"batch-writer-{self.name}")

    async def stop(self) -> None:
        """Let an in-flight write finish, then write whatever is still buffered, each within a time limit."""
        if self._task is not None:
            self._stopping = True
            self._stopped.set()
            self._wake.set()
            try:
                await asyncio.wait_for(self._task, timeout=STOP_TIMEOUT_SECONDS)
            except TimeoutError:
                logger.error("%s did not stop within %d s", self.name, STOP_TIMEOUT_SECONDS)
            self._task = None
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self.flush(), timeout=STOP_TIMEOUT_SECONDS)
        if self._buffer:
            lost = len(self._buffer)
            self.dropped += lost
            self._buffer.clear()
            logger.error("%s stopped with %d items not written", self.name, lost)
