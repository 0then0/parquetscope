"""Portable v2 artifact for a bounded set of Parquet files read through one `input` view.

v1 (single file) stays in `models.py` and keeps loading unchanged. Every interval here is keyed
by `file_id` plus offset: byte ranges of different files are never merged or compared.
"""

import unicodedata
from typing import Literal

from pydantic import Field, model_validator

from .models import (
    ByteInt,
    Capture,
    HTTPObservation,
    Input,
    Model,
    ServerObservation,
    Settings,
    Span,
)

MAX_SET_FILES = 256
MAX_SET_STRUCTURES = 100000
MAX_OBSERVATIONS = 100000
MAX_LOGICAL_PATH_BYTES = 1024
MAX_SEGMENT_BYTES = 255
MAX_SEGMENTS = 32
MAX_PARTITION_SEGMENTS = 31
FILE_ID_PATTERN = r"^f[0-9]{3}$"
Phase = Literal["view_creation", "select", "unattributed"]
ObservedAccess = Literal["none", "head_only", "get"]
# Control, format (bidi overrides), surrogate, private-use, unassigned, line/paragraph separators.
_FORBIDDEN_CATEGORIES = {"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"}


def file_id_for(index: int) -> str:
    return f"f{index + 1:03d}"


def split_logical_path(value: str) -> list[str]:
    """Validate a relative POSIX path that is both the manifest location and the URL route."""
    if not value or len(value.encode("utf-8", "surrogatepass")) > MAX_LOGICAL_PATH_BYTES:
        raise ValueError(f"Path must be 1 to {MAX_LOGICAL_PATH_BYTES} UTF-8 bytes")
    if unicodedata.normalize("NFC", value) != value:
        raise ValueError("Path must be Unicode NFC-normalized")
    if any(unicodedata.category(c) in _FORBIDDEN_CATEGORIES for c in value):
        raise ValueError("Path contains control or invisible formatting characters")
    if "\\" in value or value.startswith("/"):
        raise ValueError("Path must be relative and use '/' separators")
    segments = value.split("/")
    if len(segments) > MAX_SEGMENTS:
        raise ValueError(f"Path has more than {MAX_SEGMENTS} segments")
    for segment in segments:
        if segment in ("", ".", ".."):
            raise ValueError("Path must not contain empty, '.' or '..' segments")
        if len(segment.encode("utf-8")) > MAX_SEGMENT_BYTES:
            raise ValueError(f"Path segment exceeds {MAX_SEGMENT_BYTES} bytes")
    return segments


def partition_pairs(logical_path: str) -> list[tuple[str, str]]:
    """Directory segments written as key=value. This is path syntax, not DuckDB's schema."""
    pairs = []
    for segment in split_logical_path(logical_path)[:-1]:
        key, separator, value = segment.partition("=")
        if separator and key:
            pairs.append((key, value))
    return pairs


class PartitionSegment(Model):
    key: str = Field(min_length=1, max_length=255)
    value: str = Field(max_length=255)


class FileInput(Input):
    """One resolved file. `name` is the basename; `logical_path` is the manifest location."""

    id: str = Field(pattern=FILE_ID_PATTERN)
    logical_path: str = Field(max_length=MAX_LOGICAL_PATH_BYTES)
    partition: list[PartitionSegment] = Field(max_length=MAX_PARTITION_SEGMENTS)

    @model_validator(mode="after")
    def location(self):
        segments = split_logical_path(self.logical_path)
        if segments[-1] != self.name:
            raise ValueError("File name disagrees with its logical path")
        expected = [PartitionSegment(key=k, value=v) for k, v in partition_pairs(self.logical_path)]
        if self.partition != expected:
            raise ValueError("Partition segments disagree with the logical path")
        return self


class ReadOptions(Model):
    """Parameters of read_parquet that change which rows or columns the view exposes."""

    hive_partitioning: bool
    union_by_name: Literal[False] = False


