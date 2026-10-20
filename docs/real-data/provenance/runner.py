"""Opt-in NYC TLC experiment. Run with the Python of an installed release wheel.

No dataset download on import. Public output contains only validated native captures,
three native offline viewers and a report; working Parquet files stay outside it.
"""

import argparse
import hashlib
import importlib.metadata
import json
import platform
import subprocess
import tempfile
import urllib.request
import zipfile
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import duckdb
import pyarrow
import pyarrow.parquet as pq

import parquetscope
from parquetscope.capture import capture
from parquetscope.models import Settings, load_artifact_file, save_artifact
from parquetscope.offline import viewer_html

SOURCE_URL = "https://d37ci6vzurychx.cloudfront.net/trip-data/yellow_tripdata_2025-01.parquet"
SOURCE_PAGE = "https://www.nyc.gov/site/tlc/about/tlc-trip-record-data.page"
SOURCE_SIZE = 59158238
SOURCE_SHA256 = "9af277e4c0d3f9deb30644da822981e1e7df6af58313170fd3aa8a474485488a"
WHEEL_URL = (
    "https://github.com/0then0/parquetscope/releases/download/v0.1.0/"
    "parquetscope-0.1.0-py3-none-any.whl"
)
WHEEL_SHA256 = "616192b3e0b776ca1221b86d4b2c80dbd545b6884457d6df0394045a6d9169e9"
SQL = """SELECT
    count(*) AS n,
    sum(CAST(fare_amount AS DECIMAL(18,2))) AS fare
FROM input
WHERE PULocationID = 132
  AND trip_distance > 0"""
EXPECTED = [["142826", "8038349.75"]]
PREPARATION = {
    "threads": "1",
    "memory_limit": "1GB",
    "preserve_insertion_order": "true",
    "max_temp_directory_size": "10GB",
}
WRITER = {
    "engine": "DuckDB",
    "compression": "ZSTD",
    "compression_level": 3,
    "row_group_size": 122880,
    "parquet_version": "V1",
    "other_options": "DuckDB 1.5.6 defaults, identical for B and C",
}
LABELS = {"A": "As delivered", "B": "Repacked, original order", "C": "Sorted by pickup location"}
METRICS = {
    "get_count": "GET observations in DuckDB HTTP logs, reconciled with the independent server",
    "head_count": "HEAD observations; HEAD contributes zero body bytes",
    "server_written_bytes": (
        "Sum of bytes successfully written by the independent server to streams"
    ),
    "requested_range_bytes": "Sum of resolved GET request Range lengths",
    "confirmed_range_bytes": "Sum of GET response interval lengths confirmed by response headers",
    "declared_body_bytes": "Sum of GET Content-Length; HEAD contributes zero",
    "unique_coverage_bytes": "Length of union of confirmed GET response intervals",
    "repeated_coverage_bytes": (
        "Sum of confirmed interval lengths minus their union, not assumed waste"
    ),
    "coverage": "Union of confirmed GET response intervals, half-open byte offsets",
    "totals_scope": (
        "Complete or observed subset; only complete reconciled runs enter successful runs"
    ),
    "unknown_reasons": "Reasons unavailable metrics are null",
    "query_duration_ms": "SELECT execution through full result consumption; excludes input binding",
    "elapsed_ms": "Worker startup, binding, execution and cleanup; excludes metadata preparation",
}
LIMITATIONS = [
    "Premeasured loopback HTTP captures; static browsing executes no SQL.",
    "Fresh worker and disabled external file cache; OS/disk caches are uncontrolled, not cold S3.",
    (
        "Server writes do not prove client receipt, chunk decoding or "
        "use. Overlap is not automatic waste."
    ),
    "Read bytes do not imply proportional speedup, S3 performance or a cloud bill.",
    "One workload on one month does not establish a universally better layout.",
    "A/B change writer, encoding and layout as well as row groups; no single-factor attribution.",
    (
        "B/C use identical writer settings; row order is the intended "
        "difference. Ties have no extra key."
    ),
    (
        "Pruning and sorting are established mechanisms; ParquetScope "
        "exposes this workload's observations."
    ),
    (
        "Captures retain SQL, column names and metadata; they omit "
        "preview rows, not all possible data."
    ),
    "Versions, scheduling and platform can affect sizes, timing and request sequences.",
    (
        "Runs are sequential A1..A3, B1..B3, C1..C3, without cache "
        "eviction or randomized timing trials."
    ),
]


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def checked_source(path):
    if path.stat().st_size != SOURCE_SIZE or sha256(path) != SOURCE_SHA256:
        raise ValueError("NYC TLC input size/SHA-256 mismatch; refusing to use a different input")


