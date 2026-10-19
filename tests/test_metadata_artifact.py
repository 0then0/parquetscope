import json

import pyarrow.parquet as pq
import pytest

from parquetscope.analysis import derive_metrics
from parquetscope.metadata import read_metadata
from parquetscope.models import MAX_ARTIFACT_BYTES, Capture, Span, load_artifact, save_artifact


def test_dictionary_start_and_unknown_regions(parquet):
    data, _ = read_metadata(parquet)
    source = pq.ParquetFile(parquet).metadata
    chunks = [s for s in data.structures if s.kind == "column_chunk"]
    for chunk in chunks:
        col = source.row_group(chunk.row_group).column(int(chunk.id.split("-c")[1]))
        assert int(chunk.span.start) == min(col.dictionary_page_offset, col.data_page_offset)
        assert int(chunk.span.end) - int(chunk.span.start) == col.total_compressed_size
        assert col.file_offset == 0  # Evidence that this field is not a chunk start.
    assert sum(int(s.span.end) - int(s.span.start) for s in data.structures) == int(data.size)
    assert data.structures[1].span.end == data.size
    assert "statistics" not in data.model_dump_json()


def test_unclassified_preserves_gaps():
    from parquetscope.intervals import complement

    assert complement([(0, 4), (10, 15), (21, 30)], 30) == [(4, 10), (15, 21)]


def test_artifact_roundtrip_and_no_rows(measured, tmp_path):
    run, preview = measured
    out = tmp_path / "run.json"
    save_artifact(run, out)
    assert load_artifact(out.read_bytes()) == run
    data = json.loads(out.read_text())
    assert "preview" not in data and "result" not in data
    assert preview["rows"]
    data["schema_version"] = "2"
    with pytest.raises(ValueError, match="unsupported"):
        load_artifact(json.dumps(data).encode())
    with pytest.raises(ValueError, match="32 MiB"):
        load_artifact(b" " * (MAX_ARTIFACT_BYTES + 1))
    with pytest.raises(ValueError, match="Invalid"):
        load_artifact(b"{bad json")


def test_artifact_rejects_metric_tampering(measured):
    run, _ = measured
    data = run.model_dump()
    data["metrics"]["unique_coverage_bytes"] = "1"
    with pytest.raises(ValueError, match="disagree"):
        load_artifact(json.dumps(data).encode())


def test_large_offsets_are_exact():
    start = 2**63 + 123
    s = Span(start=str(start), end=str(start + 17))
    assert Span.model_validate_json(s.model_dump_json()).pair() == (start, start + 17)
    assert json.loads(s.model_dump_json())["start"] == str(start)


def test_partial_trace_uses_observed_subset(measured):
    run, _ = measured
    state = Capture(complete=False, reconciled=False, reason="worker interrupted")
    metrics = derive_metrics(run.observations[:1], run.server_observations, state)
    assert metrics.totals_scope == "observed_subset"
    assert "worker interrupted" in metrics.unknown_reasons
    get = next(o for o in run.observations if o.method == "GET")
    unknown = get.model_copy(update={"response_span": None, "declared_body_bytes": None})
    metrics = derive_metrics([unknown], [], state)
    assert metrics.confirmed_range_bytes is None
    assert metrics.unique_coverage_bytes is None
    assert metrics.declared_body_bytes is None


def test_full_artifact_large_size_roundtrip(measured):
    from parquetscope.models import Input, Run, Structure, span

    run, _ = measured
    data = run.input.model_dump()
    old_size = int(data["size"])
    data["size"] = str(2**63 + 123)
    data["structures"].append(
        Structure(
            id="large-unknown",
            kind="unclassified",
            label="Unclassified",
            span=span(old_size, int(data["size"])),
        ).model_dump()
    )
    modified = Run.model_validate({**run.model_dump(), "input": Input.model_validate(data)})
    reopened = load_artifact(modified.model_dump_json().encode())
    assert reopened.input.size == str(2**63 + 123)
    assert reopened.input.structures[-1].span.end == str(2**63 + 123)


def test_complete_capture_cannot_hide_unreconciled_observations(measured):
    run, _ = measured
    data = run.model_dump()
    data["server_observations"] = data["server_observations"][:-1]
    with pytest.raises(ValueError, match="reconciled"):
        load_artifact(json.dumps(data).encode())


def test_offline_template_escapes_script_termination(measured):
    from parquetscope.offline import viewer_html

    run, _ = measured
    changed = run.model_copy(update={"sql": "SELECT '</script><script>alert(1)</script>'"})
    html = viewer_html(changed)
    assert "</script><script>alert(1)" not in html
    assert "\\u003c/script\\u003e" in html
    assert 'type="application/json">null</script>' not in html


def test_oversized_artifact_file_has_bounded_read(tmp_path):
    from parquetscope.models import load_artifact_file

    path = tmp_path / "oversized.json"
    with path.open("wb") as stream:
        stream.truncate(10**10)  # Sparse file; do not allocate or read ten gigabytes.
    with pytest.raises(ValueError, match="32 MiB"):
        load_artifact_file(path)
