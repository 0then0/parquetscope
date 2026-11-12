"""Explicit, bounded file-set manifests.

Stage 1 (`load_manifest`) is pure validation of the JSON contract and of paths; it never reads
Parquet bytes. Stage 2 (`resolve_set`) hashes every file and reads its footer. Both run before
any worker or HTTP listener exists. Nothing is discovered while a query runs.
"""

import json
import os
import re
import threading
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import pyarrow.parquet as pq
from pydantic import Field, ValidationError

from .metadata import identity, read_metadata
from .models import ByteInt, Model
from .models_v2 import (
    MAX_SET_FILES,
    MAX_SET_STRUCTURES,
    FileInput,
    FileSet,
    PartitionSegment,
    ReadOptions,
    file_id_for,
    partition_pairs,
    split_logical_path,
)

MAX_MANIFEST_BYTES = 1024 * 1024
MAX_SET_BYTES = 16 * 1024**3
HIVE_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class PreparationCancelled(ValueError):
    """Preparation stopped before metadata was complete and a capture could exist."""


class ManifestFile(Model):
    path: str = Field(max_length=2048)
    size: ByteInt | None = None
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class ManifestRead(Model):
    hive_partitioning: bool = False


class Manifest(Model):
    manifest_version: Literal["1"]
    label: str | None = Field(default=None, max_length=128)
    read: ManifestRead = Field(default_factory=ManifestRead)
    files: list[ManifestFile] = Field(min_length=1, max_length=MAX_SET_FILES)


@dataclass(frozen=True)
class SetEntry:
    logical_path: str
    source: Path  # real path, inside the manifest directory
    size: int
    pinned_size: str | None
    pinned_sha256: str | None


@dataclass(frozen=True)
class SetSpec:
    manifest_path: Path
    label: str
    read: ReadOptions
    entries: tuple[SetEntry, ...]

    @property
    def name(self) -> str:
        return self.label


@dataclass(frozen=True)
class ResolvedFile:
    file: FileInput
    source: Path
    identity: tuple


@dataclass(frozen=True)
class ResolvedSet:
    label: str | None
    read: ReadOptions
    files: tuple[ResolvedFile, ...]

    def file_set(self) -> FileSet:
        return FileSet(label=self.label, read=self.read, files=[f.file for f in self.files])


def _no_duplicate_keys(pairs):
    if len({key for key, _ in pairs}) != len(pairs):
        raise ValueError("Manifest contains a duplicate JSON key")
    return dict(pairs)


def _reject_constant(value):
    raise ValueError(f"Manifest contains non-standard JSON constant {value}")


def _describe(error: ValidationError) -> str:
    first = error.errors()[0]
    where = ".".join(str(part) for part in first["loc"])
    return f"{where or 'manifest'}: {first['msg']}"


