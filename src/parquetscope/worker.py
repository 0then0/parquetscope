"""Fresh interpreter per run. Only structured logs from this process are captured.

This file runs as a plain script inside whichever environment hosts the selected DuckDB
version, so it may import only the standard library and `duckdb`.
"""

import hashlib
import json
import platform
import sys
import time
from pathlib import Path

import duckdb


def json_value(value):
    """For runtime preview only; strings preserve decimals, integers and binary values."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (str, int, float)):
        return str(value)
    return json.dumps(value, default=str, ensure_ascii=False)


# Constant SQL only: the URL list reaches DuckDB as a bound parameter, never as SQL text.
SET_VIEW_SQL = {
    False: "CREATE VIEW input AS SELECT * FROM read_parquet(getvariable('input_urls'), "
    "hive_partitioning = false, union_by_name = false)",
    True: "CREATE VIEW input AS SELECT * FROM read_parquet(getvariable('input_urls'), "
    "hive_partitioning = true, union_by_name = false)",
}
# Constant SQL only: the requested value never becomes SQL text.
SET_PREFETCH_SQL = {
    True: "SET disable_parquet_prefetching = true",
    False: "SET disable_parquet_prefetching = false",
}
# httpfs options, pinned after LOAD so every engine version runs with the same values.
PINNED_HTTP_SQL = (
    "SET enable_http_metadata_cache = false",
    "SET http_keep_alive = true",
    "SET http_retries = 3",
    "SET http_timeout = 30",
    "SET http_retry_wait_ms = 100",
    "SET http_retry_backoff = 4",
)
EFFECTIVE_SETTINGS = (
    "disable_parquet_prefetching",
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


def apply_prefetch(connection, requested: bool | None, engine: dict) -> None:
    """Set the requested value, then read it back; an explicit request must be confirmed."""
    evidence = engine["prefetch"]
    evidence["requested"] = requested
    if requested is not None:
        try:
            connection.execute(SET_PREFETCH_SQL[requested])
        except duckdb.Error as error:
            raise ValueError(f"disable_parquet_prefetching is unavailable: {error}") from error
    try:
        value = connection.execute("SELECT current_setting('disable_parquet_prefetching')")
        applied = str(value.fetchone()[0]).lower()
    except duckdb.Error as error:
        if requested is not None:
            raise ValueError(f"disable_parquet_prefetching cannot be verified: {error}") from error
        return  # Default left alone and unreadable: stays "unverified".
    evidence["applied"] = applied
    if requested is None:
        evidence["status"] = "engine_default"
    elif applied == str(requested).lower():
        evidence["status"] = "verified"
    else:
        raise ValueError(
            f"disable_parquet_prefetching requested {str(requested).lower()}, "
            f"engine reports {applied}"
        )


def execute(config: dict, result_path: Path):
    result = {
        "status": "failed",
        "errors": [],
        "logs": [],
        "logs_complete": False,
        "preview": [],
        "columns": [],
        "result_rows": None,
        "result_digest": None,
        "query_duration_ms": None,
        "engine": {
            "duckdb_version": duckdb.__version__,
            "python_version": platform.python_version(),
            "httpfs_version": None,
            "effective_settings": {},
            "bound_schema": None,
            # status stays "unverified" until the value is read back from the engine.
            "prefetch": {"requested": None, "applied": None, "status": "unverified"},
        },
        "phases": None,
    }
    connection = None
    try:
        expected = config.get("expected_duckdb_version")
        if expected and duckdb.__version__ != expected:
            raise ValueError(f"Environment runs DuckDB {duckdb.__version__}, expected {expected}")
        connection = duckdb.connect(
            config={
                "extension_directory": config["extension_dir"],
                "threads": str(config["settings"]["threads"]),
                "memory_limit": f"{config['settings']['memory_limit_mb']}MiB",
                "temp_directory": "",
                "max_temp_directory_size": "0B",
                "autoinstall_known_extensions": "false",
                "autoload_known_extensions": "false",
                "allow_persistent_secrets": "false",
                "enable_external_file_cache": "false",
            }
        )
        connection.execute("LOAD httpfs")  # Never INSTALL during capture.
        for statement in PINNED_HTTP_SQL:
            connection.execute(statement)
        urls = config.get("urls")
        row = connection.execute(
            "SELECT extension_version FROM duckdb_extensions() WHERE extension_name = 'httpfs'"
        ).fetchone()
        result["engine"]["httpfs_version"] = row[0] if row else None
        statements = connection.extract_statements(config["sql"])
        if len(statements) != 1 or statements[0].type != duckdb.StatementType.SELECT:
            raise ValueError("Run exactly one SELECT statement using the input view")
        apply_prefetch(connection, config.get("disable_parquet_prefetching"), result["engine"])
        # Before the lock: DuckDB 2.0 treats enable_logging as configuration. No HTTP happens
        # until the view is bound, so no request precedes the log.
        connection.execute("CALL enable_logging('HTTP', storage_buffer_size=0)")
        connection.execute("SET allowed_paths = ?", [urls if urls else [config["url"]]])
        connection.execute("SET enable_external_access = false")
        connection.execute("SET lock_configuration = true")
        for name in EFFECTIVE_SETTINGS:
            try:
                value = connection.execute("SELECT current_setting(?)", [name]).fetchone()[0]
                result["engine"]["effective_settings"][name] = str(value)
            except duckdb.Error:
                pass  # Not every setting exists in every version; absent means unrecorded.
        bind_started = time.time() * 1000
        if urls:
            connection.execute("SET VARIABLE input_urls = ?", [urls])
            connection.execute(SET_VIEW_SQL[bool(config["read"]["hive_partitioning"])])
        else:
            # URL is generated by our server, never taken from SQL or interpolated paths.
            connection.execute(
                f"CREATE VIEW input AS SELECT * FROM read_parquet('{config['url']}')"
            )
        bind_finished = time.time() * 1000
        started = time.monotonic()
        select_started = time.time() * 1000
        result["phases"] = {
            "bind_started_ms": bind_started,
            "bind_finished_ms": bind_finished,
            "select_started_ms": select_started,
        }
        cursor = connection.execute(statements[0])
        result["columns"] = [str(c[0]) for c in cursor.description]
        rows = 0
        preview_bytes = 0
        digest = hashlib.sha256()
        while batch := cursor.fetchmany(512):
            for row in batch:
                values = [json_value(v) for v in row]
                encoded = json.dumps(values, ensure_ascii=False).encode()
                digest.update(len(encoded).to_bytes(8, "big"))
                digest.update(encoded)
                rows += 1
                if (
                    len(result["preview"]) < config["settings"]["preview_rows"]
                    and preview_bytes + len(encoded) <= config["settings"]["preview_bytes"]
                ):
                    result["preview"].append(values)
                    preview_bytes += len(encoded)
        # The view's bound columns come from the catalog; this performs no HTTP access.
        if urls:
            result["engine"]["bound_schema"] = [
                {"name": name, "type": kind}
                for name, kind in connection.execute(
                    "SELECT column_name, data_type FROM duckdb_columns() "
                    "WHERE table_name = 'input' ORDER BY column_index"
                ).fetchall()
            ]
        result.update(
            status="succeeded",
            result_rows=str(rows),
            result_digest=digest.hexdigest(),
            query_duration_ms=(time.monotonic() - started) * 1000,
        )
    except Exception as error:
        result["errors"].append(str(error))
    finally:
        if connection:
            try:
                logs = connection.execute(
                    "SELECT to_json(request), to_json(response) FROM duckdb_logs_parsed('HTTP')"
                ).fetchmany(100001)
                if len(logs) > 100000:
                    result["errors"].append("HTTP log limit exceeded")
                result["logs_complete"] = len(logs) <= 100000
                result["logs"] = [
                    {"request": json.loads(req), "response": json.loads(res)}
                    for req, res in logs[:100000]
                ]
            except Exception as error:
                result["errors"].append(f"HTTP log collection failed: {error}")
            connection.close()
        result_path.write_text(json.dumps(result), encoding="utf-8")


if __name__ == "__main__":
    execute(json.loads(Path(sys.argv[1]).read_text()), Path(sys.argv[2]))
