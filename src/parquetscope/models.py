"""Portable v1 artifact. Byte quantities use decimal strings, never JSON numbers."""

import json
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_ARTIFACT_BYTES = 32 * 1024 * 1024
ByteInt = Annotated[str, Field(pattern=r"^(0|[1-9][0-9]{0,39})$")]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Span(Model):
    start: ByteInt
    end: ByteInt

    @model_validator(mode="after")
    def ordered(self):
        if int(self.end) <= int(self.start):
            raise ValueError("Intervals must be nonempty and half-open")
        return self

    def pair(self):
        return int(self.start), int(self.end)


class Structure(Model):
    id: str
    kind: Literal["header", "footer", "column_chunk", "unclassified"]
    label: str
    span: Span
    row_group: int | None = Field(default=None, ge=0)
    column: str | None = None
    rows: ByteInt | None = None
    dictionary_offset: ByteInt | None = None
    data_offset: ByteInt | None = None
    compression: str | None = None


class Input(Model):
    name: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size: ByteInt
    rows: ByteInt
    row_groups: int = Field(ge=0)
    schema_fields: list[str]
    structures: list[Structure] = Field(max_length=100000)

    @model_validator(mode="after")
    def bounds(self):
        if len({s.id for s in self.structures}) != len(self.structures):
            raise ValueError("Duplicate structure IDs")
        if any(int(s.span.end) > int(self.size) for s in self.structures):
            raise ValueError("Structure outside input")
        return self


class HTTPObservation(Model):
    id: str
    method: Literal["GET", "HEAD"]
    url: str
    requested_range: str | None
    requested_span: Span | None
    response_span: Span | None
    status: int = Field(ge=100, le=599)
    start_ms: float = Field(ge=0)
    duration_ms: float = Field(ge=0)
    declared_body_bytes: ByteInt | None
    request_headers: dict[str, str]
    response_headers: dict[str, str]
    engine_start_time: str


class ServerObservation(Model):
    id: str
    method: Literal["GET", "HEAD"]
    requested_range: str | None
    response_span: Span | None
    status: int = Field(ge=100, le=599)
    start_ms: float = Field(ge=0)
    duration_ms: float = Field(ge=0)
    declared_body_bytes: ByteInt
    written_body_bytes: ByteInt
    complete: bool
    error: str | None


class Capture(Model):
    complete: bool
    reason: str | None
    reconciled: bool
    scope: str = "Input binding and one fully consumed SELECT; app metadata reads excluded."


class Metrics(Model):
    get_count: int = Field(ge=0)
    head_count: int = Field(ge=0)
    requested_range_bytes: ByteInt | None
    confirmed_range_bytes: ByteInt | None
    declared_body_bytes: ByteInt | None
    server_written_bytes: ByteInt
    unique_coverage_bytes: ByteInt | None
    repeated_coverage_bytes: ByteInt | None
    coverage: list[Span] | None
    totals_scope: Literal["complete", "observed_subset"]
    unknown_reasons: list[str]


class Settings(Model):
    threads: int = Field(ge=1, le=16)
    memory_limit_mb: int = Field(ge=64, le=4096)
    deadline_seconds: float = Field(gt=0, le=300)
    preview_rows: int = Field(ge=0, le=100)
    preview_bytes: Literal[65536] = 65536
    external_file_cache: Literal[False] = False
    cold_client: Literal[True] = True


class Run(Model):
    schema_version: Literal["1"] = "1"
    app_version: str
    duckdb_version: Literal["1.5.6"]
    pyarrow_version: str
    platform: str
    id: str
    created_at: str
    sql: str = Field(max_length=65536)
    input: Input
    settings: Settings
    sql_status: Literal["succeeded", "failed", "cancelled", "timeout"]
    query_duration_ms: float | None = Field(default=None, ge=0)
    elapsed_ms: float = Field(ge=0)
    result_rows: ByteInt | None
    result_digest: str | None
    observations: list[HTTPObservation] = Field(max_length=100000)
    server_observations: list[ServerObservation] = Field(max_length=100000)
    capture: Capture
    metrics: Metrics
    errors: list[str]
    limitations: list[str]

    @model_validator(mode="after")
    def bounds(self):
        size = int(self.input.size)
        for observations in (self.observations, self.server_observations):
            if len({o.id for o in observations}) != len(observations):
                raise ValueError("Duplicate observation IDs")
            for o in observations:
                if o.response_span and int(o.response_span.end) > size:
                    raise ValueError("Response interval outside input")
        if self.capture.complete:
            from .analysis import reconcile

            if (
                not self.capture.reconciled
                or self.capture.reason is not None
                or not all(o.complete for o in self.server_observations)
                or not reconcile(self.observations, self.server_observations)
            ):
                raise ValueError("Complete capture must have reconciled, complete observations")
        return self


def load_artifact(data: bytes) -> Run:
    if len(data) > MAX_ARTIFACT_BYTES:
        raise ValueError("Artifact exceeds 32 MiB limit")
    try:
        run = Run.model_validate_json(data)
        from .analysis import derive_metrics

        expected = derive_metrics(run.observations, run.server_observations, run.capture)
        if run.metrics != expected:
            raise ValueError("Artifact metrics disagree with observations")
        return run
    except (ValueError, RecursionError) as error:
        raise ValueError(f"Invalid or unsupported ParquetScope artifact: {error}") from error


def save_artifact(run: Run, path: Path):
    data = run.model_dump_json(indent=2).encode()
    if len(data) > MAX_ARTIFACT_BYTES:
        raise ValueError("Artifact exceeds 32 MiB limit")
    path.write_bytes(data)


def load_artifact_file(path: Path) -> Run:
    # Reject huge files without first allocating their complete contents.
    with path.open("rb") as source:
        return load_artifact(source.read(MAX_ARTIFACT_BYTES + 1))


def span(start: int, end: int) -> Span:
    return Span(start=str(start), end=str(end))


def json_value(value):
    """For runtime preview only; strings preserve decimals, integers and binary values."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (str, int, float)):
        return str(value)
    return json.dumps(value, default=str, ensure_ascii=False)
