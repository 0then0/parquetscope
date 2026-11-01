import json

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from parquetscope.capture import capture, extension_directory


@pytest.fixture
def parquet(tmp_path):
    path = tmp_path / "input.parquet"
    pq.write_table(
        pa.table(
            {
                "id": range(20000),
                "key": [i % 71 for i in range(20000)],
                "label": [f"label-{i % 5}" for i in range(20000)],
            }
        ),
        path,
        row_group_size=1000,
        use_dictionary=True,
    )
    return path


@pytest.fixture(scope="session")
def httpfs():
    # Tests never download extensions. Run `parquetscope setup` explicitly first.
    directory = extension_directory()
    with duckdb.connect(
        config={"extension_directory": str(directory), "autoinstall_known_extensions": "false"}
    ) as connection:
        connection.execute("LOAD httpfs")
    return directory


@pytest.fixture
def measured(parquet, httpfs):
    run, preview = capture(parquet, "SELECT sum(id) AS total FROM input WHERE key=42")
    assert run.sql_status == "succeeded", run.errors
    return run, preview


def write_table(path, month=1, rows=3000, row_group_size=1000):
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.table(
            {
                "id": pa.array(range(rows), pa.int64()),
                "month": pa.array([month] * rows, pa.int64()),
                "v": pa.array([i % 7 for i in range(rows)], pa.int64()),
            }
        ),
        path,
        row_group_size=row_group_size,
    )
    return path


def write_manifest(root, paths, hive=True, **extra):
    manifest = {
        "manifest_version": "1",
        "label": "test-set",
        "read": {"hive_partitioning": hive},
        "files": [{"path": p} for p in paths],
        **extra,
    }
    path = root / "manifest.json"
    path.write_text(json.dumps(manifest))
    return path


@pytest.fixture
def hive_set(tmp_path):
    """Three files that share the basename data.parquet, in month=N directories."""
    paths = []
    for month in (1, 2, 3):
        write_table(tmp_path / f"month={month}" / "data.parquet", month)
        paths.append(f"month={month}/data.parquet")
    return write_manifest(tmp_path, paths)


@pytest.fixture
def set_measured(hive_set, httpfs):
    from parquetscope.capture import capture_set
    from parquetscope.manifest import load_manifest

    run, preview = capture_set(
        load_manifest(hive_set), "SELECT count(*) AS n, sum(v) AS s FROM input WHERE month = 2"
    )
    assert run.sql_status == "succeeded", run.errors
    return run, preview
