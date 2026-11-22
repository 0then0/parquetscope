"""Measure a point lookup with DuckDB 2.0.0 and 2.0.1 on one indexed Parquet file."""

import argparse
import hashlib
import json
import platform
import struct
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

import pyarrow as pa
import pyarrow.parquet as pq
from datafusion_page_index_lab import (
    Compact,
    decode_exact,
    delivered_span,
    overlap,
    read_footer,
    sha256_file,
    wait_quiet,
)

from parquetscope.engines import EngineEnv, engine_root
from parquetscope.http_profile import HTTPProfile, profile_evidence
from parquetscope.intervals import union
from parquetscope.metadata import identity
from parquetscope.models_v4 import ServerObservation4
from parquetscope.range_server import RangeServer

OUTPUT = Path(__file__).resolve().parents[1] / ".runtime/duckdb-page-index-lab/evidence"
CLIENT = Path(__file__).with_name("duckdb_page_index_client.py").resolve()
BASELINE, INDEXED = "2.0.0", "2.0.1"
ROWS = 262_144
POINT_ID = 123_456
COLUMNS = ("id", "payload")
WRITER_OPTIONS = {
    "row_group_size": ROWS,
    "compression": "NONE",
    "use_dictionary": False,
    "write_statistics": True,
    "write_page_index": True,
    "data_page_size": 128 * 1024,
    "data_page_version": "1.0",
    "version": "2.6",
}
SOURCE = "read_parquet(?, hive_partitioning=false, union_by_name=false, prefetch_strategy='auto')"
QUERIES = {
    "point": {"sql": f"SELECT id, payload FROM {SOURCE} WHERE id = ?", "parameters": [POINT_ID]},
    "wide": {"sql": f"SELECT count(*), sum(id), sum(payload) FROM {SOURCE}", "parameters": []},
}
ORDER = (
    (BASELINE, "point"),
    (INDEXED, "point"),
    (INDEXED, "point_index_off"),
    (BASELINE, "wide"),
    (INDEXED, "wide"),
)
REPETITIONS = 3
EVIDENCE = ("report.json", "runs.json")
FIXTURE_NAME = "fixture.parquet"
CHECKSUMS_NAME = "SHA256SUMS.txt"


def generate_fixture(path: Path) -> dict:
    """Write the file and compute both query results from its original Python values."""
    ids = range(ROWS)
    payload = [(i * 7919 + 17) % 1_000_003 for i in ids]
    schema = pa.schema([pa.field(name, pa.int64(), nullable=False) for name in COLUMNS])
    table = pa.Table.from_arrays([pa.array(ids), pa.array(payload)], schema=schema)
    pq.write_table(table, path, sorting_columns=[pq.SortingColumn(0)], **WRITER_OPTIONS)
    return {
        "point": [[POINT_ID, payload[POINT_ID]]],
        "wide": [[ROWS, sum(ids), sum(payload)]],
    }


def read_pages(data: bytes, chunk: dict, offsets: dict, bounds: dict) -> list[dict]:
    """Check the OffsetIndex against the chunk, the ColumnIndex and actual V1 page headers."""
    locations = offsets[1]  # OffsetIndex.page_locations
    null_pages, minima, maxima = (bounds[field] for field in (1, 2, 3))
    if len(locations) < 2 or not len(locations) == len(null_pages) == len(minima) == len(maxima):
        raise ValueError(f"Inconsistent page indexes for {chunk['column']}")
    chunk_start, chunk_end = chunk["data_span"]
    row_starts = [location[3] for location in locations] + [chunk["rows"]]
    if row_starts[0] != 0 or any(null_pages):
        raise ValueError(f"Unexpected first row or null page for {chunk['column']}")

    pages = []
    cursor = chunk_start
    for number, location in enumerate(locations):
        start, size = location[1], location[2]  # includes page header and compressed body
        end = start + size
        first, stop = row_starts[number : number + 2]
        if not (cursor == start < end <= chunk_end and 0 <= first < stop <= chunk["rows"]):
            raise ValueError(f"Invalid data-page bounds for {chunk['column']} page {number}")
        reader = Compact(data[start:end])
        header = reader.struct()
        # PageHeader.type = DATA_PAGE (0), field 5 is DataPageHeader.
        if header[1] != 0 or reader.pos + header[3] != size or header[5][1] != stop - first:
            raise ValueError(f"Page header disagrees with OffsetIndex for {chunk['column']}")
        minimum = struct.unpack("<q", minima[number])[0]
        maximum = struct.unpack("<q", maxima[number])[0]
        expected_id_bounds = (first, stop - 1)
        if minimum > maximum or (
            chunk["column"] == "id" and (minimum, maximum) != expected_id_bounds
        ):
            raise ValueError(f"Invalid ColumnIndex bounds for {chunk['column']} page {number}")
        pages.append(
            {
                "span": [start, end],
                "rows": [first, stop],
                "min": minimum,
                "max": maximum,
                "header_statistics_present": 5 in header[5],
                "contains_point_row": first <= POINT_ID < stop,
            }
        )
        cursor = end
    if cursor != chunk_end:
        raise ValueError(f"Pages do not fill the chunk for {chunk['column']}")
    return pages


