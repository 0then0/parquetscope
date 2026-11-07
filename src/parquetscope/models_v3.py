"""Portable v3 artifact: a v2 file-set capture plus the DuckDB version and prefetch evidence.

v1 and v2 stay in `models.py` and `models_v2.py` and keep loading unchanged. v3 differs from v2
in exactly these ways:

- `duckdb_version` admits every supported engine, not only 1.5.6;
- `settings.disable_parquet_prefetching` records the requested value (null: left at the default);
- `engine` records where the worker ran, how requests were correlated, and the prefetch evidence.
"""

from typing import Literal

from pydantic import Field, model_validator

from .models import Model, Settings
from .models_v2 import Engine, SetRun

PrefetchStatus = Literal["engine_default", "verified", "unverified"]
Correlation = Literal["response_header_id", "ordered_tuple_match"]


class Settings3(Settings):
    # None: not set by the capture, the engine's own default applies.
    disable_parquet_prefetching: bool | None = None


class PrefetchEvidence(Model):
    """`applied` is read back from the engine after the SET; null when it could not be read."""

    requested: bool | None
    applied: Literal["true", "false"] | None
    status: PrefetchStatus

    @model_validator(mode="after")
    def consistent(self):
        if self.status == "unverified":
            return self
        if self.applied is None:
            raise ValueError("A verified or default prefetch setting must record its value")
        if self.status == "verified" and self.applied != str(self.requested).lower():
            raise ValueError("Verified prefetch setting disagrees with the request")
        if self.status == "engine_default" and self.requested is not None:
            raise ValueError("A requested prefetch setting is verified or unverified")
        return self


class Engine3(Engine):
    environment: Literal["project", "isolated"]
    python_version: str = Field(max_length=32)
    # response_header_id: the engine echoed the server's request ID header.
    # ordered_tuple_match: it did not (DuckDB 2.0 redacts it); requests were paired by file,
    # method, range, status, response interval and length, in start order.
    request_correlation: Correlation
    prefetch: PrefetchEvidence


class SetRun3(SetRun):
    schema_version: Literal["3"] = "3"  # type: ignore[assignment]
    duckdb_version: Literal["1.5.6", "2.0.0"]  # type: ignore[assignment]
    settings: Settings3  # type: ignore[assignment]
    engine: Engine3  # type: ignore[assignment]

    @model_validator(mode="after")
    def prefetch_matches_settings(self):
        if self.settings.disable_parquet_prefetching != self.engine.prefetch.requested:
            raise ValueError("Settings and engine evidence disagree about the prefetch request")
        if self.capture.complete and self.engine.prefetch.status == "unverified":
            raise ValueError("Complete capture needs a verified prefetch setting")
        return self
