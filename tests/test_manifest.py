import json
import os
import threading

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from conftest import write_manifest, write_table

from parquetscope.manifest import MAX_MANIFEST_BYTES, load_manifest, resolve_set
from parquetscope.models_v2 import partition_pairs, split_logical_path


def test_same_basename_in_different_directories_get_distinct_stable_ids(hive_set):
    resolved = resolve_set(load_manifest(hive_set))
    files = [f.file for f in resolved.files]
    assert [f.name for f in files] == ["data.parquet"] * 3
    assert [f.id for f in files] == ["f001", "f002", "f003"]
    assert [f.logical_path for f in files] == [f"month={m}/data.parquet" for m in (1, 2, 3)]
    assert [(p.key, p.value) for p in files[1].partition] == [("month", "2")]
    assert len({f.sha256 for f in files}) == 3  # distinct bytes: distinct month values
    assert resolved.read.hive_partitioning is True
    assert str(hive_set.parent) not in resolved.file_set().model_dump_json()


def test_unicode_and_nested_partitions_are_kept_as_written(tmp_path):
    names = [
        "région=Zürich/year=2026/données 1.parquet",
        "région=a b&c/year=2026/données 1.parquet",
    ]
    for name in names:
        write_table(tmp_path / name)
    spec = load_manifest(write_manifest(tmp_path, names, hive=False))
    files = [f.file for f in resolve_set(spec).files]
    assert [(p.key, p.value) for p in files[0].partition] == [
        ("région", "Zürich"),
        ("year", "2026"),
    ]
    assert files[1].partition[0].value == "a b&c"


@pytest.mark.parametrize(
    "path",
    [
        "/etc/passwd",
        "../outside.parquet",
        "a/../b.parquet",
        "a//b.parquet",
        "./a.parquet",
        "a\\b.parquet",
        "a/‮b.parquet",
        "a/b\x00.parquet",
        "",
        "dir/",
    ],
)
def test_unsafe_logical_paths_are_rejected(tmp_path, path):
    write_table(tmp_path / "a.parquet")
    with pytest.raises(ValueError):
        load_manifest(write_manifest(tmp_path, [path]))


def test_path_rules_are_a_pure_function():
    assert split_logical_path("month=1/x.parquet") == ["month=1", "x.parquet"]
    assert partition_pairs("a=1/plain/b=/x.parquet") == [("a", "1"), ("b", "")]
    assert partition_pairs("x.parquet") == []
    with pytest.raises(ValueError, match="NFC"):
        split_logical_path("é.parquet")  # decomposed é
    with pytest.raises(ValueError, match="segments"):
        split_logical_path("/".join(["d"] * 33))


def test_nfc_decomposed_names_are_normalized_for_the_logical_path(tmp_path):
    write_table(tmp_path / "é.parquet")
    spec = load_manifest(write_manifest(tmp_path, ["é.parquet"]))
    assert spec.entries[0].logical_path == "é.parquet"


def test_duplicates_and_aliases_are_rejected(tmp_path):
    write_table(tmp_path / "a.parquet")
    with pytest.raises(ValueError, match="duplicate path"):
        load_manifest(write_manifest(tmp_path, ["a.parquet", "a.parquet"]))
    os.link(tmp_path / "a.parquet", tmp_path / "hard.parquet")
    with pytest.raises(ValueError, match="same file"):
        load_manifest(write_manifest(tmp_path, ["a.parquet", "hard.parquet"]))
    (tmp_path / "link.parquet").symlink_to(tmp_path / "a.parquet")
    with pytest.raises(ValueError, match="same file"):
        load_manifest(write_manifest(tmp_path, ["a.parquet", "link.parquet"]))


def test_symlink_leaving_the_manifest_directory_is_rejected(tmp_path):
    outside = tmp_path / "outside"
    inside = tmp_path / "inside"
    write_table(outside / "secret.parquet")
    inside.mkdir()
    (inside / "escape.parquet").symlink_to(outside / "secret.parquet")
    (inside / "dir").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="outside the manifest directory"):
        load_manifest(write_manifest(inside, ["escape.parquet"]))
    with pytest.raises(ValueError, match="outside the manifest directory"):
        load_manifest(write_manifest(inside, ["dir/secret.parquet"]))


def test_missing_and_non_regular_entries_are_rejected(tmp_path):
    (tmp_path / "dir.parquet").mkdir()
    with pytest.raises(ValueError, match="cannot be read"):
        load_manifest(write_manifest(tmp_path, ["missing.parquet"]))
    with pytest.raises(ValueError, match="not a regular file"):
        load_manifest(write_manifest(tmp_path, ["dir.parquet"]))


