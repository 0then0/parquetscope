"""DataFusion page-index cache lab (apache/datafusion#23978).

One small Parquet file is opened twice, sequentially, through DataFusion's Parquet opener with
one shared `FileMetadataCache` (`CachedParquetFileReaderFactory`). The Rust harness under
`scripts/datafusion_page_index_harness/` is built once per pinned DataFusion version (54.0.0 and
54.1.0). The file is served by ParquetScope's `RangeServer`; its observations are attributed to
an open by explicit begin/end boundaries and matched against the `ColumnIndex` and `OffsetIndex`
regions this script extracts from the footer with its own Thrift reader.

    uv run python scripts/datafusion_page_index_lab.py --output docs/datafusion-page-index-lab

The fixture, its metadata checks and the expected results are produced before any server
exists; the footer reads of that preparation are not part of any measured trace.
"""

import argparse
import hashlib
import importlib.metadata
import json
import platform
import random
import re
import shutil
import struct
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from parquetscope.intervals import coverage, union
from parquetscope.metadata import identity
from parquetscope.models_v4 import ServerObservation4
from parquetscope.range_server import RangeServer

ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / "scripts" / "datafusion_page_index_harness"
TARGETS = ROOT / ".runtime" / "datafusion-page-index-lab"
VERSIONS = ("54.0.0", "54.1.0")
SEED = 23978
ROWS = 100_000
ROW_GROUP_SIZE = 25_000
DATA_PAGE_SIZE = 32 * 1024
STATISTICS_COLUMNS = ["id", "val"]  # `fk` is written without any column-chunk statistics
# Same predicate shape for main and control; only the column metadata differs.
SCENARIOS = {
    "main": {
        "predicate": "fk:ge_zero",
        "sql": "fk >= 0",
        "meaning": "fk has no row-group statistics (no min/max, no null_count), so no row "
        "group can be proved fully matched and 54.1.0 has to load the page index.",
    },
    "control": {
        "predicate": "id:ge_zero",
        "sql": "id >= 0",
        "meaning": "id has min >= 0 and null_count = 0 in every row group, so every row group "
        "is proved fully matched and the 54.1.0 skip heuristic applies.",
    },
    "is_not_null": {
        "predicate": "id:is_not_null",
        "sql": "id IS NOT NULL",
        "meaning": "Supplementary: the predicate shape of the issue on a column with "
        "null_count = 0. Recorded to show whether the skip heuristic applies to it.",
    },
}
QUIESCE_SECONDS = 5.0
RUN_TIMEOUT_SECONDS = 120.0


# --- Fixture -------------------------------------------------------------------------------


def generate_fixture(path: Path, rows: int = ROWS, row_group_size: int = ROW_GROUP_SIZE) -> dict:
    """Write the fixture and return the expected results computed from the Python values."""
    rng = random.Random(SEED)
    ids = list(range(rows))
    fks = [None if i % 997 == 0 else (i * 7919) % 50021 for i in ids]
    vals = [rng.randrange(1_000_000) for _ in ids]
    table = pa.table(
        {
            "id": pa.array(ids, pa.int64()),
            "fk": pa.array(fks, pa.int64()),
            "val": pa.array(vals, pa.int64()),
        }
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        table,
        path,
        row_group_size=row_group_size,
        compression="NONE",
        use_dictionary=False,
        write_statistics=STATISTICS_COLUMNS,
        write_page_index=True,
        data_page_size=DATA_PAGE_SIZE,
    )

    def expected(keep) -> dict:
        selected = [(i, v) for i, f, v in zip(ids, fks, vals, strict=True) if keep(i, f)]
        return {
            "rows": len(selected),
            "sum_id": str(sum(i for i, _ in selected)),
            "sum_val": str(sum(v for _, v in selected)),
            "mix": str(sum((i * 31 + v) % 1_000_003 for i, v in selected)),
        }

    return {
        "main": expected(lambda _i, f: f is not None and f >= 0),
        "control": expected(lambda i, _f: i >= 0),
        "is_not_null": expected(lambda i, _f: i is not None),
    }


# --- Independent footer reader (Thrift compact protocol) -------------------------------------


class ThriftError(ValueError):
    pass


