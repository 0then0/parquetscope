"""A miniature prefetch lab: real engines, tiny data, one repetition. Skips unprepared engines."""

import importlib.util
import json
from pathlib import Path

import pytest

from parquetscope.engines import SUPPORTED_ENGINES, read_manifest
from parquetscope.models import load_artifact_file

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("prefetch_lab", ROOT / "scripts/prefetch_lab.py")
lab = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(lab)
EVIDENCE = ROOT / "docs/prefetch-lab"


@pytest.fixture(scope="module")
def report(tmp_path_factory):
    for engine in SUPPORTED_ENGINES:
        if read_manifest(engine) is None:
            pytest.skip(f"Run `parquetscope setup --engine {engine}` first")
    root = tmp_path_factory.mktemp("prefetch-lab")
    return root, lab.run_lab(
        root / "out", root / "data", repetitions=1, old_rows=60000, new_files=2, new_rows=25000
    )


def test_four_configurations_agree_with_the_independent_control(report):
    root, result = report
    assert all(result["checks"].values())
    assert sorted(result["summary"]) == [
        "duckdb-1.5.6-prefetch-off",
        "duckdb-1.5.6-prefetch-on",
        "duckdb-2.0.0-prefetch-off",
        "duckdb-2.0.0-prefetch-on",
    ]
    expected = result["query"]["expected"]
    assert expected["rows"] == lab.LIMIT and expected["top_rows_from_old_file"] > 0
    for entry in result["runs"]:
        run = load_artifact_file(root / "out" / entry["capture"])
        assert run.result_digest == expected["digest"] and run.capture.complete
        assert run.engine.prefetch.status == "verified"
        assert run.settings.disable_parquet_prefetching is (entry["prefetch"] == "off")


def test_fixed_settings_and_interleaving(report):
    _, result = report
    fixed = result["fixed_settings"]
    assert fixed["identical_after_numeric_normalization"] and fixed["external_file_cache"] is False
    assert [lab.rotation(r)[0] for r in (1, 2, 3, 4, 5)] == [
        lab.CONFIGS[0],
        lab.CONFIGS[1],
        lab.CONFIGS[2],
        lab.CONFIGS[3],
        lab.CONFIGS[0],
    ]
    assert {c for r in (1, 2) for c in lab.rotation(r)} == set(lab.CONFIGS)


def test_dataset_has_one_large_old_row_group_and_newer_small_files(report):
    _, result = report
    files = result["dataset"]["files"]
    assert files[0]["path"].endswith("old.parquet") and files[0]["row_groups"] == 1
    assert all(f["row_groups"] > 1 for f in files[1:])
    assert files[0]["rows"] > max(f["rows"] for f in files[1:])
    assert len({f["sha256"] for f in files}) == len(files)


def test_published_evidence_is_internally_consistent():
    published = json.loads((EVIDENCE / "report.json").read_text())
    assert all(published["checks"].values()) and len(published["runs"]) == 12
    for line in (EVIDENCE / "SHA256SUMS.txt").read_text().splitlines():
        digest, name = line.split("  ", 1)
        assert lab.sha256_file(EVIDENCE / name) == digest, name
    for entry in published["runs"]:
        run = load_artifact_file(EVIDENCE / entry["capture"])
        assert run.metrics.get_count == entry["get_count"]
        assert len([s for s in run.server_observations if s.method == "GET"]) == entry["get_count"]
