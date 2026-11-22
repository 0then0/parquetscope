"""Run one lab query in an isolated DuckDB environment."""

import json
import platform
import sys

import duckdb

SETTINGS = {
    "threads": 1,
    "memory_limit": "256.0 MiB",
    "enable_external_file_cache": False,
    "parquet_metadata_cache": False,
    "enable_http_metadata_cache": False,
    "disable_parquet_prefetching": False,
    "prefetch_all_parquet_files": False,
    "http_keep_alive": True,
    "http_retries": 3,
    "http_timeout": 30,
    "http_retry_wait_ms": 100,
    "http_retry_backoff": 4.0,
}


def run(
    version: str, extension_dir: str, sql: str, parameters: list, use_page_index: bool | None
) -> dict:
    if duckdb.__version__ != version:
        raise ValueError(f"Expected DuckDB {version}, found {duckdb.__version__}")
    with duckdb.connect(
        config={
            "extension_directory": extension_dir,
            "autoinstall_known_extensions": "false",
            "autoload_known_extensions": "false",
            "allow_persistent_secrets": "false",
        }
    ) as connection:
        connection.execute("LOAD httpfs")
        (httpfs,) = connection.execute(
            "SELECT extension_version FROM duckdb_extensions() "
            "WHERE extension_name = 'httpfs' AND loaded"
        ).fetchone()
        if not httpfs:
            raise ValueError("Loaded httpfs has no version")
        engine_version, revision, _ = connection.execute("PRAGMA version").fetchone()
        settings = dict(SETTINGS)
        if use_page_index is not None:
            settings["parquet_use_page_index"] = use_page_index
        for name, value in settings.items():
            connection.execute(f"SET {name} = ?", [value])

        names = [
            *SETTINGS,
            "parquet_use_page_index",
            "parquet_prefetch_column_gap",
            "httpfs_client_implementation",
            "httpfs_connection_caching",
        ]
        effective = dict(
            connection.execute(
                "SELECT name, value FROM duckdb_settings() WHERE name IN (SELECT unnest(?))",
                [names],
            ).fetchall()
        )
        for name, requested in settings.items():
            observed = effective.get(name)
            if str(observed).lower() != str(requested).lower():
                raise RuntimeError(f"{name} requested {requested}, observed {observed}")
        connection.execute(
            "CALL enable_logging(['PhysicalOperator', 'ParquetPrefetch'], storage_buffer_size=0)"
        )
        result = connection.execute(sql, parameters).fetchall()
        skip_pages = connection.execute(
            "SELECT info['row_group_id']::BIGINT, info['rows_skipped']::BIGINT, "
            "info['rows_total']::BIGINT FROM duckdb_logs_parsed('PhysicalOperator') "
            "WHERE class = 'ParquetReader' AND event = 'SkipPages'"
        ).fetchall()
        prefetch = connection.execute(
            "SELECT strategy, prefetch_groups, accepted_column_gap "
            "FROM duckdb_logs_parsed('ParquetPrefetch')"
        ).fetchall()
        return {
            "duckdb_version": duckdb.__version__,
            "engine_version": engine_version,
            "duckdb_revision": revision,
            "httpfs_version": httpfs,
            "python": platform.python_version(),
            "settings": dict.fromkeys(names) | effective,
            "result": result,
            "skip_pages": [
                {"row_group": group, "rows_skipped": skipped, "rows_total": total}
                for group, skipped, total in skip_pages
            ],
            "prefetch": [
                {"strategy": strategy, "groups": groups, "accepted_column_gap": gap}
                for strategy, groups, gap in prefetch
            ],
        }


if __name__ == "__main__":
    print(json.dumps(run(**json.loads(sys.argv[1]))))