class Compact:
    """A generic Thrift compact-protocol decoder: structs become {field_id: value}."""

    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def byte(self) -> int:
        if self.pos >= len(self.data):
            raise ThriftError("Unexpected end of Thrift data")
        value = self.data[self.pos]
        self.pos += 1
        return value

    def varint(self) -> int:
        result = shift = 0
        while True:
            b = self.byte()
            result |= (b & 0x7F) << shift
            if not b & 0x80:
                return result
            shift += 7
            if shift > 70:
                raise ThriftError("Varint too long")

    def zigzag(self) -> int:
        n = self.varint()
        return (n >> 1) ^ -(n & 1)

    def value(self, kind: int):
        if kind in (1, 2):  # bool inside a collection: one byte
            return self.byte() == 1
        if kind == 3:
            b = self.byte()
            return b - 256 if b > 127 else b
        if kind in (4, 5, 6):
            return self.zigzag()
        if kind == 7:
            raw = self.data[self.pos : self.pos + 8]
            self.pos += 8
            return struct.unpack("<d", raw)[0]
        if kind == 8:
            n = self.varint()
            raw = self.data[self.pos : self.pos + n]
            if len(raw) != n:
                raise ThriftError("Truncated binary")
            self.pos += n
            return bytes(raw)
        if kind in (9, 10):
            header = self.byte()
            size, element = header >> 4, header & 0x0F
            if size == 15:
                size = self.varint()
            return [self.value(element) for _ in range(size)]
        if kind == 11:
            size = self.varint()
            if not size:
                return {}
            types = self.byte()
            return {self.value(types >> 4): self.value(types & 0x0F) for _ in range(size)}
        if kind == 12:
            return self.struct()
        raise ThriftError(f"Unknown compact type {kind}")

    def struct(self) -> dict:
        fields, last = {}, 0
        while True:
            header = self.byte()
            if header == 0:
                return fields
            kind, delta = header & 0x0F, header >> 4
            field_id = last + delta if delta else self.zigzag()
            last = field_id
            fields[field_id] = kind == 1 if kind in (1, 2) else self.value(kind)


def decode_exact(data: bytes) -> dict:
    """Decode one struct that must occupy all of `data`."""
    reader = Compact(data)
    value = reader.struct()
    if reader.pos != len(data):
        raise ThriftError(f"Struct used {reader.pos} of {len(data)} bytes")
    return value


@dataclass(frozen=True)
class Region:
    kind: str  # "column_index" | "offset_index"
    row_group: int
    column: str
    start: int
    end: int

    @property
    def id(self) -> str:
        return f"{self.kind}:rg{self.row_group}:{self.column}"


def read_footer(path: Path) -> dict:
    """Return the footer span, per-chunk statistics facts and the page-index regions.

    Region bounds come only from `ColumnChunk` fields 4-7 of the Thrift footer. Each region is
    then decoded on its own and must be consumed exactly, which checks offset and length.
    """
    data = path.read_bytes()
    size = len(data)
    if data[:4] != b"PAR1" or data[-4:] != b"PAR1":
        raise ValueError("Not a plain PAR1 Parquet file")
    footer_len = int.from_bytes(data[-8:-4], "little")
    footer_start = size - 8 - footer_len
    meta = decode_exact(data[footer_start : size - 8])
    regions, chunks = [], []
    for rg_index, row_group in enumerate(meta[4]):
        for chunk in row_group[1]:
            cmd = chunk[3]
            column = ".".join(part.decode() for part in cmd[3])
            stats = cmd.get(12)
            start = min(o for o in (cmd.get(11), cmd[9]) if o is not None and o > 0)
            chunks.append(
                {
                    "row_group": rg_index,
                    "column": column,
                    "rows": row_group[3],
                    "data_span": [start, start + cmd[7]],
                    "statistics_present": stats is not None,
                    "null_count": None if stats is None else stats.get(3),
                    "min_max_present": stats is not None
                    and (5 in stats or 6 in stats or 1 in stats or 2 in stats),
                }
            )
            for kind, offset_field, length_field in (
                ("column_index", 6, 7),
                ("offset_index", 4, 5),
            ):
                if offset_field in chunk:
                    start_, length = chunk[offset_field], chunk[length_field]
                    regions.append(Region(kind, rg_index, column, start_, start_ + length))
    pages = {}
    for region in regions:
        if not 4 <= region.start < region.end <= footer_start:
            raise ValueError(f"Region outside the data area: {region}")
        decoded = decode_exact(data[region.start : region.end])
        key = (region.row_group, region.column)
        if region.kind == "offset_index":
            pages.setdefault(key, {})["offset_index_pages"] = len(decoded[1])
        else:
            pages.setdefault(key, {})["column_index_pages"] = len(decoded[1])
            pages[key]["column_index_null_counts"] = 5 in decoded
    for chunk in chunks:
        chunk.update(pages.get((chunk["row_group"], chunk["column"]), {}))
    return {
        "size": size,
        "footer": [footer_start, size],
        "num_rows": meta[3],
        "row_groups": len(meta[4]),
        "chunks": chunks,
        "regions": regions,
    }


