"""Footer extraction and range-to-index matching of the DataFusion page-index lab.

The Rust harness is not built here; the recorded evidence is re-derived from its raw
server observations instead.
"""

import importlib.util
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from parquetscope.models import span
from parquetscope.models_v4 import ServerObservation4

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "datafusion_page_index_lab", ROOT / "scripts/datafusion_page_index_lab.py"
)
lab = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(lab)
EVIDENCE = ROOT / "docs/datafusion-page-index-lab"

CI_A = lab.Region("column_index", 0, "id", 1000, 1100)
CI_B = lab.Region("column_index", 1, "id", 1100, 1200)
OI_A = lab.Region("offset_index", 0, "id", 1200, 1260)
REGIONS = [CI_A, CI_B, OI_A]


def observation(start, end, written=None, method="GET", ident="r"):
    declared = 0 if method == "HEAD" else end - start
    written = declared if written is None else written
    complete = written == declared
    return ServerObservation4(
        id=ident,
        method=method,
        requested_range=f"bytes={start}-{end - 1}",
        response_span=None if method == "HEAD" else span(start, end),
        status=206 if method == "GET" else 200,
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
        body_write_started_ms=1.5 if written else None,
        body_write_finished_ms=2.5 if written else None,
        body_write_ms=1.0 if written else 0.0,
        reserved_body_bytes=str(written),
        termination="completed" if complete else "disconnect",
    )


@pytest.fixture(scope="module")
def small_fixture(tmp_path_factory):
    path = tmp_path_factory.mktemp("dfpi") / "fixture.parquet"
    expected = lab.generate_fixture(path, rows=6000, row_group_size=2000)
    return path, expected, lab.read_footer(path)


def test_footer_regions_decode_exactly_and_agree_with_pyarrow(small_fixture):
    path, _, footer = small_fixture
    meta = pq.ParquetFile(path).metadata
    assert footer["row_groups"] == meta.num_row_groups == 3
    assert footer["footer"][1] == path.stat().st_size
    kinds = {(r.row_group, r.column, r.kind) for r in footer["regions"]}
    for rg in range(meta.num_row_groups):
        for c in range(meta.num_columns):
            col = meta.row_group(rg).column(c)
            name = col.path_in_schema
            assert ((rg, name, "column_index") in kinds) == col.has_column_index
            assert ((rg, name, "offset_index") in kinds) == col.has_offset_index
    data = path.read_bytes()
    for region in footer["regions"]:
        assert region.end <= footer["footer"][0]
        lab.decode_exact(data[region.start : region.end])
        with pytest.raises(lab.ThriftError):
            lab.decode_exact(data[region.start : region.end - 1])


def test_fixture_metadata_supports_the_scenarios(small_fixture):
    path, expected, footer = small_fixture
    checks = lab.check_fixture(path, footer)
    for key in (
        "multiple_row_groups",
        "every_chunk_has_offset_index",
        "statistics_columns_have_column_index",
        "fk_has_no_column_index",
        "fk_null_count_absent",
        "id_null_count_zero",
        "fk_contains_nulls",
        "pyarrow_agrees",
    ):
        assert checks[key], key
    table = pq.read_table(path)
    fk = table.column("fk").to_pylist()
    assert expected["main"]["rows"] == sum(v is not None and v >= 0 for v in fk)
    assert expected["control"]["rows"] == expected["is_not_null"]["rows"] == table.num_rows


def test_compact_reader_rejects_trailing_bytes():
    # struct {1: i32 = -1}, STOP; a trailing byte must not be accepted as part of it.
    data = bytes([0x15, 0x01, 0x00])
    assert lab.decode_exact(data) == {1: -1}
    with pytest.raises(lab.ThriftError):
        lab.decode_exact(data + b"\x00")


def test_coalesced_request_over_both_index_kinds_is_index_only():
    match = lab.match_observation(observation(1000, 1260), REGIONS)
    assert match["index_role"] == "index_only"
    assert match["delivered_index_bytes"] == 260
    assert all(hit["fully_delivered"] for hit in match["regions"])
    assert {hit["kind"] for hit in match["regions"]} == {"column_index", "offset_index"}


def test_request_that_also_reads_footer_is_incidental():
    match = lab.match_observation(observation(900, 1500), REGIONS)
    assert match["index_role"] == "incidental"
    assert match["delivered_index_bytes"] == 260


def test_partial_region_read_counts_only_overlap():
    match = lab.match_observation(observation(1050, 1150), REGIONS)
    hits = {hit["region"]: hit for hit in match["regions"]}
    assert hits[CI_A.id]["delivered_bytes"] == 50
    assert hits[CI_B.id]["delivered_bytes"] == 50
    assert not any(hit["fully_delivered"] for hit in hits.values())
    summary = lab.summarize_open([match], REGIONS)
    assert summary["regions_partially_covered"]["column_index"] == 2
    assert summary["regions_fully_covered"]["column_index"] == 0


def test_incomplete_response_counts_written_bytes_only():
    # Declared 900..1300, but only 150 body bytes were written: nothing reached the index.
    match = lab.match_observation(observation(900, 1300, written=150), REGIONS)
    assert match["delivered_span"] == [900, 1050]
    assert match["declared_index_bytes"] == 260
    assert match["delivered_index_bytes"] == 50
    assert match["index_role"] == "incidental"
    none = lab.match_observation(observation(900, 1300, written=50), REGIONS)
    assert none["delivered_index_bytes"] == 0 and none["index_role"] == "none"


def test_head_and_disjoint_requests_do_not_touch_index():
    assert lab.match_observation(observation(0, 0, method="HEAD"), REGIONS)["regions"] == []
    assert lab.match_observation(observation(0, 1000), REGIONS)["index_role"] == "none"
    assert lab.match_observation(observation(1260, 2000), REGIONS)["index_role"] == "none"


def test_coverage_merges_requests_without_double_counting():
    first = lab.match_observation(observation(1000, 1080, ident="a"), REGIONS)
    second = lab.match_observation(observation(1040, 1100, ident="b"), REGIONS)
    summary = lab.summarize_open([first, second], REGIONS)
    assert summary["regions_fully_covered"]["column_index"] == 1
    assert summary["index_bytes_covered"] == 100
    assert summary["delivered_index_bytes"] == 140  # transferred twice in part
    assert summary["index_only_requests"] == 2


@pytest.mark.skipif(not (EVIDENCE / "report.json").exists(), reason="no recorded evidence")
def test_recorded_evidence_rederives_from_raw_observations():
    verdict = lab.verify_evidence(EVIDENCE)
    report = lab.json.loads((EVIDENCE / "report.json").read_text())
    assert verdict == report["observations_vs_hypothesis"]
    assert all(report["fixture"]["checks"].values())
