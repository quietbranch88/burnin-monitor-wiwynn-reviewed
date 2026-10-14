"""Background WebSocket subscriber for one Streamlit session.

Streamlit reruns scripts top to bottom, so it cannot hold a socket in the
script itself. This thread keeps the connection open and buffers the per-tick
fleet summaries; the dashboard fragment reads the buffer once per second.
"""

import hashlib
import json
import threading
import time
from collections import deque
from datetime import datetime

import requests
from websockets.exceptions import ConnectionClosed, InvalidStatus
from websockets.sync.client import connect

IDLE_TIMEOUT = 120  # seconds without a reader before the thread exits
MAX_FILL_SECONDS = 600  # the API keeps 10 minutes of tick summaries


def _complete(readings: list[dict], tick: dict) -> list[dict]:
    """Add what the API leaves out of each per-server reading: the tick's time and the threshold."""
    summary = tick["summary"]
    out = []
    for r in readings:
        s = summary[r["category"]]
        over = r["value"] < s["threshold"] if s["alert_below"] else r["value"] > s["threshold"]
        out.append(r | {"threshold": s["threshold"], "is_anomaly": over, "generated_at": tick["generated_at"]})
    return out


def _at(tick: dict) -> datetime:
    return datetime.fromisoformat(tick["generated_at"])


def _digest(tick: dict) -> str:
    return hashlib.blake2b(json.dumps(tick, sort_keys=True, default=str).encode(), digest_size=8).hexdigest()


def fingerprint(ticks: list[dict]) -> str:
    """A short digest of every tick, joined: a cache key that changes with any tick's content.

    The stream computes each tick's digest once, when it arrives; hashing the whole buffer
    on every page refresh cost hundreds of milliseconds per viewer.
    """
    return ",".join(_digest(tick) for tick in ticks)


class RealtimeStream:
    def __init__(self, url: str, token: str, summary_url: str | None = None, maxlen: int = 600) -> None:
        self.url = url  # the token goes in a header, so it is not in any URL
        self.token = token
        self.summary_url = summary_url
        self._buffer: deque[dict] = deque(maxlen=maxlen)
        self._digests: dict[datetime, str] = {}  # one per tick in the buffer, computed on arrival
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._last_read = time.monotonic()
        self.status = "connecting"
        self.latest_raw: list[dict] | None = None
        self._latest_raw_at: datetime | None = None
        self._thread = threading.Thread(target=self._run, name="realtime-ws", daemon=True)

    @property
    def alive(self) -> bool:
        return self._thread.is_alive()

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def extend(self, ticks: list[dict]) -> None:
        """Merge ticks into the buffer by time; a tick already there is kept once."""
        with self._lock:
            self._merge(ticks)

    def _merge(self, ticks: list[dict]) -> None:
        """Shared history/live merge, called while holding the buffer lock."""
        for tick in ticks:
            self._digests[_at(tick)] = _digest(tick)
        merged = {_at(t): t for t in [*self._buffer, *ticks]}
        self._buffer = deque((merged[at] for at in sorted(merged)), maxlen=self._buffer.maxlen)
        self._digests = {at: self._digests[at] for at in map(_at, self._buffer)}

    def _fill_gap(self) -> None:
        """Fetch the ticks missed while disconnected, so the charts have no hole."""
        if self.summary_url is None:
            return
        with self._lock:
            ticks = list(self._buffer)
        # The newest tick arrived on this connection; the gap is before it.
        previous = _at(ticks[-2]) if len(ticks) > 1 else None
        seconds = MAX_FILL_SECONDS if previous is None else (_at(ticks[-1]) - previous).total_seconds() + 2
        if seconds <= 3:
            return  # no tick missed
        try:
            response = requests.get(self.summary_url, params={"seconds": int(min(seconds, MAX_FILL_SECONDS))},
                                    headers={"Authorization": f"Bearer {self.token}"}, timeout=5)
            response.raise_for_status()
        except requests.RequestException:
            return  # the live stream still works; the hole stays
        self.extend(response.json())

    def latest_readings(self) -> list[dict] | None:
        """Per server and metric, the newest tick's sensor nearest its limit; None when the fleet is too large to push them."""
        with self._lock:
            return self.latest_raw

    def snapshot(self) -> list[dict]:
        self._last_read = time.monotonic()
        with self._lock:
            return list(self._buffer)

    def snapshot_keyed(self) -> tuple[list[dict], str]:
        """The buffer and its fingerprint, taken together, so the key always matches the ticks."""
        self._last_read = time.monotonic()
        with self._lock:
            return list(self._buffer), ",".join(self._digests[_at(tick)] for tick in self._buffer)

    def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            if time.monotonic() - self._last_read > IDLE_TIMEOUT:
                self.status = "idle"  # nobody is reading: stop retrying for a closed tab
                return
            try:
                with connect(self.url, additional_headers={"Authorization": f"Bearer {self.token}"},
                             open_timeout=5, close_timeout=1) as ws:
                    self.status = "connecting"
                    while not self._stop.is_set():
                        if time.monotonic() - self._last_read > IDLE_TIMEOUT:
                            self.status = "idle"
                            return
                        try:
                            message = json.loads(ws.recv(timeout=1))
                        except TimeoutError:
                            continue
                        if message.get("type") == "tick":
                            # Only now is the stream really up: the server can accept and
                            # close at once with 1013 while its database is down.
                            first = self.status != "connected"
                            self.status = "connected"
                            backoff = 1.0
                            # Charts use the summaries; only the newest tick's per-server readings are kept.
                            raw = message.pop("server_readings", None)
                            if raw is not None:
                                raw = _complete(raw, message)
                            with self._lock:
                                self._merge([message])
                                at = _at(message)
                                if self._latest_raw_at is None or at >= self._latest_raw_at:
                                    self.latest_raw = raw
                                    self._latest_raw_at = at
                            if first:
                                self._fill_gap()
            except ConnectionClosed as exc:
                # 1008: the server's re-check found the token ended (logout, expiry, disabled account).
                if exc.rcvd is not None and exc.rcvd.code == 1008:
                    self.status = "unauthorized"
                    return
                self.status = f"reconnecting ({exc.__class__.__name__})"
            except InvalidStatus as exc:
                if exc.response.status_code in (401, 403):
                    self.status = "unauthorized"
                    return
                self.status = f"rejected (HTTP {exc.response.status_code}); retrying"
            except Exception as exc:  # noqa: BLE001 - reconnect on any network error
                self.status = f"reconnecting ({exc.__class__.__name__})"
            self._stop.wait(backoff)
            backoff = min(backoff * 2, 10.0)