class FileSet(Model):
    label: str | None = Field(default=None, max_length=128)
    read: ReadOptions
    files: list[FileInput] = Field(min_length=1, max_length=MAX_SET_FILES)

    @model_validator(mode="after")
    def composition(self):
        if [f.id for f in self.files] != [file_id_for(i) for i in range(len(self.files))]:
            raise ValueError("File IDs must be f001, f002, … in set order")
        if len({f.logical_path for f in self.files}) != len(self.files):
            raise ValueError("Duplicate logical paths")
        if sum(len(f.structures) for f in self.files) > MAX_SET_STRUCTURES:
            raise ValueError("File set exceeds the 100,000 structure limit")
        return self


class BoundColumn(Model):
    name: str = Field(max_length=255)
    type: str = Field(max_length=255)


class Engine(Model):
    """Effective engine conditions. Missing values are null, never guessed."""

    httpfs_version: str | None = Field(max_length=64)
    effective_settings: dict[str, str] = Field(max_length=32)
    bound_schema: list[BoundColumn] | None = Field(max_length=1024)


class FileHTTPObservation(HTTPObservation):
    file_id: str = Field(pattern=FILE_ID_PATTERN)
    phase: Phase


class FileServerObservation(ServerObservation):
    file_id: str = Field(pattern=FILE_ID_PATTERN)


class FileMetrics(Model):
    file_id: str = Field(pattern=FILE_ID_PATTERN)
    observed_access: ObservedAccess
    get_count: int = Field(ge=0)
    head_count: int = Field(ge=0)
    requested_range_bytes: ByteInt | None
    confirmed_range_bytes: ByteInt | None
    declared_body_bytes: ByteInt | None
    server_written_bytes: ByteInt
    unique_coverage_bytes: ByteInt | None
    repeated_coverage_bytes: ByteInt | None
    coverage: list[Span] | None
    unknown_reasons: list[str]


class SetMetrics(Model):
    files_without_observed_access: int = Field(ge=0)
    files_head_only: int = Field(ge=0)
    files_with_get: int = Field(ge=0)
    get_count: int = Field(ge=0)
    head_count: int = Field(ge=0)
    requested_range_bytes: ByteInt | None
    confirmed_range_bytes: ByteInt | None
    declared_body_bytes: ByteInt | None
    server_written_bytes: ByteInt
    unique_coverage_bytes: ByteInt | None
    repeated_coverage_bytes: ByteInt | None
    totals_scope: Literal["complete", "observed_subset"]
    unknown_reasons: list[str]
    files: list[FileMetrics] = Field(max_length=MAX_SET_FILES)


class SetRun(Model):
    schema_version: Literal["2"] = "2"
    app_version: str
    duckdb_version: Literal["1.5.6"]
    pyarrow_version: str
    platform: str
    id: str
    created_at: str
    sql: str = Field(max_length=65536)
    file_set: FileSet
    settings: Settings
    engine: Engine
    sql_status: Literal["succeeded", "failed", "cancelled", "timeout"]
    query_duration_ms: float | None = Field(default=None, ge=0)
    elapsed_ms: float = Field(ge=0)
    result_rows: ByteInt | None
    result_digest: str | None
    observations: list[FileHTTPObservation] = Field(max_length=MAX_OBSERVATIONS)
    server_observations: list[FileServerObservation] = Field(max_length=MAX_OBSERVATIONS)
    capture: Capture
    metrics: SetMetrics
    errors: list[str]
    limitations: list[str]

    @model_validator(mode="after")
    def bounds(self):
        sizes = {f.id: int(f.size) for f in self.file_set.files}
        for observations in (self.observations, self.server_observations):
            if len({o.id for o in observations}) != len(observations):
                raise ValueError("Duplicate observation IDs")
            for o in observations:
                if o.file_id not in sizes:
                    raise ValueError("Observation refers to an unknown file")
                if o.response_span and int(o.response_span.end) > sizes[o.file_id]:
                    raise ValueError("Response interval outside its file")
        if self.capture.complete:
            from .analysis import reconcile_set

            if (
                not self.capture.reconciled
                or self.capture.reason is not None
                or not all(o.complete for o in self.server_observations)
                or not reconcile_set(self.observations, self.server_observations)
            ):
                raise ValueError("Complete capture must have reconciled, complete observations")
        return self
