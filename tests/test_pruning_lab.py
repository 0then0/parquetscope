"""A miniature lab run: real DuckDB, tiny data, one repetition."""

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("pruning_lab", ROOT / "scripts/pruning_lab.py")
lab = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(lab)
EVIDENCE = ROOT / "docs/pruning-lab"


@pytest.fixture(scope="module")
def report(tmp_path_factory, httpfs):
    output = tmp_path_factory.mktemp("pruning-lab")
    return output, lab.run_lab(
        output, repetitions=1, months=4, files_per_month=2, rows_per_file=1500
    )


def test_layouts_have_identical_bytes_and_data(report):
    output, result = report
    assert all(v is True for k, v in result["checks"].items() if k != "rows")
    for entry in result["dataset"]["files"]:
        flat = (output / "flat" / entry["flat_path"]).read_bytes()
        assert flat == (output / "hive" / entry["hive_path"]).read_bytes()


def test_selective_and_control_queries_measure_different_behaviour(report):
    _, result = report
    s = result["summary"]
    assert (
        s["hive/selective"]["files_with_get"]["median"]
        < s["hive/control"]["files_with_get"]["median"]
    )
    assert s["hive/control"]["files_with_get"]["median"] == 8
    assert s["hive/selective"]["files_without_observed_access"]["median"] >= 5
    assert s["flat/selective"]["files_without_observed_access"]["median"] == 0
    for layout, query in (("flat", "selective"), ("hive", "control")):
        assert s[f"{layout}/{query}"]["server_written_bytes"]["median"] > 0


def test_results_match_independent_exact_integer_expectation(report):
    _, result = report
    n = 2 * 1500
    assert result["queries"]["selective"]["expected"][0][0] == str(n)
    expected_control = result["queries"]["control"]["expected"][0]
    assert expected_control[0] == str(4 * n)
    assert all(isinstance(int(v), int) for v in expected_control)


def test_evidence_files_are_consistent(report):
    output, result = report
    sums = (output / "SHA256SUMS.txt").read_text().splitlines()
    assert any("viewers/hive-selective.viewer.html" in line for line in sums)
    assert len(list((output / "captures").glob("*.json"))) == 4
    manifest = json.loads((output / "hive" / "manifest.json").read_text())
    assert manifest["read"] == {"hive_partitioning": True}
    assert all("sha256" in f and "size" in f for f in manifest["files"])


def test_published_evidence_is_internally_consistent():
    published = json.loads((EVIDENCE / "report.json").read_text())
    assert all(v is True for k, v in published["checks"].items() if k != "rows")
    for line in (EVIDENCE / "SHA256SUMS.txt").read_text().splitlines():
        digest, name = line.split("  ", 1)
        assert lab.sha256_file(EVIDENCE / name) == digest, name
