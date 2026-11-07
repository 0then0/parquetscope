import copy
import json
from pathlib import Path

import pytest

from parquetscope.analysis import derive_set_metrics
from parquetscope.models import (
    MAX_ARTIFACT_BYTES,
    Capture,
    Run,
    load_artifact,
    save_artifact,
    span,
)
from parquetscope.models_v3 import SetRun3
from parquetscope.offline import viewer_html

REAL_V1 = Path(__file__).parents[1] / "docs/real-data/captures/A-1.parquetscope.json"


def dump(run):
    return json.loads(run.model_dump_json())


def load(data):
    return load_artifact(json.dumps(data).encode())


def test_roundtrip_and_no_rows_or_absolute_paths(set_measured, tmp_path):
    run, preview = set_measured
    out = tmp_path / "set.json"
    save_artifact(run, out)
    assert load_artifact(out.read_bytes()) == run
    text = out.read_text()
    assert preview["rows"] and "preview" not in json.loads(text)
    assert str(tmp_path) not in text
    assert all(
        f["logical_path"].startswith("month=") for f in json.loads(text)["file_set"]["files"]
    )


def test_v1_artifacts_keep_opening_unchanged():
    run = load_artifact(REAL_V1.read_bytes())
    assert isinstance(run, Run) and run.schema_version == "1"
    assert load_artifact(run.model_dump_json().encode()) == run
    assert "file_set" not in run.model_dump() and "phase" not in run.model_dump_json()


@pytest.mark.parametrize("version", ["0", "4", "99", 2, None])
def test_unsupported_versions_are_rejected(set_measured, version):
    data = dump(set_measured[0])
    data["schema_version"] = version
    with pytest.raises(ValueError, match="unsupported"):
        load(data)


def test_v1_payload_cannot_pass_as_v2_and_back(set_measured):
    data = json.loads(REAL_V1.read_bytes())
    data["schema_version"] = "2"
    with pytest.raises(ValueError, match="Invalid or unsupported"):
        load(data)
    v2 = dump(set_measured[0])
    v2["schema_version"] = "1"
    with pytest.raises(ValueError, match="Invalid or unsupported"):
        load(v2)


def mutate_observation(data):
    data["observations"][0]["file_id"] = "f099"


def mutate_server_observation(data):
    data["server_observations"][0]["file_id"] = "f099"


def mutate_span(data):
    o = next(o for o in data["observations"] if o["response_span"])
    o["response_span"]["end"] = "99999999999"


def mutate_duplicate_obs(data):
    data["observations"][1]["id"] = data["observations"][0]["id"]


def mutate_swap_file(data):
    # Both sources must attribute a request to the same file.
    o = next(o for o in data["observations"] if o["file_id"] == "f002")
    o["file_id"] = "f001"


def mutate_total(data):
    data["metrics"]["get_count"] += 1


def mutate_file_total(data):
    data["metrics"]["files"][1]["server_written_bytes"] = "1"


def mutate_access(data):
    data["metrics"]["files"][2]["observed_access"] = "get"


def mutate_coverage(data):
    m = next(m for m in data["metrics"]["files"] if m["coverage"])
    m["coverage"][0]["end"] = str(int(m["coverage"][0]["end"]) + 1)


def mutate_ids(data):
    data["file_set"]["files"][1]["id"] = "f007"


def mutate_duplicate_path(data):
    data["file_set"]["files"][1]["logical_path"] = data["file_set"]["files"][0]["logical_path"]


def mutate_traversal(data):
    data["file_set"]["files"][0]["logical_path"] = "../x/data.parquet"


def mutate_partition(data):
    data["file_set"]["files"][0]["partition"][0]["value"] = "99"


def mutate_name(data):
    data["file_set"]["files"][0]["name"] = "other.parquet"


def mutate_structure(data):
    data["file_set"]["files"][0]["structures"][0]["span"]["end"] = "99999999999"


def mutate_phase(data):
    data["observations"][0]["phase"] = "later"


def mutate_extra(data):
    data["file_set"]["files"][0]["absolute_path"] = "/Users/x/y.parquet"


def mutate_hive(data):
    data["file_set"]["read"]["union_by_name"] = True


@pytest.mark.parametrize(
    "mutation",
    [
        mutate_observation,
        mutate_server_observation,
        mutate_span,
        mutate_duplicate_obs,
        mutate_swap_file,
        mutate_total,
        mutate_file_total,
        mutate_access,
        mutate_coverage,
        mutate_ids,
        mutate_duplicate_path,
        mutate_traversal,
        mutate_partition,
        mutate_name,
        mutate_structure,
        mutate_phase,
        mutate_extra,
        mutate_hive,
    ],
)
def test_corrupted_multi_file_artifacts_are_rejected(set_measured, mutation):
    data = copy.deepcopy(dump(set_measured[0]))
    mutation(data)
    with pytest.raises(ValueError, match="Invalid or unsupported"):
        load(data)


def test_complete_capture_cannot_hide_a_missing_server_observation(set_measured):
    data = dump(set_measured[0])
    data["server_observations"] = data["server_observations"][:-1]
    with pytest.raises(ValueError, match="reconciled"):
        load(data)


