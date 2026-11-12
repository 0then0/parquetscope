"""Portable HTTP server profile and bounded per-request timing evidence.

Historical v1-v3 contracts are unchanged. A missing profile in those versions is unknown,
not evidence of unrestricted service.
"""

from typing import Literal

from pydantic import Field, model_validator

from .http_profile import HTTPProfileEvidence
from .models import ByteInt, ServerObservation
from .models_v2 import FILE_ID_PATTERN, MAX_OBSERVATIONS
from .models_v3 import SetRun3


class ServerObservation4(ServerObservation):
    profile_delay_started_ms: float | None = Field(ge=0)
    profile_delay_ms: float = Field(ge=0)
    headers_sent_ms: float | None = Field(ge=0)
    body_budget_wait_ms: float = Field(ge=0)
    body_write_started_ms: float | None = Field(ge=0)
    body_write_finished_ms: float | None = Field(ge=0)
    body_write_ms: float = Field(ge=0)
    reserved_body_bytes: ByteInt
    termination: Literal["completed", "cancelled", "timeout", "disconnect", "write_error"]

    @model_validator(mode="after")
    def timing_and_bytes(self):
        # Allow floating-point roundoff, not scheduling tolerances, for one clock's bounds.
        epsilon = 0.001
        end = self.start_ms + self.duration_ms
        times = [
            self.profile_delay_started_ms,
            self.headers_sent_ms,
            self.body_write_started_ms,
            self.body_write_finished_ms,
        ]
        recorded = [t for t in times if t is not None]
        if any(t < self.start_ms - epsilon or t > end + epsilon for t in recorded):
            raise ValueError("Server timing outside request duration")
        if recorded != sorted(recorded):
            raise ValueError("Server timestamps are out of order")
        if (self.body_write_started_ms is None) != (self.body_write_finished_ms is None):
            raise ValueError("Body write boundaries must both be recorded")
        if self.body_write_ms and self.body_write_started_ms is None:
            raise ValueError("Body write duration needs write boundaries")
        if self.body_write_started_ms is not None and self.body_write_ms > (
            self.body_write_finished_ms - self.body_write_started_ms + epsilon
        ):
            raise ValueError("Body write duration exceeds its boundaries")
        if self.profile_delay_ms and self.profile_delay_started_ms is None:
            raise ValueError("Profile delay needs its start timestamp")
        if self.profile_delay_started_ms is not None and (
            self.profile_delay_started_ms + self.profile_delay_ms > end + epsilon
        ):
            raise ValueError("Profile delay exceeds request duration")
        if self.profile_delay_ms + self.body_budget_wait_ms + self.body_write_ms > (
            self.duration_ms + epsilon
        ):
            raise ValueError("Sequential timing components exceed request duration")
        written, reserved, declared = map(
            int, (self.written_body_bytes, self.reserved_body_bytes, self.declared_body_bytes)
        )
        if not written <= reserved <= declared:
            raise ValueError("Expected written <= reserved <= declared body bytes")
        if self.method == "HEAD" and (declared or reserved or written):
            raise ValueError("HEAD does not transfer body bytes")
        if self.complete != (self.termination == "completed"):
            raise ValueError("Completion disagrees with server termination")
        if self.complete and (written != declared or self.headers_sent_ms is None):
            raise ValueError("Completed response needs headers and all declared body writes")
        return self


class FileServerObservation4(ServerObservation4):
    file_id: str = Field(pattern=FILE_ID_PATTERN)


class SetRun4(SetRun3):
    schema_version: Literal["4"] = "4"  # type: ignore[assignment]
    http_profile: HTTPProfileEvidence
    server_observations: list[FileServerObservation4] = Field(  # type: ignore[assignment]
        max_length=MAX_OBSERVATIONS
    )
