"""Partition-pruning lab: identical Parquet bytes in a flat and a Hive-style layout.

The same deterministic files are copied into `flat/` and `hive/month=N/`. Both sets are read
through a manifest and the same two SELECTs (one selective on `month`, one that matches every
month as a control). Data, bytes and results are verified independently of DuckDB's HTTP reads;
only the HTTP reads of the captures are measured. Hashing and footer reads for preparation
happen before each capture and are not part of its measurement.

    uv run python scripts/pruning_lab.py --output docs/pruning-lab
"""

import argparse
import hashlib
import importlib.metadata
import json
import platform
import random
import shutil
import statistics
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

import parquetscope
from parquetscope.capture import capture_set
from parquetscope.manifest import load_manifest
from parquetscope.models import Settings, load_artifact_file, save_artifact
from parquetscope.offline import viewer_html

SEED = 20261101
MONTHS = 6
FILES_PER_MONTH = 2
ROWS_PER_FILE = 20000
ROW_GROUP_SIZE = 5000
SELECTIVE_MONTH = 3
SELECT = (
    "SELECT count(*) AS n, sum(amount_cents) AS total_cents, "
    "sum(event_id % 97) AS checksum FROM input WHERE {predicate}"
)
LAYOUTS = ("flat", "hive")
METRICS = (
    "get_count",
    "head_count",
    "server_written_bytes",
    "unique_coverage_bytes",
    "files_with_get",
    "files_head_only",
    "files_without_observed_access",
    "binding_requests",
    "query_duration_ms",
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while block := source.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


QUERIES = {
    "selective": {
        "predicate": f"month = {SELECTIVE_MONTH}",
        "meaning": "One partition value; matches the rows of a single month.",
    },
    "control": {
        "predicate": "month >= 1",
        "meaning": "Same column, matches every month; no file can be excluded by the key.",
    },
}


def generate(root: Path, months: int, files_per_month: int, rows_per_file: int) -> list[dict]:
    """Write each source file once, then copy identical bytes into both layouts."""
    for layout in LAYOUTS:
        shutil.rmtree(root / layout, ignore_errors=True)
        (root / layout).mkdir(parents=True)
    files = []
    event_id = 0
    for month in range(1, months + 1):
        for index in range(files_per_month):
            rng = random.Random(f"{SEED}:{month}:{index}")
            rows = {
                "event_id": list(range(event_id, event_id + rows_per_file)),
                "month": [month] * rows_per_file,
                "account_id": [rng.randrange(10000) for _ in range(rows_per_file)],
                "amount_cents": [rng.randrange(100, 100000) for _ in range(rows_per_file)],
                "region": [f"region-{rng.randrange(8)}" for _ in range(rows_per_file)],
            }
            event_id += rows_per_file
            flat = root / "flat" / f"m{month:02d}-data-{index}.parquet"
            hive = root / "hive" / f"month={month}" / f"data-{index}.parquet"
            hive.parent.mkdir(exist_ok=True)
            pq.write_table(
                pa.table(rows),
                flat,
                row_group_size=ROW_GROUP_SIZE,
                compression="zstd",
                use_dictionary=True,
            )
            shutil.copyfile(flat, hive)
            files.append(
                {
                    "month": month,
                    "index": index,
                    "rows": rows_per_file,
                    "flat": flat,
                    "hive": hive,
                    "amounts": rows["amount_cents"],
                    "ids": rows["event_id"],
                }
            )
    return files


def write_manifests(root: Path, files: list[dict]) -> None:
    for layout in LAYOUTS:
        entries = []
        for file in files:
            path = file[layout]
            entries.append(
                {
                    "path": path.relative_to(root / layout).as_posix(),
                    "size": str(path.stat().st_size),
                    "sha256": sha256_file(path),
                }
            )
        manifest = {
            "manifest_version": "1",
            "label": f"pruning-lab-{layout}",
            "read": {"hive_partitioning": layout == "hive"},
            "files": entries,
        }
        (root / layout / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")


def verify_data(files: list[dict]) -> dict:
    """Bytes, rows and partition values compared directly with pyarrow, without DuckDB."""
    identical = all(sha256_file(f["flat"]) == sha256_file(f["hive"]) for f in files)
    tables = {layout: [pq.read_table(f[layout]) for f in files] for layout in LAYOUTS}
    path_matches = all(
        set(t.column("month").to_pylist()) == {int(f["hive"].parent.name.split("=")[1])}
        for f, t in zip(files, tables["hive"], strict=True)
    )
    combined = {layout: pa.concat_tables(tables[layout]).sort_by("event_id") for layout in LAYOUTS}
    return {
        "file_bytes_identical": identical,
        "data_equivalent": combined["flat"].equals(combined["hive"]),
        "path_values_match_stored_month": path_matches,
        "rows": combined["flat"].num_rows,
    }


def expected_result(files: list[dict], month: int | None) -> list[list[str]]:
    """Exact integer aggregation computed from the generated Python values."""
    rows = [f for f in files if month is None or f["month"] == month]
    count = sum(f["rows"] for f in rows)
    total = sum(sum(f["amounts"]) for f in rows)
    checksum = sum(i % 97 for f in rows for i in f["ids"])
    return [[str(count), str(total), str(checksum)]]


def run_metrics(run) -> dict:
    by_id = {f.id: f.logical_path for f in run.file_set.files}
    access = {"get": [], "head_only": [], "none": []}
    for m in run.metrics.files:
        access[m.observed_access].append(by_id[m.file_id])
    return {
        "get_count": run.metrics.get_count,
        "head_count": run.metrics.head_count,
        "server_written_bytes": int(run.metrics.server_written_bytes),
        "unique_coverage_bytes": int(run.metrics.unique_coverage_bytes),
        "files_with_get": run.metrics.files_with_get,
        "files_head_only": run.metrics.files_head_only,
        "files_without_observed_access": run.metrics.files_without_observed_access,
        "binding_requests": sum(o.phase == "view_creation" for o in run.observations),
        "unattributed_requests": sum(o.phase == "unattributed" for o in run.observations),
        "query_duration_ms": round(run.query_duration_ms, 3),
        "files": access,
        "per_file": {
            by_id[m.file_id]: {
                "get": m.get_count,
                "head": m.head_count,
                "server_written_bytes": int(m.server_written_bytes),
            }
            for m in run.metrics.files
        },
    }


def spread(values: list[float]) -> dict:
    return {"min": min(values), "median": statistics.median(values), "max": max(values)}


def run_lab(
    output: Path,
    repetitions: int = 3,
    months: int = MONTHS,
    files_per_month: int = FILES_PER_MONTH,
    rows_per_file: int = ROWS_PER_FILE,
) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    files = generate(output, months, files_per_month, rows_per_file)
    write_manifests(output, files)
    checks = verify_data(files)
    if not all(v is True for k, v in checks.items() if k != "rows"):
        raise RuntimeError(f"Layouts are not equivalent: {checks}")
    settings = Settings(threads=4, memory_limit_mb=512, deadline_seconds=60, preview_rows=5)
    for name in ("captures", "viewers"):
        shutil.rmtree(output / name, ignore_errors=True)
        (output / name).mkdir()
    if months < SELECTIVE_MONTH:
        raise ValueError(f"The selective query needs at least {SELECTIVE_MONTH} months")
    plan = QUERIES
    runs, results_ok, digests = [], True, {}
    for query_id, query in plan.items():
        sql = SELECT.format(predicate=query["predicate"])
        month = SELECTIVE_MONTH if query_id == "selective" else None
        expected = expected_result(files, month)
        for layout in LAYOUTS:
            spec = load_manifest(output / layout / "manifest.json")
            for repetition in range(1, repetitions + 1):
                run, preview = capture_set(spec, sql, settings)
                if run.sql_status != "succeeded" or not run.capture.complete:
                    raise RuntimeError(f"Capture failed: {run.errors} {run.capture.reason}")
                if preview["rows"] != expected:
                    results_ok = False
                    raise RuntimeError(f"Result differs from expectation: {preview['rows']}")
                digests.setdefault(query_id, set()).add(run.result_digest)
                name = f"{layout}-{query_id}-{repetition}.parquetscope.json"
                save_artifact(run, output / "captures" / name)
                runs.append(
                    {
                        "layout": layout,
                        "query": query_id,
                        "repetition": repetition,
                        "capture": f"captures/{name}",
                        **run_metrics(run),
                    }
                )
                if repetition == 1:
                    viewer = output / "viewers" / f"{layout}-{query_id}.viewer.html"
                    viewer.write_text(viewer_html(load_artifact_file(output / "captures" / name)))
    summary = {}
    for query_id in plan:
        for layout in LAYOUTS:
            group = [r for r in runs if r["layout"] == layout and r["query"] == query_id]
            summary[f"{layout}/{query_id}"] = {m: spread([r[m] for r in group]) for m in METRICS}
    report = {
        "schema_version": "1",
        "seed": SEED,
        "dataset": {
            "months": months,
            "files_per_month": files_per_month,
            "rows_per_file": rows_per_file,
            "row_group_size": ROW_GROUP_SIZE,
            "compression": "zstd",
            "columns": ["event_id", "month", "account_id", "amount_cents", "region"],
            "files": [
                {
                    "flat_path": f["flat"].relative_to(output / "flat").as_posix(),
                    "hive_path": f["hive"].relative_to(output / "hive").as_posix(),
                    "rows": f["rows"],
                    "size": f["flat"].stat().st_size,
                    "sha256": sha256_file(f["flat"]),
                }
                for f in files
            ],
        },
        "versions": {
            "parquetscope": parquetscope.__version__,
            "duckdb": duckdb.__version__,
            "pyarrow": importlib.metadata.version("pyarrow"),
            "httpfs": run.engine.httpfs_version,
            "platform": platform.platform(),
        },
        "settings": settings.model_dump(),
        "queries": {
            query_id: {
                "sql": SELECT.format(predicate=query["predicate"]),
                "meaning": query["meaning"],
                "expected": expected_result(
                    files, SELECTIVE_MONTH if query_id == "selective" else None
                ),
            }
            for query_id, query in plan.items()
        },
        "checks": {
            **checks,
            "results_match_independent_expectation": results_ok,
            "ordered_digests_match_across_layouts": all(len(d) == 1 for d in digests.values()),
        },
        "repetitions": repetitions,
        "runs": runs,
        "summary": summary,
        "layout_notes": {
            "flat": "hive_partitioning=false; month is read from the files themselves.",
            "hive": "hive_partitioning=true; month also appears in the path. The files "
            "store the same value, which the lab verifies, so the bytes are identical.",
        },
        "limitations": [
            "Loopback HTTP, not S3: an explicit URL list does not reproduce object-store listing.",
            "Cold client per run; OS and disk caches are not controlled.",
            "Server-written bytes are not total application I/O and not bytes DuckDB used.",
            "Hashing and footer reads for manifest preparation are outside the measurement.",
            "Counts can vary between runs; the spread above is observed, not a guarantee.",
        ],
    }
    (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    sums = sorted(
        [
            *(p for p in (output / "captures").glob("*.json")),
            *(p for p in (output / "viewers").glob("*.html")),
            output / "report.json",
            output / "flat" / "manifest.json",
            output / "hive" / "manifest.json",
        ]
    )
    (output / "SHA256SUMS.txt").write_text(
        "".join(f"{sha256_file(p)}  {p.relative_to(output).as_posix()}\n" for p in sums)
    )
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--output", type=Path, default=Path("docs/pruning-lab"))
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--months", type=int, default=MONTHS)
    parser.add_argument("--files-per-month", type=int, default=FILES_PER_MONTH)
    parser.add_argument("--rows-per-file", type=int, default=ROWS_PER_FILE)
    args = parser.parse_args()
    report = run_lab(
        args.output, args.repetitions, args.months, args.files_per_month, args.rows_per_file
    )
    for key, value in report["summary"].items():
        print(
            f"{key:18} GET {value['get_count']['median']:>5} "
            f"files with GET {value['files_with_get']['median']:>3} "
            f"server wrote {int(value['server_written_bytes']['median']):>9}"
        )
    print(json.dumps(report["checks"], indent=2))


if __name__ == "__main__":
    main()
