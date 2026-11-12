"""Small reproducibility check plus immutable evidence validation; no downloads or setup."""

import importlib.util
import json
from pathlib import Path

import pytest

from parquetscope.engines import read_manifest
from parquetscope.models import load_artifact_file

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "http_profile_lab", ROOT / "scripts/http_profile_lab.py"
)
lab = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(lab)
EVIDENCE = ROOT / "docs/http-profile-lab"


@pytest.fixture(scope="module")
def report(tmp_path_factory):
    if read_manifest(lab.ENGINE) is None:
        pytest.skip(f"Run `parquetscope setup --engine {lab.ENGINE}` first")
    root = tmp_path_factory.mktemp("http-profile-lab")
    result = lab.run_lab(root / "out", root / "data", repetitions=1, rows_per_file=5000)
    return root / "out", result


def test_each_profile_and_prefetch_mode_matches_independent_result(report):
    output, result = report
    assert all(result["checks"].values())
    assert len(result["runs"]) == 6
    assert {run["config"] for run in result["runs"]} == {
        f"{profile}-prefetch-{mode}" for profile in lab.PROFILES for mode in ("on", "off")
    }
    expected = result["query"]["independent_pyarrow_control"]
    for entry in result["runs"]:
        run = load_artifact_file(output / entry["capture"])
        assert run.schema_version == "4"
        assert run.result_digest == expected["digest"]
        assert run.result_rows == expected["result_rows"]
        assert run.sql_status == "succeeded" and run.capture.complete and run.capture.reconciled
        assert run.engine.prefetch.status == "verified"
        assert (
            run.http_profile.requested.model_dump()
            == lab.PROFILES[entry["profile_name"]].model_dump()
        )
        assert run.http_profile.effective == run.http_profile.requested
        assert entry["files_with_get"] == 2 and entry["server_get_count"] > 0
        assert entry["server_written_bytes"] == run.metrics.server_written_bytes
        assert entry["http_profile"] == run.http_profile.model_dump(mode="json")
        assert sum(int(r.written_body_bytes) for r in run.server_observations) == int(
            run.metrics.server_written_bytes
        )
        viewer = output / "viewers" / f"{entry['config']}.viewer.html"
        assert run.result_digest in viewer.read_text()
        assert '"schema_version":"4"' in viewer.read_text()


def test_independent_parallel_http_control_measures_server_model(report):
    _, result = report
    measured = result["independent_http_control"]
    assert all(measured["checks"].values())
    assert measured["delay_clients"][0]["method"] == "HEAD"
    assert measured["delay_clients"][0]["bytes"] == 0
    assert len(measured["parallel_server"]) == 2
    assert measured["body_write_span_ms"] >= (
        measured["model_minimum_ms"] - measured["timing_allowance_ms"]
    )
    assert sum(r["body_budget_wait_ms"] for r in measured["parallel_server"]) > 0
    assert all(
        r["profile_delay_ms"] >= measured["delay_ms"] * 0.9 for r in measured["delay_server"]
    )


def test_rotation_and_lab_bounds():
    for repetition in (1, 2, 3):
        order = lab.rotation(repetition)
        assert len(order) == 6 and len(set(order)) == 6
        assert all(order[i][0] == order[i + 1][0] for i in (0, 2, 4))
    assert lab.rotation(1)[0] == ("unrestricted", "on")
    assert lab.rotation(2)[0] == ("delay", "off")
    assert lab.rotation(3)[0] == ("delay-rate", "on")
    with pytest.raises(ValueError, match="whole-lab deadline"):
        lab.run_lab(Path("unused"), Path("unused"), deadline_seconds=181)
    with pytest.raises(ValueError, match="rows_per_file"):
        lab.generate(Path("unused"), rows_per_file=20001)


def test_published_http_profile_evidence_is_consistent():
    published = json.loads((EVIDENCE / "report.json").read_text())
    assert len(published["runs"]) == 18 and published["repetitions"] == 3
    assert all(published["checks"].values())
    assert published["lab_elapsed_seconds"] < published["deadline_seconds"] <= 180
    assert published["versions"]["duckdb"] == lab.ENGINE
    assert len(published["dataset"]["files"]) == 2
    assert all(file["row_groups"] > 1 for file in published["dataset"]["files"])
    assert len(published["dataset"]["columns"]) == 8
    expected = published["query"]["independent_pyarrow_control"]["digest"]
    for line in (EVIDENCE / "SHA256SUMS.txt").read_text().splitlines():
        digest, name = line.split("  ", 1)
        assert lab.sha256_file(EVIDENCE / name) == digest, name
    for entry in published["runs"]:
        run = load_artifact_file(EVIDENCE / entry["capture"])
        assert run.result_digest == expected and run.capture.complete
        assert entry["get_count"] == run.metrics.get_count
        assert entry["http_profile"] == run.http_profile.model_dump(mode="json")
        assert entry["effective_engine_settings"] == run.engine.effective_settings
