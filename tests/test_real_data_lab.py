"""Offline checks only: never download the full TLC dataset in a normal test run."""

import hashlib
import importlib.util
import io
import json
import shutil
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("real_data_lab", ROOT / "scripts/real_data_lab.py")
lab = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(lab)
EVIDENCE = ROOT / "docs/real-data"


class Response(io.BytesIO):
    def __init__(self, data, declared=None):
        super().__init__(data)
        self.headers = {} if declared is None else {"Content-Length": str(declared)}


def test_streamed_download_checks_hash_before_publish(monkeypatch, tmp_path):
    data = b"bounded test input"
    calls = []

    def open_url(url, timeout):
        calls.append((url, timeout))
        return Response(data, len(data))

    monkeypatch.setattr(lab.urllib.request, "urlopen", open_url)
    target = tmp_path / "source.parquet"
    lab.download_source(
        target,
        url="https://example.test/input",
        size=len(data),
        digest=hashlib.sha256(data).hexdigest(),
    )
    assert target.read_bytes() == data
    assert calls == [("https://example.test/input", 30)]
    assert list(tmp_path.iterdir()) == [target]


@pytest.mark.parametrize(
    "data,declared,size,digest",
    [
        (b"wrong hash", None, 10, "0" * 64),
        (b"too long", None, 3, "0" * 64),
        (b"short", 99, 10, "0" * 64),
        (b"short", None, 10, "0" * 64),
    ],
)
def test_reject_download_without_replacing_existing_file(
    monkeypatch, tmp_path, data, declared, size, digest
):
    monkeypatch.setattr(lab.urllib.request, "urlopen", lambda *a, **kw: Response(data, declared))
    target = tmp_path / "source.parquet"
    target.write_bytes(b"previous input")
    with pytest.raises(ValueError, match="size|SHA-256"):
        lab.download_source(target, size=size, digest=digest)
    assert target.read_bytes() == b"previous input"
    assert list(tmp_path.iterdir()) == [target]


def test_cached_source_mismatch_is_rejected(tmp_path):
    target = tmp_path / "source.parquet"
    target.write_bytes(b"not January 2025")
    with pytest.raises(ValueError, match="mismatch"):
        lab.checked_source(target)


def test_full_multiset_comparison_catches_duplicates_and_values(tmp_path):
    a, b, wrong = (tmp_path / name for name in ("a.parquet", "b.parquet", "wrong.parquet"))
    pq.write_table(pa.table({"key": [1, 1, 2, None], "value": ["a", "a", "b", None]}), a)
    pq.write_table(pa.table({"key": [None, 2, 1, 1], "value": [None, "b", "a", "a"]}), b)
    # Same count and the same set of rows, but a different duplicate multiset.
    pq.write_table(pa.table({"key": [None, 2, 2, 1], "value": [None, "b", "b", "a"]}), wrong)
    with duckdb.connect() as con:
        assert lab.multiset_difference(con, a, b)["difference_rows"] == [0, 0]
        with pytest.raises(ValueError, match="multiset equality failed"):
            lab.multiset_difference(con, a, wrong)
    with pytest.raises(ValueError, match="row order"):
        lab.same_order(a, b)
    assert lab.same_order(a, a)


def test_exact_decimal_representation_never_rounds_a_wrong_value():
    assert lab.exact_result({"rows": [["142826", '"8038349.75"']]}) == lab.EXPECTED
    assert lab.exact_result({"rows": lab.EXPECTED}) == lab.EXPECTED
    with pytest.raises(ValueError, match="Exact DECIMAL"):
        lab.exact_result({"rows": [["142826", "8038349.75001"]]})