def test_limits_and_garbage(set_measured):
    with pytest.raises(ValueError, match="32 MiB"):
        load_artifact(b" " * (MAX_ARTIFACT_BYTES + 1))
    with pytest.raises(ValueError, match="Invalid"):
        load_artifact(b'{"schema_version": "2"')
    with pytest.raises(ValueError, match="Invalid"):
        load_artifact(b"[1, 2]")
    data = dump(set_measured[0])
    data["file_set"]["files"] = data["file_set"]["files"] * 100  # beyond 256 files
    with pytest.raises(ValueError, match="at most 256"):
        load(data)


def test_same_offsets_in_different_files_are_never_merged(set_measured):
    run, _ = set_measured
    files = run.file_set.files
    template = next(o for o in run.observations if o.method == "GET" and o.response_span)
    server_template = next(s for s in run.server_observations if s.id == template.id)
    observations = [
        template.model_copy(update={"id": f"a{i}", "file_id": files[i].id}) for i in range(2)
    ] + [template.model_copy(update={"id": "a2", "file_id": files[0].id})]
    servers = [
        server_template.model_copy(update={"id": o.id, "file_id": o.file_id}) for o in observations
    ]
    state = Capture(complete=True, reconciled=True, reason=None)
    metrics = derive_set_metrics(files, observations, servers, state)
    width = int(template.response_span.end) - int(template.response_span.start)
    first, second, third = metrics.files
    assert (first.unique_coverage_bytes, first.repeated_coverage_bytes) == (str(width), str(width))
    assert (second.unique_coverage_bytes, second.repeated_coverage_bytes) == (str(width), "0")
    assert third.observed_access == "none" and third.coverage == []
    assert metrics.unique_coverage_bytes == str(2 * width)  # sum of per-file unions
    assert metrics.repeated_coverage_bytes == str(width)
    assert (metrics.files_with_get, metrics.files_without_observed_access) == (2, 1)


def test_head_only_and_unobserved_files_are_distinct_states(set_measured):
    run, _ = set_measured
    files = run.file_set.files
    head = next(o for o in run.observations if o.method == "HEAD")
    server_head = next(s for s in run.server_observations if s.id == head.id)
    state = Capture(complete=True, reconciled=True, reason=None)
    observations = [head.model_copy(update={"file_id": files[1].id})]
    servers = [server_head.model_copy(update={"file_id": files[1].id})]
    metrics = derive_set_metrics(files, observations, servers, state)
    assert [m.observed_access for m in metrics.files] == ["none", "head_only", "none"]
    assert metrics.files[1].get_count == 0 and metrics.files[1].head_count == 1
    assert metrics.files[1].unique_coverage_bytes == "0"
    assert metrics.files_head_only == 1 and metrics.files_without_observed_access == 2
    empty = derive_set_metrics(files, [], [], state)
    assert {m.observed_access for m in empty.files} == {"none"}
    assert empty.get_count == 0 and empty.server_written_bytes == "0"


def test_partial_trace_and_unknown_values_are_explained(set_measured):
    run, _ = set_measured
    files = run.file_set.files
    get = next(o for o in run.observations if o.method == "GET")
    unknown = get.model_copy(update={"response_span": None, "declared_body_bytes": None})
    state = Capture(complete=False, reconciled=False, reason="worker interrupted")
    metrics = derive_set_metrics(files, [unknown], [], state)
    assert metrics.totals_scope == "observed_subset"
    assert metrics.unique_coverage_bytes is None and metrics.confirmed_range_bytes is None
    assert "worker interrupted" in metrics.unknown_reasons
    assert "Some GET responses have no confirmed file interval" in metrics.unknown_reasons
    owner = next(m for m in metrics.files if m.file_id == get.file_id)
    assert owner.unique_coverage_bytes is None and owner.observed_access == "get"
    assert next(m for m in metrics.files if m.file_id != get.file_id).unique_coverage_bytes == "0"


def test_large_offsets_are_exact_per_file(set_measured):
    run, _ = set_measured
    data = dump(run)
    big = 2**63 + 123
    f = data["file_set"]["files"][0]
    f["size"] = str(big)
    f["structures"].append(
        {
            "id": "huge",
            "kind": "unclassified",
            "label": "Unclassified",
            "span": span(int(f["size"]) - 7, big).model_dump(),
            "row_group": None,
            "column": None,
            "rows": None,
            "dictionary_offset": None,
            "data_offset": None,
            "compression": None,
        }
    )
    reopened = load(data)
    assert reopened.file_set.files[0].structures[-1].span.end == str(big)


def test_offline_html_escapes_paths_and_embeds_without_sources(set_measured):
    run, _ = set_measured
    data = dump(run)
    f = data["file_set"]["files"][0]
    f["logical_path"] = "x=<script>alert(1)<!--/data.parquet"
    f["partition"] = [{"key": "x", "value": "<script>alert(1)<!--"}]
    f["name"] = "data.parquet"
    data["sql"] = "SELECT '</script><script>alert(2)</script>'"
    hostile = SetRun3.model_validate(data)
    html = viewer_html(hostile)
    assert "</script><script>alert" not in html and "<script>alert(1)" not in html
    assert "x=\\u003cscript\\u003ealert(1)\\u003c!--" in html
    assert 'type="application/json">null</script>' not in html
    assert str(Path.home()) not in html.split('id="parquetscope-artifact"', 1)[1][:200000]