def check_fixture(path: Path, footer: dict) -> dict:
    """Fixture facts the scenarios rely on, from our footer reader, cross-checked with pyarrow."""
    pa_meta = pq.ParquetFile(path).metadata
    pa_view = {}
    for rg in range(pa_meta.num_row_groups):
        for c in range(pa_meta.num_columns):
            col = pa_meta.row_group(rg).column(c)
            stats = col.statistics if col.is_stats_set else None
            pa_view[(rg, col.path_in_schema)] = {
                "has_column_index": col.has_column_index,
                "has_offset_index": col.has_offset_index,
                "null_count_known": bool(stats is not None and stats.has_null_count),
            }
    by_chunk = {(r.row_group, r.column, r.kind) for r in footer["regions"]}
    chunks = footer["chunks"]
    fk = [c for c in chunks if c["column"] == "fk"]
    ident = [c for c in chunks if c["column"] == "id"]
    checks = {
        "multiple_row_groups": footer["row_groups"] > 1,
        "every_chunk_has_offset_index": all(
            (c["row_group"], c["column"], "offset_index") in by_chunk for c in chunks
        ),
        # pyarrow writes no ColumnIndex for a column without statistics; record it.
        "statistics_columns_have_column_index": all(
            (c["row_group"], c["column"], "column_index") in by_chunk
            for c in chunks
            if c["column"] in STATISTICS_COLUMNS
        ),
        "fk_has_no_column_index": all(
            (c["row_group"], c["column"], "column_index") not in by_chunk for c in fk
        ),
        "multiple_pages_per_chunk": all(c.get("offset_index_pages", 0) > 1 for c in chunks),
        "fk_null_count_absent": all(c["null_count"] is None for c in fk),
        "id_null_count_zero": all(c["null_count"] == 0 for c in ident),
        "fk_contains_nulls": pq.read_table(path, columns=["fk"]).column("fk").null_count > 0,
        "pyarrow_agrees": all(
            pa_view[(c["row_group"], c["column"])]
            == {
                "has_column_index": (c["row_group"], c["column"], "column_index") in by_chunk,
                "has_offset_index": (c["row_group"], c["column"], "offset_index") in by_chunk,
                "null_count_known": c["null_count"] is not None,
            }
            for c in chunks
        ),
    }
    return checks


def region_layout(regions: list[Region]) -> dict:
    spans = {
        kind: union((r.start, r.end) for r in regions if r.kind == kind)
        for kind in ("column_index", "offset_index")
    }
    return {
        "column_index": spans["column_index"],
        "offset_index": spans["offset_index"],
        "page_index": union((r.start, r.end) for r in regions),
    }


# --- Matching server observations with index regions -------------------------------------------


def delivered_span(observation) -> tuple[int, int] | None:
    """Bytes the server confirms it wrote: the response start plus completed body writes."""
    if observation.method != "GET" or observation.response_span is None:
        return None
    written = int(observation.written_body_bytes)
    if written <= 0:
        return None
    start, end = observation.response_span.pair()
    return start, min(end, start + written)


def overlap(a: tuple[int, int], b: tuple[int, int]) -> int:
    return max(0, min(a[1], b[1]) - max(a[0], b[0]))