def test_public_report_matches_nine_native_captures_and_source_snapshot():
    report = lab.verify_report(EVIDENCE)
    assert len(report["attempts"]) == 9
    assert all(a["status"] == "succeeded" for a in report["attempts"])
    assert report["wheel_acceptance"]["installed_non_editable"]
    assert report["wheel_acceptance"]["installed_files_verified_against_wheel"]
    assert report["wheel_acceptance"]["cwd_outside_checkout"]
    assert lab.sha256(EVIDENCE / report["runner_source"]) == report["runner_sha256"]
    assert report["equality"]["A_B"]["difference_rows"] == [0, 0]
    assert report["equality"]["A_C"]["difference_rows"] == [0, 0]
    for previous in report["previous_attempts"]:
        assert (EVIDENCE / previous["report"]).is_file()
        assert json.loads((EVIDENCE / previous["report"]).read_text())["status"] == "failed"


@pytest.mark.parametrize("mutation", ["metric", "missing_repeat", "result", "incomplete", "sql"])
def test_report_tampering_or_missing_repetitions_are_rejected(tmp_path, mutation):
    # Copies only small public evidence; no source Parquet is needed.
    shutil.copytree(EVIDENCE, tmp_path / "evidence")
    target = tmp_path / "evidence"
    report_path = target / "report.json"
    report = json.loads(report_path.read_text())
    if mutation == "metric":
        report["layouts"][0]["repetitions"][0]["metrics"]["get_count"] += 1
    elif mutation == "missing_repeat":
        report["layouts"][1]["repetitions"].pop()
    elif mutation == "result":
        report["layouts"][0]["repetitions"][0]["runtime_result"] = [["1", "8038349.75"]]
    elif mutation == "sql":
        report["sql"] = "SELECT 1 AS wrong_workload"
    else:
        report["status"] = "failed"
    report_path.write_text(json.dumps(report))
    with pytest.raises(ValueError):
        lab.verify_report(target)


def test_failed_preparation_is_recorded_and_existing_output_is_preserved(monkeypatch, tmp_path):
    output = tmp_path / "public"
    monkeypatch.setattr(lab, "wheel_provenance", lambda _: {"cwd_outside_checkout": True})

    def bad_source(_):
        raise ValueError("NYC TLC input size/SHA-256 mismatch")

    monkeypatch.setattr(lab, "download_source", bad_source)
    with pytest.raises(ValueError, match="mismatch"):
        lab.run_experiment(tmp_path / "work", output, tmp_path / "wheel.whl")
    report = json.loads((output / "report.json").read_text())
    assert report["status"] == "failed"
    assert report["failure"]["stage"] == "download"
    with pytest.raises(ValueError, match="Output must be empty"):
        lab.run_experiment(tmp_path / "work", output, tmp_path / "wheel.whl")
    assert json.loads((output / "report.json").read_text()) == report


def test_manifest_hashes_and_no_private_paths_or_result_rows():
    manifest = json.loads((EVIDENCE / "manifest.json").read_text())
    for name, info in manifest["files"].items():
        path = EVIDENCE / name
        assert path.stat().st_size == info["bytes"]
        assert lab.sha256(path) == info["sha256"]
    for path in EVIDENCE.rglob("*.parquetscope.json"):
        data = path.read_text()
        assert "/Users/" not in data
        assert "/home/" not in data
        assert "preview" not in json.loads(data)
        assert "AWS_SECRET_ACCESS_KEY" not in data


def test_new_experiment_readme_uses_its_own_measurements_and_manifests(tmp_path):
    report = json.loads((EVIDENCE / "report.json").read_text())
    report["layouts"][0]["repetitions"][0]["metrics"]["server_written_bytes"] = "1234567"
    lab.write_case_study(tmp_path, report)
    text = (tmp_path / "README.md").read_text()
    assert "1234567 server-written body bytes" in text
    assert "--work-dir /tmp/tlc-work" in text
    (tmp_path / "provenance").mkdir()
    (tmp_path / "provenance/runner.py").write_bytes(
        (ROOT / "scripts/real_data_lab.py").read_bytes()
    )
    lab.public_manifest(tmp_path)
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert manifest["files"]["README.md"]["sha256"] == lab.sha256(tmp_path / "README.md")