def check_regions(regions: dict, size: int) -> None:
    end = 0
    for name, region in sorted(regions.items(), key=lambda item: item[1]["span"]):
        start, stop = region["span"]
        if not end <= start < stop <= size:
            raise ValueError(f"Overlapping or out-of-file region {name}: {region['span']}")
        end = stop


def inspect_fixture(path: Path) -> dict:
    footer = read_footer(path)
    data = path.read_bytes()
    arrow = pq.read_metadata(path)
    if footer["row_groups"] != 1 or arrow.num_row_groups != 1 or footer["num_rows"] != ROWS:
        raise ValueError("Expected one full row group")
    row_group = arrow.row_group(0)
    if row_group.num_rows != ROWS or row_group.num_columns != len(COLUMNS):
        raise ValueError("PyArrow found an unexpected row group shape")
    if row_group.sorting_columns != (pq.SortingColumn(0),):
        raise ValueError("Expected an ascending sort on id")
    if tuple(c["column"] for c in footer["chunks"]) != COLUMNS:
        raise ValueError("Unexpected columns in fixture")

    regions = {
        "magic": {"kind": "magic", "span": [0, 4]},
        "footer": {"kind": "footer", "span": footer["footer"]},
    }
    columns = {}
    for number, chunk in enumerate(footer["chunks"]):
        column = chunk["column"]
        info = row_group.column(number)
        if not (info.has_column_index and info.has_offset_index and info.is_stats_set):
            raise ValueError(f"PyArrow did not find both indexes and chunk statistics for {column}")
        if (
            info.physical_type,
            info.compression,
            info.has_dictionary_page,
            chunk["null_count"],
        ) != (
            "INT64",
            "UNCOMPRESSED",
            False,
            0,
        ):
            raise ValueError(f"Expected plain uncompressed non-null integers in {column}")
        indexes = {}
        for region in footer["regions"]:
            if region.column == column:
                indexes[region.kind] = decode_exact(data[region.start : region.end])
                regions[f"{column}.{region.kind}"] = {
                    "kind": region.kind,
                    "span": [region.start, region.end],
                }
        pages = read_pages(data, chunk, indexes["offset_index"], indexes["column_index"])
        if any(p["header_statistics_present"] for p in pages):
            raise ValueError(f"Expected no page-header statistics for {column}")
        (target,) = (i for i, p in enumerate(pages) if p["contains_point_row"])
        stats = info.statistics
        if column == "id" and not (
            indexes["column_index"][4] == 1 and stats.min < POINT_ID < stats.max
        ):
            raise ValueError("Expected ascending page bounds and an interior point in id")
        columns[column] = {
            "chunk_span": chunk["data_span"],
            "row_group_min_max": [stats.min, stats.max],
            "point_page": target,
            "data_pages": len(pages),
        }
        for i, page in enumerate(pages):
            regions[f"{column}.page.{i}"] = {"kind": "data_page", **page}

    check_regions(regions, len(data))
    return {
        "file": path.name,
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "rows": ROWS,
        "row_groups": 1,
        "sorted_by": "id",
        "writer": arrow.created_by,
        "writer_options": WRITER_OPTIONS,
        "columns": columns,
        "regions": regions,
    }