def match_observation(observation, regions: list[Region]) -> dict:
    """Describe one request against the index regions.

    `declared` overlap uses the response span the server announced; `delivered` overlap uses
    only bytes it wrote. A request is `index_only` when everything it delivered lies inside the
    page-index regions, and `incidental` when it also delivered bytes outside them.
    """
    declared = observation.response_span.pair() if observation.response_span else None
    delivered = delivered_span(observation)
    hits = []
    for region in regions:
        span = (region.start, region.end)
        declared_bytes = overlap(declared, span) if declared else 0
        if not declared_bytes:
            continue
        delivered_bytes = overlap(delivered, span) if delivered else 0
        hits.append(
            {
                "region": region.id,
                "kind": region.kind,
                "region_bytes": region.end - region.start,
                "declared_bytes": declared_bytes,
                "delivered_bytes": delivered_bytes,
                "fully_delivered": delivered_bytes == region.end - region.start,
            }
        )
    delivered_index = sum(h["delivered_bytes"] for h in hits)
    delivered_total = delivered[1] - delivered[0] if delivered else 0
    if not delivered_index:
        role = "none"
    elif delivered_index == delivered_total:
        role = "index_only"
    else:
        role = "incidental"
    return {
        "id": observation.id,
        "method": observation.method,
        "status": observation.status,
        "requested_range": observation.requested_range,
        "response_span": list(declared) if declared else None,
        "delivered_span": list(delivered) if delivered else None,
        "declared_body_bytes": int(observation.declared_body_bytes),
        "written_body_bytes": int(observation.written_body_bytes),
        "complete": observation.complete,
        "start_ms": round(observation.start_ms, 3),
        "duration_ms": round(observation.duration_ms, 3),
        "index_role": role,
        "delivered_index_bytes": delivered_index,
        "declared_index_bytes": sum(h["declared_bytes"] for h in hits),
        "regions": hits,
    }


def summarize_open(requests: list[dict], regions: list[Region]) -> dict:
    """Per-open totals. Region coverage counts only delivered bytes, merged across requests."""
    delivered = [tuple(r["delivered_span"]) for r in requests if r["delivered_span"]]
    covered = {}
    for region in regions:
        span = (region.start, region.end)
        got = coverage(
            (max(a, span[0]), min(b, span[1])) for a, b in delivered if overlap((a, b), span)
        )
        covered[region.id] = got
    full = {k: 0 for k in ("column_index", "offset_index")}
    partial = {k: 0 for k in ("column_index", "offset_index")}
    for region in regions:
        got = covered[region.id]
        if got == region.end - region.start:
            full[region.kind] += 1
        elif got:
            partial[region.kind] += 1
    return {
        "requests": len(requests),
        "get_requests": sum(r["method"] == "GET" for r in requests),
        "head_requests": sum(r["method"] == "HEAD" for r in requests),
        "incomplete_requests": sum(not r["complete"] for r in requests),
        "server_written_bytes": sum(r["written_body_bytes"] for r in requests),
        "index_requests": sum(r["index_role"] != "none" for r in requests),
        "index_only_requests": sum(r["index_role"] == "index_only" for r in requests),
        "incidental_index_requests": sum(r["index_role"] == "incidental" for r in requests),
        "delivered_index_bytes": sum(r["delivered_index_bytes"] for r in requests),
        "index_bytes_covered": sum(covered.values()),
        "regions_fully_covered": full,
        "regions_partially_covered": partial,
    }


# --- Harness ------------------------------------------------------------------------------------


def manifest_dir(version: str) -> Path:
    return HARNESS / f"df-{version}"


def locked_versions(version: str) -> dict:
    text = (manifest_dir(version) / "Cargo.lock").read_text()
    packages = re.findall(r'\[\[package\]\]\nname = "([^"]+)"\nversion = "([^"]+)"', text)
    datafusion = {n: v for n, v in packages if n.startswith("datafusion")}
    if not datafusion or any(v != version for v in datafusion.values()):
        raise RuntimeError(f"Cargo.lock for {version} does not pin every datafusion crate")
    others = {n: v for n, v in packages if n in ("arrow", "parquet", "object_store", "tokio")}
    return {"datafusion_crates": len(datafusion), **others}


def build_harness(version: str) -> Path:
    target = TARGETS / f"target-{version}"
    subprocess.run(
        [
            "cargo",
            "build",
            "--release",
            "--locked",
            "--manifest-path",
            str(manifest_dir(version) / "Cargo.toml"),
            "--target-dir",
            str(target),
        ],
        check=True,
    )
    return target / "release" / "harness"


