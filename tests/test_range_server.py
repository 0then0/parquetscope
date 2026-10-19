import http.client
import socket
import struct
import time

from parquetscope.metadata import identity
from parquetscope.range_server import RangeServer


def request(server, method="GET", header=None):
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=3)
    conn.request(method, server.route, headers={"Range": header} if header else {})
    res = conn.getresponse()
    status = res.status
    headers = dict(res.getheaders())
    data = res.read()
    conn.close()
    return status, headers, data


def test_head_full_get_and_single_ranges(parquet):
    original = parquet.read_bytes()
    with RangeServer(parquet, identity(parquet.stat())) as server:
        status, headers, data = request(server, "HEAD", "bytes=0-3")
        assert status == 200 and data == b"" and int(headers["Content-Length"]) == len(original)
        assert request(server)[2] == original
        for header, expected in [
            ("bytes=1-7", original[1:8]),
            ("bytes=-8", original[-8:]),
            ("bytes=10-", original[10:]),
        ]:
            status, headers, data = request(server, header=header)
            assert status == 206 and data == expected
            assert int(headers["Content-Length"]) == len(expected)
        status, headers, data = request(server, header=f"bytes={len(original)}-")
        assert status == 416 and data == b""
        assert headers["Content-Range"] == f"bytes */{len(original)}"
        # Explicit RFC 9110 policy: ignore unsupported multi-range and send 200.
        status, headers, data = request(server, header="bytes=0-1,4-5")
        assert status == 200 and data == original
    heads = [o for o in server.observations if o.method == "HEAD"]
    assert heads[0].written_body_bytes == "0" and heads[0].declared_body_bytes == "0"
    assert all(o.complete for o in server.observations)
    assert all(int(o.written_body_bytes) == int(o.declared_body_bytes) for o in server.observations)


def test_selected_file_only_and_changed_file(parquet):
    with RangeServer(parquet, identity(parquet.stat())) as server:
        conn = http.client.HTTPConnection("127.0.0.1", server.port)
        conn.request("GET", "/etc/passwd")
        assert conn.getresponse().status == 404
        conn.close()
        with parquet.open("ab") as out:
            out.write(b"changed")
        assert request(server)[0] == 409
    assert server.changed


def test_interrupted_transfer_is_observed(tmp_path):
    path = tmp_path / "large.bin"
    with path.open("wb") as out:
        out.truncate(64 * 1024 * 1024)
    with RangeServer(path, identity(path.stat())) as server:
        connection = socket.create_connection(("127.0.0.1", server.port))
        connection.sendall(f"GET {server.route} HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n".encode())
        connection.recv(1024)
        connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        connection.close()
        deadline = time.monotonic() + 3
        while not server.observations and time.monotonic() < deadline:
            time.sleep(0.01)
    record = server.observations[0]
    assert not record.complete and record.error
    assert int(record.written_body_bytes) < int(record.declared_body_bytes)
