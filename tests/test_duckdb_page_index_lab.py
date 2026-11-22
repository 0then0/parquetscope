from pathlib import Path

import pyarrow.parquet as pq
import pytest

from parquetscope.models import span
from parquetscope.models_v4 import ServerObservation4

ROOT = Path(__file__).resolve().parents[1]
with pytest.MonkeyPatch.context() as monkeypatch:
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    import duckdb_page_index_lab as lab


REGIONS = {
    "id.page.0": {"kind": "data_page", "span": [10, 30], "contains_point_row": False},
    "id.page.1": {"kind": "data_page", "span": [30, 50], "contains_point_row": True},
    "id.column_index": {"kind": "column_index", "span": [50, 60]},
    "id.offset_index": {"kind": "offset_index", "span": [60, 65]},
    "footer": {"kind": "footer", "span": [65, 80]},
}


def response(start, end, *, written=None, request_id="get"):
    declared = end - start
    written = declared if written is None else written
    complete = written == declared
    return ServerObservation4(
        id=request_id,
        method="GET",
        requested_range=f"bytes={start}-{end - 1}",
        response_span=span(start, end),
        status=206,
        start_ms=1.0,
        duration_ms=2.0,
        declared_body_bytes=str(declared),
        written_body_bytes=str(written),
        complete=complete,
        error=None if complete else "disconnect",
        profile_delay_started_ms=1.0,
        profile_delay_ms=0.0,
        headers_sent_ms=1.5,
        body_budget_wait_ms=0.0,
        body_write_started_ms=1.5,
        body_write_finished_ms=2.5,
        body_write_ms=1.0,
        reserved_body_bytes=str(declared),
        termination="completed" if complete else "disconnect",
    )


def test_combined_response_maps_index_footer_and_data_pages():
    matches, totals = lab.analyze([response(20, 75)], REGIONS, 80)
    assert matches[0]["overlap_bytes"] == {
        "id.page.0": 10,
        "id.page.1": 20,
        "id.column_index": 10,
        "id.offset_index": 5,
        "footer": 10,
    }
    assert totals["server_written_bytes"] == totals["unique_coverage_bytes"] == 55
    assert totals["data_pages_touched"] == 2
    assert totals["bytes_in_pages_outside_point"] == {"written_bytes": 10, "unique_bytes": 10}


def test_incomplete_response_maps_only_the_written_prefix():
    matches, totals = lab.analyze([response(40, 80, written=15)], REGIONS, 80)
    assert matches[0]["written_span"] == [40, 55]
    assert matches[0]["overlap_bytes"] == {"id.page.1": 10, "id.column_index": 5}
    assert totals["bytes_by_kind"]["offset_index"] == {"written_bytes": 0, "unique_bytes": 0}
    assert totals["incomplete_requests"] == 1
    assert totals["server_written_bytes"] == totals["unique_coverage_bytes"] == 15


def test_repeated_partial_page_read_keeps_written_and_unique_bytes_separate():
    observations = [response(20, 46), response(36, 57, request_id="repeat")]
    _, totals = lab.analyze(observations, REGIONS, 80)
    assert totals["requests"] == 2
    assert totals["server_written_bytes"] == 47
    assert totals["unique_coverage_bytes"] == 37
    assert totals["repeated_coverage_bytes"] == 10
    assert totals["bytes_by_kind"]["data_page"] == {"written_bytes": 40, "unique_bytes": 30}


def test_generated_fixture_contains_expected_row_and_indexes(tmp_path):
    fixture = tmp_path / "fixture.parquet"
    expected = lab.generate_fixture(fixture)
    layout = lab.inspect_fixture(fixture)
    (row,) = pq.read_table(fixture, filters=[("id", "=", lab.POINT_ID)]).to_pylist()
    assert [[row["id"], row["payload"]]] == expected["point"]
    for name, column in layout["columns"].items():
        assert column["data_pages"] > 1
        assert f"{name}.column_index" in layout["regions"]
        assert f"{name}.offset_index" in layout["regions"]
        page = layout["regions"][f"{name}.page.{column['point_page']}"]
        assert page["rows"][0] < lab.POINT_ID < page["rows"][1]
        assert not page["header_statistics_present"]


def test_saved_evidence_rederives_from_server_observations():
    report = lab.verify_evidence(ROOT / "docs/duckdb-page-index-lab")
    assert len(report["runs"]) == 15


def test_failed_rerun_preserves_existing_output(tmp_path, monkeypatch):
    engine = tmp_path / "engine"
    (engine / "venv/bin").mkdir(parents=True)
    (engine / "venv/bin/python").touch()
    (engine / "extensions").mkdir()
    monkeypatch.setattr(lab, "engine_root", lambda _version: engine)

    output = tmp_path / "evidence"
    output.mkdir()
    previous = {
        name: f"Previous {name}".encode()
        for name in ("fixture.parquet", "report.json", "runs.json", "SHA256SUMS.txt")
    }
    for name, content in previous.items():
        (output / name).write_bytes(content)

    def interrupted(*_args):
        raise OSError("Capture interrupted")

    monkeypatch.setattr(lab, "capture", interrupted)
    with pytest.raises(OSError, match="Capture interrupted"):
        lab.run_lab(output)
    assert {path.name: path.read_bytes() for path in output.iterdir()} == previous