class Harness:
    """Line protocol: the harness prints JSON events; we answer `go` and `next`."""

    def __init__(self, binary: Path, args: list[str]):
        self.process = subprocess.Popen(
            [str(binary), *args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self.timer = threading.Timer(RUN_TIMEOUT_SECONDS, self.process.kill)
        self.timer.start()

    def event(self) -> dict:
        line = self.process.stdout.readline()
        if not line:
            raise RuntimeError(f"Harness exited early (code {self.process.wait()})")
        return json.loads(line)

    def send(self, token: str):
        self.process.stdin.write(token + "\n")
        self.process.stdin.flush()

    def close(self):
        self.timer.cancel()
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait()


def server_ms(server: RangeServer) -> float:
    return (time.monotonic() - server.started) * 1000


def wait_quiet(server: RangeServer):
    """Every request of an open is recorded once its handler has finished."""
    deadline = time.monotonic() + QUIESCE_SECONDS
    while time.monotonic() < deadline:
        with server.lock:
            if server.open_connections == 0 and not server.active:
                return
        time.sleep(0.01)
    raise RuntimeError("Server did not become idle after an open")


def run_once(binary: Path, fixture: Path, scenario: str, regions, expected, opens=2) -> dict:
    with RangeServer(fixture, identity(fixture.stat())) as server:
        harness = Harness(
            binary,
            [
                "--base-url",
                f"http://127.0.0.1:{server.port}",
                "--path",
                server.route.lstrip("/"),
                "--size",
                str(server.size),
                "--predicate",
                SCENARIOS[scenario]["predicate"],
                "--opens",
                str(opens),
            ],
        )
        try:
            ready = harness.event()
            windows = []
            ends = []
            for number in range(1, opens + 1):
                begin = harness.event()
                if begin != {"event": "begin", "open": number}:
                    raise RuntimeError(f"Unexpected event {begin}")
                wait_quiet(server)
                with server.lock:
                    first = len(server.observations)
                begin_ms = server_ms(server)
                harness.send("go")
                end = harness.event()
                if end.get("event") != "end" or end.get("open") != number:
                    raise RuntimeError(f"Unexpected event {end}")
                wait_quiet(server)
                end_ms = server_ms(server)
                with server.lock:
                    last = len(server.observations)
                windows.append((number, first, last, begin_ms, end_ms))
                ends.append(end)
                harness.send("next")
            done = harness.event()
            if done != {"event": "done"}:
                raise RuntimeError(f"Unexpected event {done}")
        finally:
            harness.close()
        wait_quiet(server)
        with server.lock:
            observations = list(server.observations)
            server_state = {
                "truncated": server.truncated,
                "unmatched_routes": server.unmatched,
                "rejected_connections": server.rejected_connections,
                "input_changed": server.changed,
            }
    attributed = set()
    result_opens = []
    for (number, first, last, begin_ms, end_ms), end in zip(windows, ends, strict=True):
        own = observations[first:last]
        outside = [o.id for o in own if not begin_ms <= o.start_ms <= end_ms]
        attributed.update(range(first, last))
        requests = [match_observation(o, regions) for o in own]
        result_opens.append(
            {
                "open": number,
                "begin_ms": round(begin_ms, 3),
                "end_ms": round(end_ms, 3),
                "requests_started_outside_window": outside,
                "result": end["result"],
                "result_matches_expected": end["result"] == expected,
                "cache_after": end["cache_after"],
                "datafusion_metrics": end["metrics"],
                "summary": summarize_open(requests, regions),
                "requests": requests,
                # Raw server records; `--verify` re-derives everything above from these.
                "observations": [o.model_dump(mode="json") for o in own],
            }
        )
    unattributed = [o.id for i, o in enumerate(observations) if i not in attributed]
    return {
        "ready": ready,
        "server": server_state,
        "unattributed_requests": unattributed,
        "opens": result_opens,
    }


def integrity_errors(run: dict, version: str, scenario: str) -> list[str]:
    errors = []
    if run["ready"]["datafusion_version"] != version:
        errors.append(f"harness reports {run['ready']['datafusion_version']}")
    if run["ready"]["predicate"] != SCENARIOS[scenario]["sql"]:
        errors.append(f"harness predicate {run['ready']['predicate']}")
    if run["unattributed_requests"]:
        errors.append("requests outside open boundaries")
    if any(run["server"].values()):
        errors.append(f"server state {run['server']}")
    for open_ in run["opens"]:
        if not open_["result_matches_expected"]:
            errors.append(f"open {open_['open']} result {open_['result']}")
        if open_["requests_started_outside_window"]:
            errors.append(f"open {open_['open']} has requests outside its window")
    return errors


# --- Report -------------------------------------------------------------------------------------


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def key_summary(open_: dict) -> dict:
    s = open_["summary"]
    return {
        "requests": s["requests"],
        "server_written_bytes": s["server_written_bytes"],
        "index_requests": s["index_requests"],
        "index_only_requests": s["index_only_requests"],
        "incidental_index_requests": s["incidental_index_requests"],
        "delivered_index_bytes": s["delivered_index_bytes"],
        # Supplementary: loopback wall time between the open's boundaries.
        "open_wall_ms": round(open_["end_ms"] - open_["begin_ms"], 3),
        "index_spans": [
            r["delivered_span"] for r in open_["requests"] if r["index_role"] != "none"
        ],
        "cache_after": open_["cache_after"],
    }


def observations_verdict(summary: dict) -> dict:
    """Compare the observations with the hypotheses; the outcome is reported, not enforced."""

    def opens(version, scenario):
        return summary[f"{version}/{scenario}"]["opens"]

    old_main = opens("54.0.0", "main")
    new_main = opens("54.1.0", "main")
    new_control = opens("54.1.0", "control")
    return {
        "54.1.0_main_second_open_refetches_index": all(
            o["2"]["index_only_requests"] >= 1 for o in new_main
        ),
        "54.0.0_main_second_open_has_no_index_request": all(
            o["2"]["index_requests"] == 0 for o in old_main
        ),
        "54.1.0_control_has_no_separate_index_request": all(
            o[n]["index_only_requests"] == 0 for o in new_control for n in ("1", "2")
        ),
        "54.1.0_control_first_open_reads_index_incidentally": all(
            o["1"]["incidental_index_requests"] >= 1 for o in new_control
        ),
        "54.1.0_is_not_null_second_open_refetches_index": all(
            o["2"]["index_only_requests"] >= 1 for o in opens("54.1.0", "is_not_null")
        ),
    }


def summarize_records(records: list[dict]) -> dict:
    summary = {}
    for record in records:
        key = f"{record['version']}/{record['scenario']}"
        summary.setdefault(key, {"opens": []})["opens"].append(
            {str(o["open"]): key_summary(o) for o in record["opens"]}
        )
    return summary


def regions_from_report(report: dict) -> list[Region]:
    regions = []
    for entry in report["fixture"]["regions"]:
        kind, row_group, column = entry["id"].split(":", 2)
        regions.append(Region(kind, int(row_group[2:]), column, entry["start"], entry["end"]))
    return regions


def verify_evidence(output: Path) -> dict:
    """Re-derive matching, summaries and the verdict from the saved raw observations."""
    for line in (output / "SHA256SUMS.txt").read_text().splitlines():
        digest, name = line.split("  ", 1)
        if sha256_file(output / name) != digest:
            raise RuntimeError(f"Checksum mismatch: {name}")
    report = json.loads((output / "report.json").read_text())
    regions = regions_from_report(report)
    fixture = output / report["fixture"]["file"]
    if fixture.exists() and sha256_file(fixture) == report["fixture"]["sha256"]:
        if read_footer(fixture)["regions"] != regions:
            raise RuntimeError("Regions in report.json differ from the fixture footer")
    records = []
    for name in report["runs"]:
        record = json.loads((output / name).read_text())
        if integrity_errors(record, record["version"], record["scenario"]):
            raise RuntimeError(f"{name}: integrity check failed")
        expected = report["scenarios"][record["scenario"]]["expected"]
        for open_ in record["opens"]:
            raw = [ServerObservation4.model_validate(o) for o in open_["observations"]]
            requests = [match_observation(o, regions) for o in raw]
            if requests != open_["requests"]:
                raise RuntimeError(f"{name} open {open_['open']}: requests differ")
            if summarize_open(requests, regions) != open_["summary"]:
                raise RuntimeError(f"{name} open {open_['open']}: summary differs")
            if open_["result"] != expected:
                raise RuntimeError(f"{name} open {open_['open']}: result differs")
            if any(not open_["begin_ms"] <= o.start_ms <= open_["end_ms"] for o in raw):
                raise RuntimeError(f"{name} open {open_['open']}: request outside window")
        records.append(record)
    summary = summarize_records(records)
    if summary != report["summary"]:
        raise RuntimeError("Summary in report.json differs from the runs")
    verdict = observations_verdict(summary)
    if verdict != report["observations_vs_hypothesis"]:
        raise RuntimeError("Verdict in report.json differs from the runs")
    return verdict


def run_lab(output: Path, repetitions: int = 3, versions=VERSIONS, rows: int = ROWS) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    fixture = output / "fixture.parquet"
    expected = generate_fixture(fixture, rows=rows)
    footer = read_footer(fixture)
    checks = check_fixture(fixture, footer)
    if not all(checks.values()):
        raise RuntimeError(f"Fixture does not have the required metadata: {checks}")
    regions = footer["regions"]
    binaries = {v: build_harness(v) for v in versions}
    runs_dir = output / "runs"
    shutil.rmtree(runs_dir, ignore_errors=True)
    runs_dir.mkdir()
    records, runs = [], []
    for version in versions:
        for scenario in SCENARIOS:
            for repetition in range(1, repetitions + 1):
                run = run_once(binaries[version], fixture, scenario, regions, expected[scenario])
                errors = integrity_errors(run, version, scenario)
                if errors:
                    raise RuntimeError(f"{version}/{scenario}/{repetition}: {errors}")
                name = f"{version}-{scenario}-{repetition}.json"
                record = {
                    "version": version,
                    "scenario": scenario,
                    "repetition": repetition,
                    **run,
                }
                (runs_dir / name).write_text(json.dumps(record, indent=1) + "\n")
                runs.append(f"runs/{name}")
                records.append(record)
    summary = summarize_records(records)
    report = {
        "issue": "https://github.com/apache/datafusion/issues/23978",
        "environment": {
            "parquetscope": importlib.metadata.version("parquetscope"),
            "pyarrow": pa.__version__,
            "python": platform.python_version(),
            "platform": f"{platform.system()} {platform.machine()}",
            "rustc": subprocess.run(
                ["rustc", "--version"], capture_output=True, text=True, check=True
            ).stdout.strip(),
            "locked": {v: locked_versions(v) for v in versions},
        },
        "fixture": {
            "file": "fixture.parquet",
            "sha256": sha256_file(fixture),
            "size": footer["size"],
            "rows": footer["num_rows"],
            "row_groups": footer["row_groups"],
            "row_group_size": ROW_GROUP_SIZE,
            "data_page_size": DATA_PAGE_SIZE,
            "statistics_columns": STATISTICS_COLUMNS,
            "footer_span": footer["footer"],
            "checks": checks,
            "layout": region_layout(regions),
            "regions": [
                {"id": r.id, "start": r.start, "end": r.end, "bytes": r.end - r.start}
                for r in regions
            ],
            "chunks": footer["chunks"],
        },
        "scenarios": {
            name: {
                "predicate": s["sql"],
                "meaning": s["meaning"],
                "expected": expected[name],
            }
            for name, s in SCENARIOS.items()
        },
        "repetitions": repetitions,
        "summary": summary,
        "observations_vs_hypothesis": observations_verdict(summary)
        if set(versions) == set(VERSIONS)
        else None,
        "runs": runs,
    }
    (output / "report.json").write_text(json.dumps(report, indent=1) + "\n")
    files = sorted(p for p in output.rglob("*") if p.is_file() and p.name != "SHA256SUMS.txt")
    (output / "SHA256SUMS.txt").write_text(
        "".join(
            f"{sha256_file(p)}  {p.relative_to(output).as_posix()}\n"
            for p in files
            if p.suffix not in (".md", ".parquet")
        )
    )
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Check saved evidence in --output without running anything.",
    )
    args = parser.parse_args()
    if args.verify:
        print(json.dumps(verify_evidence(args.output), indent=1))
        return
    report = run_lab(args.output, args.repetitions)
    print(json.dumps(report["observations_vs_hypothesis"], indent=1))
    for key, value in report["summary"].items():
        for repetition, opens in enumerate(value["opens"], 1):
            for number, s in opens.items():
                print(
                    f"{key} rep {repetition} open {number}: {s['requests']} requests, "
                    f"{s['server_written_bytes']} B written, index requests "
                    f"{s['index_requests']} (index-only {s['index_only_requests']}), "
                    f"index spans {s['index_spans']}"
                )


if __name__ == "__main__":
    main()
