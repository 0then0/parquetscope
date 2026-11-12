"""Capture-local HTTP response shaping, immediately before stream body writes.

No future reservations or waiter queue are retained. The bounded connection budget bounds
threads competing for one token bucket. A reservation is spent even if its write fails:
a successful write records stream acceptance, never acknowledged network delivery.
"""

import math
import threading
import time
from collections.abc import Callable
from typing import Literal

from pydantic import Field, model_validator

from .models import ByteInt, Model

MAX_WRITE_CHUNK = 16384
POLL_SECONDS = 0.05


class HTTPProfile(Model):
    response_delay_ms: int = Field(default=0, strict=True, ge=0, le=60000)
    body_bytes_per_second: int | None = Field(default=None, strict=True, ge=1, le=1000000000)


class RecordedHTTPProfile(HTTPProfile):
    """Artifact evidence must state both values; only configuration may use defaults."""

    response_delay_ms: int = Field(strict=True, ge=0, le=60000)
    body_bytes_per_second: int | None = Field(strict=True, ge=1, le=1000000000)


class HTTPProfileEvidence(Model):
    requested: RecordedHTTPProfile
    effective: RecordedHTTPProfile
    semantics: Literal["capture_shared_token_bucket_v1"]
    burst_bytes: ByteInt
    write_chunk_bytes: int = Field(ge=1, le=65536)

    @model_validator(mode="after")
    def consistent(self):
        if self.requested != self.effective:
            raise ValueError("Requested and effective HTTP profiles must agree")
        rate = self.effective.body_bytes_per_second
        burst = max(1, min(MAX_WRITE_CHUNK, math.ceil(rate / 10))) if rate else 0
        if int(self.burst_bytes) != burst or self.write_chunk_bytes != (burst or 65536):
            raise ValueError("HTTP limiter burst or chunk size disagrees with its semantics")
        return self


def profile_evidence(profile: HTTPProfile) -> HTTPProfileEvidence:
    rate = profile.body_bytes_per_second
    burst = max(1, min(MAX_WRITE_CHUNK, math.ceil(rate / 10))) if rate else 0
    return HTTPProfileEvidence(
        requested=RecordedHTTPProfile(**profile.model_dump()),
        effective=RecordedHTTPProfile(**profile.model_dump()),
        semantics="capture_shared_token_bucket_v1",
        burst_bytes=str(burst),
        write_chunk_bytes=burst or 65536,
    )


class TransferInterrupted(Exception):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


class BodyLimiter:
    """A shared full token bucket; reservations across any interval <= burst + rate*time.

    Waiting happens outside the lock. Only current tokens can be spent, so concurrent
    callers cannot build unbounded scheduled reservations. `clock` is monotonic and may
    be replaced by a controlled clock in algorithm tests.
    """

    def __init__(self, rate: int, burst: int, clock: Callable[[], float] = time.monotonic):
        self.rate = rate
        self.burst = burst
        self.clock = clock
        self.tokens = float(burst)
        self.updated = clock()
        self.lock = threading.Lock()
        self.write_lock = threading.Lock()

    def try_reserve(self, amount: int) -> float:
        """Spend tokens and return zero, or return seconds until this chunk can fit."""
        if not 0 < amount <= self.burst:
            raise ValueError("Reservation must fit within the burst allowance")
        with self.lock:
            now = self.clock()
            self.tokens = min(self.burst, self.tokens + max(0, now - self.updated) * self.rate)
            self.updated = now
            if self.tokens >= amount:
                self.tokens -= amount
                return 0
            return (amount - self.tokens) / self.rate

    def reserve(self, amount: int, check: Callable[[], None], wait: Callable[[float], None]):
        while True:
            check()
            delay = self.try_reserve(amount)
            if delay == 0:
                return
            wait(min(delay, POLL_SECONDS))

    def begin_write(self, amount: int, check: Callable[[], None], wait: Callable[[float], None]):
        """Admit one immediate write; never hold a lock during budget or lock waiting."""
        while True:
            check()
            if not self.write_lock.acquire(blocking=False):
                wait(POLL_SECONDS)
                continue
            try:
                delay = self.try_reserve(amount)
            except BaseException:
                self.write_lock.release()
                raise
            if delay == 0:
                return
            self.write_lock.release()
            wait(min(delay, POLL_SECONDS))

    def finish_write(self):
        # Pause refill while the stream write was in flight. Otherwise an old delayed
        # reservation plus newly refilled tokens could create another full burst.
        with self.lock:
            self.updated = self.clock()
        self.write_lock.release()