def download_source(path, *, url=SOURCE_URL, size=SOURCE_SIZE, digest=SOURCE_SHA256):
    """Bounded streaming into a temporary sibling; publish only after size AND hash match."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, suffix=".part", delete=False) as temp:
        temporary = Path(temp.name)
        try:
            total = 0
            hasher = hashlib.sha256()
            with urllib.request.urlopen(url, timeout=30) as response:
                length = response.headers.get("Content-Length")
                if length is not None and int(length) > size:
                    raise ValueError("Download exceeds pinned input size")
                while chunk := response.read(min(1024 * 1024, size + 1 - total)):
                    total += len(chunk)
                    if total > size:
                        raise ValueError("Download exceeds pinned input size")
                    hasher.update(chunk)
                    temp.write(chunk)
            if total != size or hasher.hexdigest() != digest:
                raise ValueError("NYC TLC input size/SHA-256 mismatch; download rejected")
            temp.close()
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def write_json(path, data):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, indent=2) + "\n")
    temporary.replace(path)


def schema(connection, path):
    return [
        list(row[:2])
        for row in connection.execute(
            "DESCRIBE SELECT * FROM read_parquet(?)", [str(path)]
        ).fetchall()
    ]


def describe(connection, path):
    meta = pq.ParquetFile(path)
    return {
        "name": path.name,
        "size": str(path.stat().st_size),
        "sha256": sha256(path),
        "rows": str(meta.metadata.num_rows),
        "row_groups": meta.metadata.num_row_groups,
        "row_group_rows": [
            meta.metadata.row_group(i).num_rows for i in range(meta.metadata.num_row_groups)
        ],
        "schema": schema(connection, path),
        "arrow_schema": [
            {"name": f.name, "type": str(f.type), "nullable": f.nullable} for f in meta.schema_arrow
        ],
        "parquet_schema": str(meta.schema).split("required group", 1)[1],
        "created_by": meta.metadata.created_by,
        "compression": sorted(
            {
                meta.metadata.row_group(i).column(j).compression
                for i in range(meta.metadata.num_row_groups)
                for j in range(meta.metadata.num_columns)
            }
        ),
        "encodings": sorted(
            {
                e
                for i in range(meta.metadata.num_row_groups)
                for j in range(meta.metadata.num_columns)
                for e in meta.metadata.row_group(i).column(j).encodings
            }
        ),
    }


def multiset_difference(connection, a, b):
    """Full duplicate-sensitive comparison, including all columns and NULLs, both ways."""
    counts = []
    for left, right in ((a, b), (b, a)):
        counts.append(
            connection.execute(
                "SELECT count(*) FROM (SELECT * FROM read_parquet(?) "
                "EXCEPT ALL SELECT * FROM read_parquet(?))",
                [str(left), str(right)],
            ).fetchone()[0]
        )
    if counts != [0, 0]:
        raise ValueError(f"Full multiset equality failed: directional difference counts {counts}")
    return {
        "method": "bidirectional EXCEPT ALL over all columns",
        "difference_rows": counts,
        "equal": True,
    }


def same_order(a, b):
    """Compare persisted Arrow batches in sequence, with identical SQL logical types.

    large_string/string are Arrow offset widths, both Parquet UTF8 and DuckDB VARCHAR.
    """
    original = pq.ParquetFile(a)
    rewritten = pq.ParquetFile(b)
    from itertools import zip_longest

    for x, y in zip_longest(original.iter_batches(), rewritten.iter_batches()):
        if x is None or y is None or not x.equals(y.cast(x.schema), check_metadata=False):
            raise ValueError("B did not preserve original row order")
    return True


def wheel_provenance(wheel):
    if sha256(wheel) != WHEEL_SHA256:
        raise ValueError("Wheel differs from the pinned published v0.1.0 release")
    package = Path(parquetscope.__file__).resolve()
    if "site-packages" not in package.parts:
        raise ValueError("Run acceptance with a non-editable installed wheel, outside checkout")
    # Verify the installed package, including worker and bundled viewer, against the wheel.
    with zipfile.ZipFile(wheel) as archive:
        for name in archive.namelist():
            if name.startswith("parquetscope/") and not name.endswith("/"):
                if (package.parent.parent / name).read_bytes() != archive.read(name):
                    raise ValueError(f"Installed wheel content differs: {name}")
    return {
        "url": WHEEL_URL,
        "sha256": sha256(wheel),
        "version": parquetscope.__version__,
        "installed_non_editable": True,
        "installed_files_verified_against_wheel": True,
        "environment": "Separate virtual environment; execution package in site-packages",
        "cwd_outside_checkout": not Path.cwd()
        .resolve()
        .is_relative_to(Path(__file__).resolve().parents[1]),
        "dependencies": {d.metadata["Name"]: d.version for d in importlib.metadata.distributions()},
    }


def exact_result(preview):
    rows = preview["rows"]
    if len(rows) != 1 or len(rows[0]) != 2:
        raise ValueError("Expected exactly one aggregate result row")
    n, fare = rows[0]
    # v0.1.0 json_value serializes Decimal via json.dumps(default=str), retaining quotes.
    # Decode that representation, never convert money through float.
    if isinstance(fare, str) and fare.startswith('"'):
        fare = json.loads(fare)
    if not isinstance(n, str) or not isinstance(fare, str):
        raise ValueError("Expected exact string representations of integer and DECIMAL")
    return [[str(int(n)), format(Decimal(fare), ".2f")]]


def verify_capture(run, preview, expected, settings, layout):
    if (
        run.sql_status != "succeeded"
        or not run.capture.complete
        or not run.capture.reconciled
        or run.errors
        or run.metrics.unknown_reasons
    ):
        raise ValueError(f"Capture rejected: {run.sql_status}, {run.capture}, {run.errors}")
    if exact_result(preview) != expected or run.result_rows != "1":
        raise ValueError(f"Exact SQL result mismatch: {preview}")
    encoded = json.dumps(preview["rows"][0], ensure_ascii=False).encode()
    digest = hashlib.sha256(len(encoded).to_bytes(8, "big") + encoded).hexdigest()
    if digest != run.result_digest:
        raise ValueError("Runtime aggregate values disagree with captured result digest")
    if run.sql != SQL or run.settings != settings:
        raise ValueError("Capture workload/settings differ")
    for key in ("sha256", "size", "rows", "row_groups"):
        if getattr(run.input, key) != layout[key]:
            raise ValueError(f"Capture input {key} differs from prepared layout")


def verify_report(directory):
    """Revalidate native artifacts and report links; never trust copied metric totals."""
    report = json.loads((directory / "report.json").read_text())
    seen = set()
    for layout in report["layouts"]:
        repeats = layout["repetitions"]
        if [r["repetition"] for r in repeats] != [1, 2, 3]:
            raise ValueError("Report must retain all three repetitions of every layout")
        for item in repeats:
            path = directory / item["capture"]
            run = load_artifact_file(path)
            if run.id in seen:
                raise ValueError("Captures must be nine distinct fresh runs")
            seen.add(run.id)
            verify_capture(
                run,
                {"rows": item["runtime_result"]},
                EXPECTED,
                Settings(**report["capture_settings"]),
                layout,
            )
            if (
                sha256(path) != item["sha256"]
                or run.id != item["id"]
                or run.metrics.model_dump() != item["metrics"]
                or run.result_digest != item["result_digest"]
                or run.query_duration_ms != item["query_duration_ms"]
                or run.elapsed_ms != item["elapsed_ms"]
            ):
                raise ValueError("Report differs from native capture")
        if layout["selected_repetition"] != 1:
            raise ValueError("Interactive selection must be explicitly repetition 1")
    if len(seen) != 9 or [x["id"] for x in report["layouts"]] != ["A", "B", "C"]:
        raise ValueError("Expected nine fresh captures for A/B/C")
    return report


def public_manifest(directory):
    files = {}
    for path in sorted(directory.rglob("*")):
        if not path.is_file() or path.name in ("manifest.json", "SHA256SUMS.txt"):
            continue
        data = path.read_bytes()
        if any(
            token in data
            for token in (
                b"/Users/",
                b"/home/",
                b"/private/tmp/",
                b"Authorization:",
                b"AWS_SECRET_ACCESS_KEY",
            )
        ):
            raise ValueError(f"Potential private path/credential in public file {path.name}")
        files[path.relative_to(directory).as_posix()] = {
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }
    write_json(directory / "manifest.json", {"schema_version": "1", "files": files})
    (directory / "SHA256SUMS.txt").write_text(
        "".join(f"{meta['sha256']}  {name}\n" for name, meta in files.items())
    )


def run_experiment(work, output, wheel):
    if output.exists() and any(output.iterdir()):
        raise ValueError("Output must be empty; previous attempts/evidence must not be overwritten")
    if work.resolve().is_relative_to(output.resolve()) or output.resolve().is_relative_to(
        work.resolve()
    ):
        raise ValueError("Keep working Parquet files and public output in separate directories")
    output.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)
    settings = Settings(threads=4, memory_limit_mb=512, deadline_seconds=60, preview_rows=1)
    root = Path(__file__).resolve().parents[1]
    commit = subprocess.check_output(
        ["git", "-C", str(root), "rev-parse", "HEAD"], text=True
    ).strip()
    report = {
        "schema_version": "1",
        "status": "running",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "parquetscope": {"version": parquetscope.__version__, "commit": commit},
        "runner_sha256": sha256(Path(__file__)),
        "runner_commit_note": "Runner/frontend additions are local changes based on the recorded commit",
        "source": {
            "url": SOURCE_URL,
            "page": SOURCE_PAGE,
            "size": str(SOURCE_SIZE),
            "sha256": SOURCE_SHA256,
        },
        "versions": {
            "python": platform.python_version(),
            "duckdb": duckdb.__version__,
            "pyarrow": pyarrow.__version__,
        },
        "platform": {
            "os": platform.system(),
            "release": platform.release(),
            "architecture": platform.machine(),
        },
        "preparation_settings": {**PREPARATION, "spill": True, "temp_directory": "work/spill"},
        "capture_settings": settings.model_dump(),
        "capture_process": {
            "fresh_worker_per_run": True,
            "spill": False,
            "os_cache_eviction": False,
            "order": "A1,A2,A3,B1,B2,B3,C1,C2,C3",
        },
        "writer": {**WRITER, "version": duckdb.__version__},
        "sql": SQL,
        "expected_result": EXPECTED,
        "layouts": [],
        "equality": {},
        "attempts": [],
        "metric_definitions": METRICS,
        "limitations": LIMITATIONS,
    }
    stage = "wheel"
    try:
        report["wheel_acceptance"] = wheel_provenance(wheel)
        if not report["wheel_acceptance"]["cwd_outside_checkout"]:
            raise ValueError("Execute the installed-wheel experiment from outside the checkout")
        stage = "download"
        source = work / "yellow_tripdata_2025-01.parquet"
        if source.exists():
            checked_source(source)
        else:
            download_source(source)
        checked_source(source)
        stage = "preparation"
        paths = {"A": source, "B": work / "repacked.parquet", "C": work / "sorted.parquet"}
        with duckdb.connect(config={**PREPARATION, "temp_directory": str(work / "spill")}) as con:
            report["preparation_effective_settings"] = {
                key: con.execute("SELECT current_setting(?)", [key]).fetchone()[0]
                for key in PREPARATION
            }
            con.read_parquet(str(source)).create_view("lab_source", replace=True)
            for key in ("B", "C"):
                query = "SELECT * FROM lab_source" + (
                    " ORDER BY PULocationID" if key == "C" else ""
                )
                con.execute(
                    f"COPY ({query}) TO ? (FORMAT PARQUET, COMPRESSION ZSTD, "
                    "COMPRESSION_LEVEL 3, ROW_GROUP_SIZE 122880, PARQUET_VERSION V1)",
                    [str(paths[key])],
                )
            for key, path in paths.items():
                layout = {
                    "id": key,
                    "label": LABELS[key],
                    **describe(con, path),
                    "order_by": "PULocationID" if key == "C" else None,
                    "selected_repetition": 1,
                    "repetitions": [],
                }
                if layout["rows"] != "3475226" or len(layout["schema"]) != 20:
                    raise ValueError("Unexpected TLC rows/schema")
                if report["layouts"] and layout["schema"] != report["layouts"][0]["schema"]:
                    raise ValueError("Rewriting changed logical column types/order")
                if layout["row_groups"] != (4 if key == "A" else 29):
                    raise ValueError("Unexpected persisted row group count")
                con.read_parquet(str(path)).create_view("input", replace=True)
                result = con.execute(SQL).fetchall()
                if result != [(142826, Decimal("8038349.75"))]:
                    raise ValueError("Prepared layout exact DECIMAL query result differs")
                layout["preparation_result"] = [[str(v) for v in row] for row in result]
                report["layouts"].append(layout)
            for key in ("B", "C"):
                report["equality"][f"A_{key}"] = multiset_difference(con, source, paths[key])
            report["equality"]["B_preserves_original_order"] = same_order(source, paths["B"])
        write_json(output / "report.json", report)
        (output / "captures").mkdir()
        (output / "viewers").mkdir()
        for layout in report["layouts"]:
            for repeat in range(1, 4):
                stage = f"capture-{layout['id']}-{repeat}"
                attempt = {"layout": layout["id"], "repetition": repeat, "status": "started"}
                report["attempts"].append(attempt)
                write_json(output / "report.json", report)
                print(stage, flush=True)
                run, preview = capture(paths[layout["id"]], SQL, settings)
                relative = f"captures/{layout['id']}-{repeat}.parquetscope.json"
                save_artifact(run, output / relative)  # Failed captures survive, too.
                attempt["capture"] = relative
                attempt["runtime_result"] = preview["rows"]
                verify_capture(run, preview, EXPECTED, settings, layout)
                item = {
                    "repetition": repeat,
                    "id": run.id,
                    "capture": relative,
                    "sha256": sha256(output / relative),
                    "result": exact_result(preview),
                    "runtime_result": preview["rows"],
                    "result_digest": run.result_digest,
                    "metrics": run.metrics.model_dump(),
                    "query_duration_ms": run.query_duration_ms,
                    "elapsed_ms": run.elapsed_ms,
                }
                layout["repetitions"].append(item)
                attempt["status"] = "succeeded"
                if repeat == 1:
                    layout["viewer"] = f"viewers/{layout['id']}.viewer.html"
                    (output / layout["viewer"]).write_text(viewer_html(run))
                write_json(output / "report.json", report)
        stage = "verification"
        verify_report(output)
        report["status"] = "complete"
        report["wheel_acceptance"]["result"] = "All preparation checks and nine captures passed"
    except Exception as error:
        report["status"] = "failed"
        reason = str(error)
        for path in (str(work.resolve()), str(root), str(Path.home())):
            reason = reason.replace(path, "[local]")
        if report["attempts"] and report["attempts"][-1]["status"] == "started":
            report["attempts"][-1].update(status="failed", error=reason)
        report["failure"] = {"stage": stage, "error": reason}
        raise
    finally:
        write_json(output / "report.json", report)
    public_manifest(output)
    print(f"Complete: nine captures, exact equality and DECIMAL results; {output / 'report.json'}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--wheel", type=Path)
    parser.add_argument(
        "--verify", action="store_true", help="Verify existing public report offline"
    )
    args = parser.parse_args()
    if args.verify:
        verify_report(args.output)
        print("Report matches all nine validated native captures")
    else:
        if args.work_dir is None or args.wheel is None:
            parser.error("--work-dir and --wheel are required for an experiment")
        run_experiment(args.work_dir, args.output, args.wheel)


if __name__ == "__main__":
    main()
