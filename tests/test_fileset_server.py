import http.client
import socket
import time
from urllib.parse import quote

import pytest
from conftest import write_table

import parquetscope.range_server as range_server
from parquetscope.metadata import identity
from parquetscope.range_server import FileSetServer, ServedFile, encode_route


def served(root, names, token="tok"):
    files = []
    for index, name in enumerate(names):
        path = write_table(root / name, month=index + 1)
        files.append(
            ServedFile(f"f{index + 1:03d}", path, identity(path.stat()), encode_route(token, name))
        )
    return files


def get(server, target, method="GET", header=None):
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=3)
    conn.request(method, target, headers={"Range": header} if header else {})
    res = conn.getresponse()
    data = res.read()
    conn.close()
    return res.status, data


def test_same_basename_and_same_offsets_are_served_and_attributed_per_file(tmp_path):
    files = served(tmp_path, ["month=1/data.parquet", "month=2/data.parquet"])
    with FileSetServer(files) as server:
        a = get(server, files[0].route, header="bytes=0-7")
        b = get(server, files[1].route, header="bytes=0-7")
        assert a[0] == b[0] == 206 and a[1] == b[1] == b"PAR1" + a[1][4:]
        assert get(server, files[0].route)[1] == files[0].path.read_bytes()
        assert get(server, files[1].route)[1] == files[1].path.read_bytes()
    by_file = {}
    for o, file_id in zip(server.observations, server.observation_files, strict=True):
        by_file.setdefault(file_id, []).append(o)
    assert set(by_file) == {"f001", "f002"}
    first = [o.response_span.pair() for o in by_file["f001"] if o.requested_range == "bytes=0-7"]
    second = [o.response_span.pair() for o in by_file["f002"] if o.requested_range == "bytes=0-7"]
    assert first == second == [(0, 8)]  # identical offsets, different files


@pytest.mark.parametrize(
    "name",
    [
        "région=Zürich/données 1.parquet",
        "region=a b&c/x.parquet",
        "region=x%y#z?q/it's.parquet",
        "ж=ю/ф.parquet",
    ],
)
def test_unicode_spaces_and_special_characters_have_exact_routes(tmp_path, name):
    (file,) = served(tmp_path, [name])
    assert "'" not in file.route and " " not in file.route and "#" not in file.route
    with FileSetServer([file]) as server:
        assert get(server, file.route)[1] == file.path.read_bytes()
        # An equivalent spelling of the same bytes still names the same file.
        decoded_spelling = "/tok/" + "/".join(quote(s, safe="=~") for s in name.split("/"))
        assert get(server, decoded_spelling)[0] == 200
        assert server.file_for_url(server.url_for(file)).file_id == "f001"


@pytest.mark.parametrize(
    "target",
    [
        "/",
        "/tok/",
        "/tok",
        "/tok/..",
        "/tok/../etc/passwd",
        "/tok/month=1/../month=1/data.parquet",
        "/tok/month=1%2Fdata.parquet",
        "/tok/month=1/data.parquet?x=1",
        "/tok/month=1/data.parquet#x",
        "/other/month=1/data.parquet",
        "/tok/month=1/data.parquet/",
        "/tok/%ff",
        "/etc/passwd",
        "/tok/month=1/%2e%2e/month=1/data.parquet",
    ],
)
def test_nothing_outside_the_manifest_is_reachable(tmp_path, target):
    files = served(tmp_path, ["month=1/data.parquet"])
    with FileSetServer(files) as server:
        assert get(server, target)[0] == 404
        assert get(server, target, method="HEAD")[0] == 404
        assert server.unmatched == 2
        assert server.observations == []
        assert get(server, files[0].route)[0] == 200


def test_changing_one_file_invalidates_only_that_file(tmp_path):
    files = served(tmp_path, ["a/x.parquet", "b/x.parquet"])
    with FileSetServer(files) as server:
        assert get(server, files[0].route)[0] == 200
        with files[1].path.open("ab") as out:
            out.write(b"changed")
        assert get(server, files[1].route)[0] == 409
        assert get(server, files[0].route)[0] == 200
    assert server.changed and server.changed_files == {"f002"}


def test_replacement_by_rename_is_detected_even_with_the_same_content(tmp_path):
    (file,) = served(tmp_path, ["a/x.parquet"])
    replacement = tmp_path / "replacement"
    replacement.write_bytes(file.path.read_bytes())
    with FileSetServer([file]) as server:
        replacement.replace(file.path)
        assert get(server, file.route)[0] == 409
    assert server.changed_files == {"f001"}


def test_deleting_a_file_is_a_change(tmp_path):
    (file,) = served(tmp_path, ["a/x.parquet"])
    with FileSetServer([file]) as server:
        file.path.unlink()
        assert get(server, file.route)[0] == 409
    assert server.changed


def test_observation_budget_is_enforced(tmp_path, monkeypatch):
    monkeypatch.setattr(range_server, "MAX_OBSERVATIONS", 2)
    (file,) = served(tmp_path, ["a/x.parquet"])
    with FileSetServer([file]) as server:
        for _ in range(4):
            get(server, file.route, method="HEAD")
    assert len(server.observations) == 2 and server.truncated


def test_connection_budget_closes_excess_connections_and_reports_them(tmp_path):
    (file,) = served(tmp_path, ["a/x.parquet"])
    with FileSetServer([file], max_connections=1) as server:
        holder = socket.create_connection(("127.0.0.1", server.port))  # idle, holds the slot
        deadline = time.monotonic() + 2
        while server.open_connections < 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        extra = socket.create_connection(("127.0.0.1", server.port))
        extra.settimeout(2)
        assert extra.recv(10) == b""  # closed by the server, no response
        extra.close()
        assert server.rejected_connections == 1
        holder.close()
        deadline = time.monotonic() + 3
        while server.open_connections and time.monotonic() < deadline:
            time.sleep(0.02)
        assert get(server, file.route)[0] == 200  # the slot was released
    assert server.open_connections == 0


def test_duplicate_routes_are_refused(tmp_path):
    (file,) = served(tmp_path, ["a/x.parquet"])
    with pytest.raises(ValueError, match="Duplicate routes"):
        FileSetServer([file, file])


def test_foreign_urls_are_not_attributed(tmp_path):
    (file,) = served(tmp_path, ["a/x.parquet"])
    with FileSetServer([file]) as server:
        assert server.file_for_url(f"http://127.0.0.1:1{file.route}") is None
        assert server.file_for_url(f"https://127.0.0.1:{server.port}{file.route}") is None
        assert server.file_for_url(f"http://localhost:{server.port}{file.route}") is None