@pytest.mark.parametrize(
    "text, message",
    [
        ("{bad", "not valid JSON"),
        ('{"manifest_version": "1", "files": [{"path": "a"}], "files": []}', "duplicate JSON key"),
        ('{"manifest_version": "1", "files": [{"path": "a", "size": NaN}]}', "non-standard"),
        ('{"manifest_version": "2", "files": [{"path": "a"}]}', "manifest_version"),
        ('{"manifest_version": "1", "files": []}', "files"),
        ('{"manifest_version": "1", "files": [{"path": "a", "glob": "*"}]}', "glob"),
        ('{"manifest_version": "1", "files": [{"path": "a"}], "recursive": true}', "recursive"),
        ('{"manifest_version": "1", "files": [{"path": "a", "sha256": "xyz"}]}', "sha256"),
        (
            '{"manifest_version": "1", "read": {"union_by_name": true}, "files": [{"path": "a"}]}',
            "union",
        ),
        ("[]", "manifest"),
    ],
)
def test_malformed_manifest_is_rejected(tmp_path, text, message):
    path = tmp_path / "m.json"
    path.write_text(text)
    with pytest.raises(ValueError, match=message):
        load_manifest(path)


def test_manifest_size_and_file_count_are_bounded(tmp_path):
    path = tmp_path / "m.json"
    with path.open("wb") as stream:
        stream.truncate(MAX_MANIFEST_BYTES + 1)
    with pytest.raises(ValueError, match="1 MiB"):
        load_manifest(path)
    too_many = {"manifest_version": "1", "files": [{"path": f"f{i}"} for i in range(257)]}
    path.write_text(json.dumps(too_many))
    with pytest.raises(ValueError, match="files"):
        load_manifest(path)
    assert not (tmp_path / "f0").exists()  # nothing was discovered or created


def test_directories_are_never_expanded(tmp_path):
    write_table(tmp_path / "data" / "a.parquet")
    with pytest.raises(ValueError, match="not a regular file"):
        load_manifest(write_manifest(tmp_path, ["data"]))


def test_pins_detect_a_file_changed_after_the_manifest(hive_set, tmp_path):
    resolved = resolve_set(load_manifest(hive_set))
    first = resolved.files[0].file
    pins = {"size": first.size, "sha256": first.sha256}
    manifest = json.loads(hive_set.read_text())
    manifest["files"][0].update(pins)
    hive_set.write_text(json.dumps(manifest))
    assert resolve_set(load_manifest(hive_set)).files[0].file.sha256 == first.sha256
    write_table(tmp_path / "month=1" / "data.parquet", month=1, rows=3001)
    with pytest.raises(ValueError, match="month=1/data.parquet: size"):
        resolve_set(load_manifest(hive_set))
    manifest["files"][0]["size"] = (tmp_path / "month=1" / "data.parquet").stat().st_size.__str__()
    hive_set.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="SHA-256 differs"):
        resolve_set(load_manifest(hive_set))


def test_incompatible_schemas_are_rejected_not_merged(tmp_path):
    write_table(tmp_path / "a.parquet")
    pq.write_table(pa.table({"id": pa.array([1], pa.int64())}), tmp_path / "b.parquet")
    with pytest.raises(ValueError, match="Schema evolution"):
        resolve_set(load_manifest(write_manifest(tmp_path, ["a.parquet", "b.parquet"])))
    pq.write_table(
        pa.table(
            {
                "id": pa.array([1], pa.int32()),
                "month": pa.array([1], pa.int64()),
                "v": pa.array([1], pa.int64()),
            }
        ),
        tmp_path / "c.parquet",
    )
    with pytest.raises(ValueError, match="schema differs"):
        resolve_set(load_manifest(write_manifest(tmp_path, ["a.parquet", "c.parquet"])))


def test_non_parquet_file_and_cancellation(tmp_path):
    (tmp_path / "text.parquet").write_text("not parquet")
    with pytest.raises(ValueError, match="text.parquet"):
        resolve_set(load_manifest(write_manifest(tmp_path, ["text.parquet"])))
    write_table(tmp_path / "a.parquet")
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(ValueError, match="cancelled"):
        resolve_set(load_manifest(write_manifest(tmp_path, ["a.parquet"])), cancel)


def test_hive_keys_must_survive_url_encoding(tmp_path):
    write_table(tmp_path / "région=1" / "a.parquet")
    with pytest.raises(ValueError, match="partition key 'région'"):
        load_manifest(write_manifest(tmp_path, ["région=1/a.parquet"], hive=True))
    assert load_manifest(write_manifest(tmp_path, ["région=1/a.parquet"], hive=False))
