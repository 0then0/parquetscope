"""Deterministic dataset, measured runs, independently checked logical equality."""

import json
import random
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from .capture import capture
from .models import Settings, save_artifact

SEED = 20261019
ROWS = 262144
ROW_GROUP_SIZE = 4096
SQL = (
    "SELECT count(*) AS matched_rows, sum(amount_cents) AS total_cents, "
    "sum(event_id % 97) AS checksum FROM input WHERE customer_id BETWEEN 1000 AND 1099"
)


def generate(directory: Path):
    directory.mkdir(parents=True, exist_ok=True)
    rng = random.Random(SEED)
    customers = [rng.randrange(100000) for _ in range(ROWS)]
    amounts = [rng.randrange(100, 100000) for _ in range(ROWS)]
    table = pa.table(
        {
            "event_id": pa.array(range(ROWS), type=pa.int64()),
            "customer_id": pa.array(customers, type=pa.int64()),
            "amount_cents": pa.array(amounts, type=pa.int64()),
            "region": [f"region-{i % 8}" for i in range(ROWS)],
        }
    )
    original, sorted_path = directory / "original.parquet", directory / "sorted.parquet"
    options = {"row_group_size": ROW_GROUP_SIZE, "compression": "zstd", "use_dictionary": True}
    pq.write_table(table, original, **options)
    pq.write_table(table.sort_by("customer_id"), sorted_path, **options)
    # Read persisted files and compare every value after restoring original row order.
    equivalent = pq.read_table(original).equals(pq.read_table(sorted_path).sort_by("event_id"))
    if not equivalent:
        raise RuntimeError("Demo layouts contain different data")
    indices = [i for i, customer in enumerate(customers) if 1000 <= customer <= 1099]
    expected = [
        [
            str(len(indices)),
            str(sum(amounts[i] for i in indices)),
            str(sum(i % 97 for i in indices)),
        ]
    ]
    return original, sorted_path, expected


def run_demo(directory: Path):
    original, sorted_path, expected = generate(directory)
    settings = Settings(threads=4, memory_limit_mb=512, deadline_seconds=30, preview_rows=30)
    runs = []
    for path in (original, sorted_path):
        run, preview = capture(path, SQL, settings)
        if run.sql_status != "succeeded" or not run.capture.complete:
            raise RuntimeError(f"Demo capture failed: {run.errors} {run.capture.reason}")
        if preview["rows"] != expected:
            raise RuntimeError(f"Demo result differs from independent expectation: {preview}")
        save_artifact(run, directory / f"{path.stem}.parquetscope.json")
        runs.append(run)
    if runs[0].result_digest != runs[1].result_digest:
        raise RuntimeError("Demo query results differ")
    report = {
        "seed": SEED,
        "rows": ROWS,
        "row_group_size": ROW_GROUP_SIZE,
        "compression": "zstd",
        "dictionary": True,
        "sql": SQL,
        "data_equivalence_verified": True,
        "expected_result": expected,
        "result_equivalence_verified": True,
        "conditions": "Fresh worker per run; cold client only; OS/disk caches uncontrolled",
        "runs": [
            {
                "file": r.input.name,
                "size": r.input.size,
                "duckdb": r.duckdb_version,
                "pyarrow": r.pyarrow_version,
                "platform": r.platform,
                "metrics": r.metrics.model_dump(),
                "query_duration_ms": r.query_duration_ms,
            }
            for r in runs
        ],
    }
    (directory / "report.json").write_text(json.dumps(report, indent=2))
    return runs, report