def analyze(observations: list[ServerObservation4], regions: dict, size: int) -> tuple[list, dict]:
    """Match only confirmed, sequential body writes to disjoint half-open file regions."""
    requests = []
    written_spans = []
    for observation in observations:
        written = delivered_span(observation)
        if observation.response_span:
            start, end = observation.response_span.pair()
            if not 0 <= start < end <= size:
                raise ValueError(f"Out-of-file response {observation.id}")
        if written:
            written_spans.append(written)
        if int(observation.written_body_bytes) != (written[1] - written[0] if written else 0):
            raise ValueError(f"Unmappable server writes for {observation.id}")
        overlaps = {}
        if written:
            for name, region in regions.items():
                if count := overlap(written, region["span"]):
                    overlaps[name] = count
        requests.append(
            {
                "id": observation.id,
                "written_span": list(written) if written else None,
                "overlap_bytes": overlaps,
            }
        )

    unique = union(written_spans)
    unique_bytes = sum(end - start for start, end in unique)
    written_bytes = sum(end - start for start, end in written_spans)
    by_kind = {}
    touched_pages = outside_point_pages = 0
    outside_point = {"written_bytes": 0, "unique_bytes": 0}
    for name, region in regions.items():
        used = sum(request["overlap_bytes"].get(name, 0) for request in requests)
        covered = sum(overlap(interval, region["span"]) for interval in unique)
        kind = region["kind"]
        totals = by_kind.setdefault(kind, {"written_bytes": 0, "unique_bytes": 0})
        totals["written_bytes"] += used
        totals["unique_bytes"] += covered
        if kind == "data_page" and covered:
            touched_pages += 1
            if not region["contains_point_row"]:
                outside_point_pages += 1
                outside_point["written_bytes"] += used
                outside_point["unique_bytes"] += covered
    return requests, {
        "requests": len(observations),
        "get_requests": sum(o.method == "GET" for o in observations),
        "head_requests": sum(o.method == "HEAD" for o in observations),
        "incomplete_requests": sum(not o.complete for o in observations),
        "server_written_bytes": written_bytes,
        "unique_coverage_bytes": unique_bytes,
        "repeated_coverage_bytes": written_bytes - unique_bytes,
        "bytes_by_kind": by_kind,
        "data_pages_touched": touched_pages,
        "pages_outside_point_touched": outside_point_pages,
        "bytes_in_pages_outside_point": outside_point,
    }


def schedule():
    for repetition in range(1, REPETITIONS + 1):
        for version, scenario in ORDER if repetition % 2 else reversed(ORDER):
            yield repetition, version, scenario


def capture(
    path: Path,
    file_identity: tuple,
    client: EngineEnv,
    profile: HTTPProfile,
    query: dict,
    scenario: str,
) -> dict:
    with RangeServer(path, file_identity, profile=profile) as server:
        config = {
            "version": client.version,
            "extension_dir": str(client.extension_dir),
            "sql": query["sql"],
            "parameters": [server.url, *query["parameters"]],
            "use_page_index": scenario != "point_index_off" if client.version == INDEXED else None,
        }
        result = subprocess.run(
            [str(client.python), "-I", str(CLIENT), json.dumps(config)],
            capture_output=True,
            text=True,
            timeout=120,
        )
        wait_quiet(server)
    if result.returncode:
        raise RuntimeError(f"DuckDB query failed:\n{result.stderr.strip()}")
    if server.truncated or server.changed or server.unmatched or server.rejected_connections:
        raise RuntimeError("Server trace was truncated, input changed or a connection was rejected")
    observations = sorted(server.observations, key=lambda o: o.start_ms)
    if not observations:
        raise RuntimeError("Missing server observations")
    return {**json.loads(result.stdout), "observations": observations}


def summarize(report: dict, runs: list[dict]) -> list[dict]:
    fixture = report["fixture"]
    regions = fixture["regions"]
    check_regions(regions, fixture["size"])
    data_bytes = sum(
        r["span"][1] - r["span"][0] for r in regions.values() if r["kind"] == "data_page"
    )
    summaries = []
    common_settings = None
    builds = {}
    for run, (repetition, version, scenario) in zip(runs, schedule(), strict=True):
        if (run["repetition"], run["duckdb_version"], run["scenario"]) != (
            repetition,
            version,
            scenario,
        ):
            raise ValueError("Run order or DuckDB version differs from the planned schedule")
        build = (run["duckdb_revision"], run["httpfs_version"], run["python"])
        if not all(build) or build != builds.setdefault(version, build):
            raise ValueError(f"Missing or varying build provenance for {version}")
        query = report["queries"]["wide" if scenario == "wide" else "point"]
        if run["result"] != query["expected"]:
            raise ValueError(f"Wrong result for {version} {scenario}: {run['result']}")
        settings = run["settings"].copy()
        index_setting = settings.pop("parquet_use_page_index")
        expected_index = None if version == BASELINE else str(scenario != "point_index_off").lower()
        if index_setting != expected_index or settings["disable_parquet_prefetching"] != "false":
            raise ValueError(f"Unexpected page-index or prefetch setting for {version} {scenario}")
        if common_settings is None:
            common_settings = settings
        if common_settings != settings:
            raise ValueError(f"Effective settings differ for {version} {scenario}")
        raw = [ServerObservation4.model_validate(o) for o in run["observations"]]
        requests, totals = analyze(raw, regions, fixture["size"])
        if requests != run["matches"]:
            raise ValueError(f"Response overlap differs for {version} {scenario}")
        data_coverage = totals["bytes_by_kind"]["data_page"]["unique_bytes"]
        if scenario == "wide" and data_coverage != data_bytes:
            raise ValueError(f"Wide control did not cover every data page for {version}")
        summaries.append(
            {"repetition": repetition, "version": version, "scenario": scenario, **totals}
        )
    return summaries


