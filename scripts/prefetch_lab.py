"""Prefetch lab: DuckDB 1.5.6 and 2.0.0, Parquet prefetch on and off, one fixed file set.

The data follows the shape reported in duckdb/duckdb#25704: one old file that holds a single
large row group, and several small files with newer rows, read through a filtered TOP-N query.
Every run goes through the regular capture path in a fresh worker process inside the isolated
environment of its engine. Results are checked against a control computed with pyarrow alone.
Differences between configurations are judged from the server's own request trace.

    uv run parquetscope setup --engine 1.5.6
    uv run parquetscope setup --engine 2.0.0
    uv run python scripts/prefetch_lab.py calibrate     # optional: choose the memory limit
    uv run python scripts/prefetch_lab.py run --output docs/prefetch-lab
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

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

import parquetscope
from parquetscope.capture import capture_set
from parquetscope.engines import SUPPORTED_ENGINES, read_manifest
from parquetscope.manifest import load_manifest
from parquetscope.models import load_artifact_file, save_artifact
from parquetscope.models_v3 import Settings3
from parquetscope.offline import viewer_html

SEED = 20261107
OLD_ROWS = 400_000
OLD_ROW_GROUP = 400_000  # one row group
NEW_FILES = 7
NEW_ROWS = 40_000
NEW_ROW_GROUP = 10_000
PAGE_SIZE = 32 * 1024
TS_MAX = 1_000_000_000
NEW_TS_MIN = 600_000_000
LIMIT = 200
MEMORY_LADDER = (64, 96, 128, 192, 256, 384, 512, 768, 1024)
MEMORY_LIMIT_MB = 256
THREADS = 4
SQL = (
    "SELECT id, ts, account_id, category, amount_cents, note FROM input "
    "WHERE amount_cents >= 5000 AND category <> 'void' "
    f"ORDER BY ts DESC, id DESC LIMIT {LIMIT}"
)
CATEGORIES = ["card", "cash", "wire", "refund", "fee", "void", "credit", "debit"]
CONFIGS = [(engine, prefetch) for engine in SUPPORTED_ENGINES for prefetch in ("on", "off")]
FIXED_SETTINGS = (
    "threads",
    "memory_limit",
    "enable_external_file_cache",
    "enable_http_metadata_cache",
    "http_keep_alive",
    "http_retries",
    "http_timeout",
    "http_retry_wait_ms",
    "http_retry_backoff",
)
COLUMNS = ["id", "ts", "account_id", "category", "amount_cents", "note"]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while block := source.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def config_name(engine: str, prefetch: str) -> str:
    return f"duckdb-{engine}-prefetch-{prefetch}"


def rows_for(name: str, first_id: int, count: int, ts_min: int) -> dict:
    rng = random.Random(f"{SEED}:{name}")
    return {
        "id": list(range(first_id, first_id + count)),
        "ts": [rng.randrange(ts_min, TS_MAX) for _ in range(count)],
        "account_id": [rng.randrange(50_000) for _ in range(count)],
        "category": [CATEGORIES[rng.randrange(len(CATEGORIES))] for _ in range(count)],
        "amount_cents": [rng.randrange(100, 100_000) for _ in range(count)],
        "note": [f"{rng.getrandbits(160):040x}" for _ in range(count)],
    }


def generate(data: Path, old_rows: int, new_files: int, new_rows: int) -> list[Path]:
    """The old file is listed first; its single row group spans the whole timestamp range."""
    shutil.rmtree(data, ignore_errors=True)
    data.mkdir(parents=True)
    plan = [("part-00-old", old_rows, 0)]
    plan += [(f"part-{i:02d}-new", new_rows, NEW_TS_MIN) for i in range(1, new_files + 1)]
    paths, next_id = [], 0
    for name, count, ts_min in plan:
        table = pa.table(rows_for(name, next_id, count, ts_min))
        path = data / f"{name}.parquet"
        pq.write_table(
            table,
            path,
            row_group_size=old_rows if name.endswith("old") else NEW_ROW_GROUP,
            data_page_size=PAGE_SIZE,
            compression="zstd",
            use_dictionary=["category"],
        )
        paths.append(path)
        next_id += count
    return paths


def write_manifest(data: Path, paths: list[Path]) -> Path:
    manifest = {
        "manifest_version": "1",
        "label": "prefetch-lab",
        "read": {"hive_partitioning": False},
        "files": [
            {
                "path": p.relative_to(data).as_posix(),
                "size": str(p.stat().st_size),
                "sha256": sha256_file(p),
            }
            for p in paths
        ],
    }
    target = data / "manifest.json"
    target.write_text(json.dumps(manifest, indent=2) + "\n")
    return target


def control(paths: list[Path]) -> dict:
    """The expected result and file facts, from pyarrow alone: no DuckDB, no HTTP."""
    tables = [pq.read_table(p) for p in paths]
    table = pa.concat_tables(tables)
    kept = table.filter(
        pc.and_(
            pc.greater_equal(table["amount_cents"], 5000),
            pc.not_equal(table["category"], "void"),
        )
    )
    top = kept.sort_by([("ts", "descending"), ("id", "descending")]).slice(0, LIMIT)
    rows = [
        [str(v) for v in row] for row in zip(*(top[c].to_pylist() for c in COLUMNS), strict=True)
    ]
    digest = hashlib.sha256()
    for row in rows:
        encoded = json.dumps(row, ensure_ascii=False).encode()
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    from_old = sum(1 for r in rows if int(r[0]) < tables[0].num_rows)
    return {
        "rows": len(rows),
        "digest": digest.hexdigest(),
        "first_rows": rows[:5],
        "top_rows_from_old_file": from_old,
        "total_rows": table.num_rows,
        "row_groups": [pq.ParquetFile(p).num_row_groups for p in paths],
    }


def settings_for(prefetch: str, memory_mb: int) -> Settings3:
    return Settings3(
        threads=THREADS,
        memory_limit_mb=memory_mb,
        deadline_seconds=240,
        preview_rows=5,
        disable_parquet_prefetching=prefetch == "off",
    )


def one_run(spec, engine: str, prefetch: str, memory_mb: int):
    return capture_set(spec, SQL, settings_for(prefetch, memory_mb), engine=engine)


def run_metrics(run) -> dict:
    gets = [s for s in run.server_observations if s.method == "GET"]
    return {
        "get_count": run.metrics.get_count,
        "head_count": run.metrics.head_count,
        "server_get_count": len(gets),
        "server_head_count": len(run.server_observations) - len(gets),
        "requested_range_bytes": run.metrics.requested_range_bytes,
        "confirmed_range_bytes": run.metrics.confirmed_range_bytes,
        "unique_coverage_bytes": run.metrics.unique_coverage_bytes,
        "repeated_coverage_bytes": run.metrics.repeated_coverage_bytes,
        "server_written_bytes": run.metrics.server_written_bytes,
        "binding_requests": sum(o.phase == "view_creation" for o in run.observations),
        "files_with_get": run.metrics.files_with_get,
        "query_duration_ms": round(run.query_duration_ms or 0, 3),
        "elapsed_ms": round(run.elapsed_ms, 3),
    }


def spread(values: list[float]) -> dict:
    return {"min": min(values), "median": statistics.median(values), "max": max(values)}


def calibrate(data: Path, output: Path, ladder=MEMORY_LADDER) -> dict:
    """One capture per configuration at rising limits; the first limit all four pass."""
    paths = generate(data, OLD_ROWS, NEW_FILES, NEW_ROWS)
    spec = load_manifest(write_manifest(data, paths))
    expected = control(paths)
    attempts, chosen = [], None
    for memory in ladder:
        outcomes = {}
        for engine, prefetch in CONFIGS:
            run, _ = one_run(spec, engine, prefetch, memory)
            ok = (
                run.sql_status == "succeeded"
                and run.capture.complete
                and run.result_digest == expected["digest"]
            )
            outcomes[config_name(engine, prefetch)] = (
                "ok" if ok else (run.errors or ["failed"])[0][:120]
            )
        attempts.append({"memory_limit_mb": memory, "outcomes": outcomes})
        if all(v == "ok" for v in outcomes.values()):
            chosen = memory
            break
    result = {"ladder": list(ladder), "attempts": attempts, "lowest_passing_mb": chosen}
    output.mkdir(parents=True, exist_ok=True)
    (output / "calibration.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def rotation(repetition: int) -> list[tuple[str, str]]:
    shift = (repetition - 1) % len(CONFIGS)
    return CONFIGS[shift:] + CONFIGS[:shift]


def run_lab(
    output: Path,
    data: Path,
    repetitions: int = 3,
    memory_mb: int = MEMORY_LIMIT_MB,
    old_rows: int = OLD_ROWS,
    new_files: int = NEW_FILES,
    new_rows: int = NEW_ROWS,
) -> dict:
    for engine in SUPPORTED_ENGINES:
        if read_manifest(engine) is None:
            raise RuntimeError(f"Run `parquetscope setup --engine {engine}` first")
    paths = generate(data, old_rows, new_files, new_rows)
    manifest = write_manifest(data, paths)
    spec = load_manifest(manifest)
    expected = control(paths)
    for name in ("captures", "viewers"):
        shutil.rmtree(output / name, ignore_errors=True)
        (output / name).mkdir(parents=True, exist_ok=True)
    runs, engines, effective = [], {}, {}
    for repetition in range(1, repetitions + 1):
        for position, (engine, prefetch) in enumerate(rotation(repetition), start=1):
            run, preview = one_run(spec, engine, prefetch, memory_mb)
            name = config_name(engine, prefetch)
            if run.sql_status != "succeeded" or not run.capture.complete:
                raise RuntimeError(
                    f"{name} #{repetition} failed: {run.errors} {run.capture.reason}"
                )
            if run.result_digest != expected["digest"] or run.result_rows != str(expected["rows"]):
                raise RuntimeError(f"{name} #{repetition} differs from the pyarrow control")
            if preview["rows"] != expected["first_rows"]:
                raise RuntimeError(f"{name} #{repetition} preview differs from the control")
            evidence = run.engine.prefetch
            if evidence.status != "verified" or evidence.applied != (
                "true" if prefetch == "off" else "false"
            ):
                raise RuntimeError(f"{name} #{repetition}: prefetch not confirmed: {evidence}")
            capture_name = f"{name}-{repetition}.parquetscope.json"
            save_artifact(run, output / "captures" / capture_name)
            engines[engine] = {
                "duckdb": run.duckdb_version,
                "httpfs": run.engine.httpfs_version,
                "python": run.engine.python_version,
                "request_correlation": run.engine.request_correlation,
            }
            effective[name] = run.engine.effective_settings
            runs.append(
                {
                    "config": name,
                    "engine": engine,
                    "prefetch": prefetch,
                    "repetition": repetition,
                    "position_in_round": position,
                    "capture": f"captures/{capture_name}",
                    **run_metrics(run),
                }
            )
            if repetition == 1:
                viewer = output / "viewers" / f"{name}.viewer.html"
                viewer.write_text(
                    viewer_html(load_artifact_file(output / "captures" / capture_name))
                )

    def normalized(settings: dict) -> dict:
        out = {}
        for key in FIXED_SETTINGS:
            value = settings.get(key)
            try:
                out[key] = float(value)
            except (TypeError, ValueError):
                out[key] = value
        return out

    reference = normalized(next(iter(effective.values())))
    fixed_identical = all(normalized(s) == reference for s in effective.values())
    metrics = (
        "get_count",
        "head_count",
        "server_get_count",
        "server_head_count",
        "server_written_bytes",
        "confirmed_range_bytes",
        "query_duration_ms",
    )
    summary = {}
    for engine, prefetch in CONFIGS:
        name = config_name(engine, prefetch)
        group = [r for r in runs if r["config"] == name]
        summary[name] = {m: spread([float(r[m]) for r in group]) for m in metrics}
    files = [
        {
            "path": p.name,
            "size": p.stat().st_size,
            "sha256": sha256_file(p),
            "rows": pq.ParquetFile(p).metadata.num_rows,
            "row_groups": pq.ParquetFile(p).num_row_groups,
        }
        for p in paths
    ]
    report = {
        "schema_version": "1",
        "seed": SEED,
        "dataset": {
            "description": "One old file with a single large row group plus newer small files.",
            "page_size_bytes": PAGE_SIZE,
            "compression": "zstd",
            "columns": COLUMNS,
            "files": files,
        },
        "query": {"sql": SQL, "expected": {k: v for k, v in expected.items()}},
        "versions": {
            "parquetscope": parquetscope.__version__,
            "pyarrow": importlib.metadata.version("pyarrow"),
            "platform": platform.platform(),
            "engines": engines,
        },
        "fixed_settings": {
            "memory_limit_mb": memory_mb,
            "threads": THREADS,
            "external_file_cache": False,
            "compared": list(FIXED_SETTINGS),
            "effective_by_config": effective,
            "identical_after_numeric_normalization": fixed_identical,
        },
        "repetitions": repetitions,
        "order": "Configurations rotate by one position each repetition; fresh worker per run.",
        "runs": runs,
        "summary": summary,
        "checks": {
            "results_match_pyarrow_control": True,  # run_lab raises otherwise
            "all_captures_complete_and_reconciled": True,
            "prefetch_setting_confirmed_by_engine_readback": True,
            "fixed_settings_identical": fixed_identical,
        },
        "limitations": [
            "Loopback HTTP on one machine, not S3: no network latency, no object-store listing.",
            "The request counts of duckdb/duckdb#25704 (33 vs 1,697 GET) belong to its dataset "
            "and S3 environment and are not expected here.",
            "Server records count requests and bytes written by the local listener. They are not "
            "bytes DuckDB received, decoded or used.",
            "DuckDB 2.0.0 redacts the request ID header; its log rows were paired with server "
            "records by request content (ordered_tuple_match).",
            "Cold client per run; OS and disk caches are uncontrolled. Durations are observed "
            "spreads over a few repetitions, not benchmarks.",
            "Hashing and footer reads for manifest preparation are outside the measurement.",
        ],
    }
    cal = output / "calibration.json"
    if cal.exists():
        report["memory_calibration"] = json.loads(cal.read_text())
    (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    manifest_copy = output / "manifest.json"
    shutil.copyfile(manifest, manifest_copy)
    sums = sorted(
        [
            *(output / "captures").glob("*.json"),
            *(output / "viewers").glob("*.html"),
            output / "report.json",
            manifest_copy,
        ]
    )
    (output / "SHA256SUMS.txt").write_text(
        "".join(f"{sha256_file(p)}  {p.relative_to(output).as_posix()}\n" for p in sums)
    )
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("calibrate", "run"):
        command = sub.add_parser(name)
        command.add_argument("--output", type=Path, default=Path("docs/prefetch-lab"))
        command.add_argument("--data", type=Path, default=Path(".runtime/prefetch-lab"))
    sub.choices["run"].add_argument("--repetitions", type=int, default=3)
    sub.choices["run"].add_argument("--memory-limit-mb", type=int, default=MEMORY_LIMIT_MB)
    args = parser.parse_args()
    if args.command == "calibrate":
        print(json.dumps(calibrate(args.data, args.output), indent=2))
        return
    report = run_lab(args.output, args.data, args.repetitions, args.memory_limit_mb)
    for name, value in report["summary"].items():
        print(
            f"{name:28} GET(server) {value['server_get_count']['median']:>6.0f} "
            f"HEAD {value['server_head_count']['median']:>3.0f} "
            f"wrote {value['server_written_bytes']['median'] / 1048576:>7.1f} MiB "
            f"SELECT {value['query_duration_ms']['median']:>8.1f} ms"
        )
    print(json.dumps(report["checks"], indent=2))


if __name__ == "__main__":
    main()
