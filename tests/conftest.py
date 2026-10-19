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