def run_lab(output: Path) -> dict:
    clients = {}
    for version in (BASELINE, INDEXED):
        root = engine_root(version).resolve()
        client = EngineEnv(version, root / "venv/bin/python", root / "extensions", isolated=True)
        if not client.python.is_file() or not client.extension_dir.is_dir():
            raise RuntimeError(f"Prepare DuckDB {version} and httpfs first. See the lab README.")
        clients[version] = client

    output.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".capture-", dir=output) as temporary:
        staging = Path(temporary)
        report = write_evidence(staging, clients)
        for name in (FIXTURE_NAME, *EVIDENCE, CHECKSUMS_NAME):
            (staging / name).replace(output / name)
    return report


def write_evidence(output: Path, clients: dict[str, EngineEnv]) -> dict:
    path = output / FIXTURE_NAME
    expected = generate_fixture(path)
    file_identity = identity(path.stat())
    fixture = inspect_fixture(path)
    profile = HTTPProfile()
    queries = {name: {**query, "expected": expected[name]} for name, query in QUERIES.items()}
    report = {
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "pyarrow": pa.__version__,
        },
        "fixture": fixture,
        "queries": queries,
        "repetitions": REPETITIONS,
        "http_profile": profile_evidence(profile).model_dump(mode="json"),
    }
    runs = []
    for repetition, version, scenario in schedule():
        query = queries["wide" if scenario == "wide" else "point"]
        captured = capture(path, file_identity, clients[version], profile, query, scenario)
        observations = captured["observations"]
        matches, _ = analyze(observations, fixture["regions"], fixture["size"])
        runs.append(
            {
                "repetition": repetition,
                "scenario": scenario,
                **captured,
                "observations": [o.model_dump(mode="json") for o in observations],
                "matches": matches,
            }
        )
    report["runs"] = summarize(report, runs)
    traces = {"fixture_sha256": fixture["sha256"], "runs": runs}
    for name, value in zip(EVIDENCE, (report, traces), strict=True):
        (output / name).write_text(json.dumps(value, indent=1) + "\n", encoding="utf-8")
    (output / CHECKSUMS_NAME).write_text(checksums(output), encoding="utf-8")
    return report


def checksums(output: Path) -> str:
    return "".join(f"{sha256_file(output / name)}  {name}\n" for name in EVIDENCE)


def verify_evidence(output: Path) -> dict:
    if (output / CHECKSUMS_NAME).read_text() != checksums(output):
        raise ValueError("Evidence checksums differ")
    report = json.loads((output / "report.json").read_text())
    traces = json.loads((output / "runs.json").read_text())
    fixture = report["fixture"]
    path = output / fixture["file"]
    if path.exists() and inspect_fixture(path) != fixture:
        raise ValueError("Local fixture differs from the recorded file map")
    if traces["fixture_sha256"] != fixture["sha256"]:
        raise ValueError("Fixture hash differs from the saved traces")
    if summarize(report, traces["runs"]) != report["runs"]:
        raise ValueError("Run summaries differ from the saved traces")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--verify", action="store_true", help="Recompute the saved range overlaps")
    args = parser.parse_args()
    report = verify_evidence(args.output) if args.verify else run_lab(args.output)
    for run in report["runs"]:
        print(
            f"{run['version']} {run['scenario']} #{run['repetition']}: "
            f"{run['requests']} requests, {run['server_written_bytes']:,} B written, "
            f"{run['unique_coverage_bytes']:,} B unique"
        )


if __name__ == "__main__":
    main()