def load_manifest(path: Path) -> SetSpec:
    try:
        size = path.stat().st_size
        if size > MAX_MANIFEST_BYTES:
            raise ValueError("Manifest exceeds 1 MiB limit")
        with path.open("rb") as source:
            data = source.read(MAX_MANIFEST_BYTES + 1)
    except OSError as error:
        raise ValueError(f"Cannot read manifest {path.name}: {error.strerror}") from error
    if len(data) > MAX_MANIFEST_BYTES:
        raise ValueError("Manifest exceeds 1 MiB limit")
    try:
        value = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_no_duplicate_keys,
            parse_constant=_reject_constant,
        )
        manifest = Manifest.model_validate(value)
    except (UnicodeDecodeError, RecursionError) as error:
        raise ValueError("Manifest is not valid UTF-8 JSON") from error
    except json.JSONDecodeError as error:
        raise ValueError(f"Manifest is not valid JSON: {error.msg}") from error
    except ValidationError as error:
        raise ValueError(f"Invalid manifest: {_describe(error)}") from error
    root = Path(os.path.realpath(path.parent))
    entries: list[SetEntry] = []
    logical_seen: set[str] = set()
    inode_seen: dict[tuple, str] = {}
    total = 0
    for index, entry in enumerate(manifest.files):
        where = f"files[{index}]"
        logical = unicodedata.normalize("NFC", entry.path)
        try:
            split_logical_path(logical)
        except ValueError as error:
            raise ValueError(f"{where}: {error}") from error
        if manifest.read.hive_partitioning:
            for key, _value in partition_pairs(logical):
                if not HIVE_KEY.fullmatch(key):
                    raise ValueError(
                        f"{where}: partition key {key!r} must use ASCII letters, digits and "
                        "underscores when hive_partitioning is on; DuckDB does not decode "
                        "percent-encoded keys"
                    )
        if logical in logical_seen:
            raise ValueError(f"{where}: duplicate path {logical!r}")
        logical_seen.add(logical)
        real = Path(os.path.realpath(root / entry.path))
        if real != root and root not in real.parents:
            raise ValueError(f"{where}: {logical!r} resolves outside the manifest directory")
        try:
            stat = real.stat()
        except OSError as error:
            raise ValueError(f"{where}: {logical!r} cannot be read: {error.strerror}") from error
        if not real.is_file():
            raise ValueError(f"{where}: {logical!r} is not a regular file")
        key = (stat.st_dev, stat.st_ino)
        if key in inode_seen:
            raise ValueError(
                f"{where}: {logical!r} is the same file as {inode_seen[key]!r}; "
                "each file may appear once"
            )
        inode_seen[key] = logical
        total += stat.st_size
        if total > MAX_SET_BYTES:
            raise ValueError("File set exceeds the 16 GiB limit")
        entries.append(SetEntry(logical, real, stat.st_size, entry.size, entry.sha256))
    return SetSpec(
        manifest_path=path,
        label=manifest.label or path.stem[:128],
        read=ReadOptions(hive_partitioning=manifest.read.hive_partitioning),
        entries=tuple(entries),
    )


def _comparable_schema(path: Path):
    return [(field.name, field.type) for field in pq.read_schema(path)]


def resolve_set(spec: SetSpec, cancel: threading.Event | None = None) -> ResolvedSet:
    """Read identity, size and layout of every file once; reject unsupported sets early."""
    resolved: list[ResolvedFile] = []
    structures = 0
    reference = None
    for index, entry in enumerate(spec.entries):
        if cancel is not None and cancel.is_set():
            raise PreparationCancelled("Run cancelled while preparing the file set")
        try:
            prepared, file_identity = read_metadata(entry.source)
        except (OSError, ValueError) as error:
            raise ValueError(f"{entry.logical_path}: {error}") from error
        if entry.pinned_size is not None and entry.pinned_size != prepared.size:
            raise ValueError(
                f"{entry.logical_path}: size {prepared.size} differs from the manifest "
                f"({entry.pinned_size}); the file changed after the manifest was written"
            )
        if entry.pinned_sha256 is not None and entry.pinned_sha256 != prepared.sha256:
            raise ValueError(
                f"{entry.logical_path}: SHA-256 differs from the manifest; "
                "the file changed after the manifest was written"
            )
        schema = _comparable_schema(entry.source)
        if reference is None:
            reference = schema
        elif schema != reference:
            raise ValueError(
                f"{entry.logical_path}: schema differs from the first file (column names, "
                "order or types). Schema evolution and union_by_name are not supported."
            )
        structures += len(prepared.structures)
        if structures > MAX_SET_STRUCTURES:
            raise ValueError("File set exceeds the 100,000 structure limit")
        name = entry.logical_path.rsplit("/", 1)[-1]
        file = FileInput(
            **prepared.model_dump(exclude={"name"}),
            name=name,
            id=file_id_for(index),
            logical_path=entry.logical_path,
            partition=[
                PartitionSegment(key=k, value=v) for k, v in partition_pairs(entry.logical_path)
            ],
        )
        resolved.append(ResolvedFile(file, entry.source, file_identity))
    for item in resolved:
        if identity(item.source.stat()) != item.identity:
            raise ValueError(f"{item.file.logical_path}: changed while the set was prepared")
    return ResolvedSet(spec.label, spec.read, tuple(resolved))
