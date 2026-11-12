"""Model bounds use a controlled clock; socket tests use broad scheduling tolerances."""

import http.client
import math
import socket
import struct
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from pydantic import ValidationError

from parquetscope.http_profile import BodyLimiter, HTTPProfile, profile_evidence
from parquetscope.metadata import identity
from parquetscope.range_server import FileSetServer, RangeServer, ServedFile


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def wait(self, seconds):
        self.now = math.nextafter(self.now + seconds, math.inf)


def noop():
    pass


def wait_for(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "HTTP observer did not finish in time"
        time.sleep(0.005)


def fetch(server, route=None, method="GET"):
    connection = http.client.HTTPConnection("127.0.0.1", server.port, timeout=3)
    try:
        connection.request(method, route or server.route)
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def binary(tmp_path, name="input.bin", size=20000):
    path = tmp_path / name
    path.write_bytes(b"x" * size)
    return path


@pytest.mark.parametrize(
    "values",
    [
        {"response_delay_ms": -1},
        {"response_delay_ms": 60001},
        {"response_delay_ms": 0.5},
        {"response_delay_ms": True},
        {"response_delay_ms": "12"},
        {"body_bytes_per_second": 0},
        {"body_bytes_per_second": -1},
        {"body_bytes_per_second": 1000000001},
        {"body_bytes_per_second": 1.1},
        {"body_bytes_per_second": False},
    ],
)
def test_invalid_profiles(values):
    with pytest.raises(ValidationError):
        HTTPProfile(**values)


def test_profile_provenance_is_exact():
    assert profile_evidence(HTTPProfile()).burst_bytes == "0"
    for rate, burst in [(1, 1), (19, 2), (100000, 10000), (1000000000, 16384)]:
        evidence = profile_evidence(HTTPProfile(body_bytes_per_second=rate))
        assert int(evidence.burst_bytes) == evidence.write_chunk_bytes == burst
        data = evidence.model_dump()
        data["burst_bytes"] = str(burst + 1)
        with pytest.raises(ValidationError):
            type(evidence)(**data)
        data = evidence.model_dump()
        data["effective"]["response_delay_ms"] = 1
        with pytest.raises(ValidationError):
            type(evidence)(**data)


def test_shared_reservations_are_bounded_and_do_not_accumulate_idle_credit():
    clock = Clock()
    limiter = BodyLimiter(1000, 100, clock)
    reservations = []
    # Alternating callers still share one bucket, with no per-connection allowance.
    for _caller in [0, 1] * 12:
        limiter.reserve(100, noop, clock.wait)
        reservations.append(clock.now)
    assert reservations[0] == 0
    assert clock.now >= 2.3 - 1e-9
    for first in range(len(reservations)):
        for last in range(first, len(reservations)):
            count = (last - first + 1) * 100
            assert count <= 100 + 1000 * (reservations[last] - reservations[first]) + 1e-7
    clock.now += 100000
    assert limiter.try_reserve(100) == 0
    assert limiter.try_reserve(100) == pytest.approx(0.1)
    with pytest.raises(ValueError):
        limiter.try_reserve(101)


def test_no_refill_or_queued_reservations_during_inflight_write():
    clock = Clock()
    limiter = BodyLimiter(1000, 100, clock)
    limiter.begin_write(100, noop, clock.wait)
    clock.now += 10  # A stalled stream must not earn the next response another burst.
    limiter.finish_write()
    assert limiter.try_reserve(100) == pytest.approx(0.1)
    limiter.begin_write(100, noop, clock.wait)
    limiter.finish_write()
    assert clock.now == pytest.approx(10.1)


def test_captures_have_independent_limiter_state(tmp_path):
    path = binary(tmp_path)
    profile = HTTPProfile(body_bytes_per_second=1000)
    first = RangeServer(path, identity(path.stat()), profile=profile)
    second = RangeServer(path, identity(path.stat()), profile=profile)
    try:
        assert first.limiter.try_reserve(100) == 0
        assert first.limiter.try_reserve(100) > 0
        assert second.limiter.try_reserve(100) == 0
        profile.response_delay_ms = 60000
        assert first.profile_evidence.effective.response_delay_ms == 0
    finally:
        first.server.server_close()
        second.server.server_close()


def test_default_http_execution_and_request_correlation(tmp_path):
    path = binary(tmp_path)
    with RangeServer(path, identity(path.stat())) as server:
        status, headers, body = fetch(server)
    assert status == 200 and body == path.read_bytes()
    record = server.observations[0]
    assert headers["X-ParquetScope-Request-ID"] == record.id
    assert record.complete and record.termination == "completed"
    assert record.body_budget_wait_ms == 0
    assert record.written_body_bytes == record.reserved_body_bytes == "20000"
    assert server.profile_evidence.effective.model_dump() == HTTPProfile().model_dump()


@pytest.mark.parametrize("method", ["HEAD", "GET"])
def test_response_delay_is_once_before_headers_for_head_and_get(tmp_path, method):
    path = binary(tmp_path)
    with RangeServer(
        path, identity(path.stat()), profile=HTTPProfile(response_delay_ms=80)
    ) as server:
        start = time.monotonic()
        _, _, body = fetch(server, method=method)
        elapsed = time.monotonic() - start
    record = server.observations[0]
    assert elapsed >= 0.075  # 5ms tolerance for independent boundary reads.
    assert 75 <= record.profile_delay_ms < 1000
    assert record.start_ms <= record.profile_delay_started_ms <= record.headers_sent_ms
    assert record.headers_sent_ms - record.profile_delay_started_ms >= 75
    assert record.profile_delay_ms <= record.duration_ms
    assert len(body) == (0 if method == "HEAD" else 20000)
    if method == "HEAD":
        assert record.reserved_body_bytes == record.written_body_bytes == "0"
        assert record.body_write_started_ms is None


def test_parallel_responses_and_files_share_one_rate_and_burst(tmp_path, monkeypatch):
    paths = [binary(tmp_path, f"{index}.bin") for index in range(2)]
    files = [
        ServedFile(f"f{index + 1:03}", path, identity(path.stat()), f"/token/{index}")
        for index, path in enumerate(paths)
    ]
    writes = []
    original = FileSetServer.write_body

    def record_write(stream, block):
        writes.append((time.monotonic(), len(block)))
        return original(stream, block)

    monkeypatch.setattr(FileSetServer, "write_body", staticmethod(record_write))
    rate = 40000
    profile = HTTPProfile(body_bytes_per_second=rate)
    with FileSetServer(files, profile=profile) as server, ThreadPoolExecutor(2) as pool:
        start = time.monotonic()
        results = list(pool.map(lambda file: fetch(server, file.route), files))
        elapsed = time.monotonic() - start
    assert all(len(result[2]) == 20000 for result in results)
    burst = int(server.profile_evidence.burst_bytes)
    assert elapsed >= (40000 - burst) / rate - 0.03
    assert max(size for _, size in writes) <= burst == 4000
    # Scheduling can move timestamps around admission by a few milliseconds. The
    # deterministic test above enforces the exact algorithmic envelope separately.
    for first in range(len(writes)):
        for last in range(first, len(writes)):
            total = sum(size for _, size in writes[first : last + 1])
            assert total <= burst + rate * (writes[last][0] - writes[first][0] + 0.02)
    assert set(server.observation_files) == {"f001", "f002"}
    assert sum(int(o.written_body_bytes) for o in server.observations) == 40000
    assert all(o.body_budget_wait_ms > 0 for o in server.observations)
    assert len({o.id for o in server.observations}) == 2
    for result in results:
        assert result[1]["X-ParquetScope-Request-ID"] in {o.id for o in server.observations}


@pytest.mark.parametrize("phase", ["delay", "budget"])
def test_cancellation_interrupts_waits_and_retains_partial_bytes(tmp_path, phase):
    path = binary(tmp_path)
    cancel = threading.Event()
    profile = (
        HTTPProfile(response_delay_ms=5000)
        if phase == "delay"
        else HTTPProfile(body_bytes_per_second=1)
    )
    with RangeServer(path, identity(path.stat()), profile=profile, cancel=cancel) as server:
        connection = socket.create_connection(("127.0.0.1", server.port))
        connection.sendall(f"GET {server.route} HTTP/1.1\r\nHost: localhost\r\n\r\n".encode())
        wait_for(lambda: server.open_connections == 1)
        if phase == "budget":
            assert b"HTTP/1.1 200" in connection.recv(4096)
        time.sleep(0.08)
        start = time.monotonic()
        cancel.set()
        wait_for(lambda: bool(server.observations))
        assert time.monotonic() - start < 0.5
        connection.close()
    record = server.observations[0]
    assert not record.complete and record.termination == "cancelled"
    if phase == "delay":
        assert record.headers_sent_ms is None and record.written_body_bytes == "0"
        assert record.profile_delay_ms < 1000
    else:
        assert record.written_body_bytes == record.reserved_body_bytes == "1"
        assert record.body_budget_wait_ms > 0


def test_deadline_and_server_stop_interrupt_profile_waits(tmp_path):
    path = binary(tmp_path)
    with RangeServer(
        path,
        identity(path.stat()),
        profile=HTTPProfile(response_delay_ms=60000),
        deadline_at=time.monotonic() + 0.15,
    ) as server:
        connection = socket.create_connection(("127.0.0.1", server.port))
        connection.sendall(f"HEAD {server.route} HTTP/1.1\r\nHost: localhost\r\n\r\n".encode())
        wait_for(lambda: bool(server.observations))
        connection.close()
    assert server.observations[0].termination == "timeout"
    with RangeServer(
        path, identity(path.stat()), profile=HTTPProfile(body_bytes_per_second=1)
    ) as server:
        connection = socket.create_connection(("127.0.0.1", server.port))
        connection.sendall(f"GET {server.route} HTTP/1.1\r\nHost: localhost\r\n\r\n".encode())
        assert b"HTTP/1.1 200" in connection.recv(4096)
        start = time.monotonic()
    assert time.monotonic() - start < 0.5
    connection.close()
    assert server.observations[0].termination == "cancelled"


def test_disconnect_during_budget_wait_is_observed_without_spending_more_bytes(tmp_path):
    path = binary(tmp_path)
    with RangeServer(
        path, identity(path.stat()), profile=HTTPProfile(body_bytes_per_second=1)
    ) as server:
        connection = socket.create_connection(("127.0.0.1", server.port))
        connection.sendall(f"GET {server.route} HTTP/1.1\r\nHost: localhost\r\n\r\n".encode())
        connection.recv(4096)
        time.sleep(0.08)
        connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        connection.close()
        wait_for(lambda: bool(server.observations))
    record = server.observations[0]
    assert record.termination == "disconnect" and not record.complete
    assert record.reserved_body_bytes == record.written_body_bytes == "1"


@pytest.mark.parametrize("short", [False, True])
def test_write_failure_and_short_write_keep_exact_known_prefix(tmp_path, monkeypatch, short):
    path = binary(tmp_path, size=40000)
    calls = 0
    original = FileSetServer.write_body

    def fail_write(stream, block):
        nonlocal calls
        calls += 1
        if short:
            return original(stream, block[:17])
        if calls == 2:
            raise OSError("injected stream write failure")
        return original(stream, block)

    monkeypatch.setattr(FileSetServer, "write_body", staticmethod(fail_write))
    with RangeServer(
        path, identity(path.stat()), profile=HTTPProfile(body_bytes_per_second=1000000)
    ) as server:
        with pytest.raises(http.client.IncompleteRead) as exc:
            fetch(server)
    record = server.observations[0]
    expected = 17 if short else 16384
    assert len(exc.value.partial) == expected
    assert int(record.written_body_bytes) == expected
    assert int(record.reserved_body_bytes) == (16384 if short else 32768)
    assert not record.complete and record.termination == "write_error"
    assert record.body_write_started_ms <= record.body_write_finished_ms
    assert record.body_write_ms <= record.duration_ms


def test_unknown_routes_keep_immediate_error_contract(tmp_path):
    path = binary(tmp_path)
    with RangeServer(
        path, identity(path.stat()), profile=HTTPProfile(response_delay_ms=60000)
    ) as server:
        start = time.monotonic()
        assert fetch(server, "/unknown")[0] == 404
        assert time.monotonic() - start < 1
    assert server.unmatched == 1 and not server.observations


def test_waiting_for_an_inflight_write_remains_cancellable():
    limiter = BodyLimiter(1000, 100)
    limiter.begin_write(100, noop, time.sleep)
    cancel = threading.Event()

    def check():
        if cancel.is_set():
            raise RuntimeError("cancelled while waiting for write admission")

    with ThreadPoolExecutor(1) as pool:
        waiting = pool.submit(limiter.begin_write, 100, check, time.sleep)
        cancel.set()
        with pytest.raises(RuntimeError, match="cancelled while waiting"):
            waiting.result(timeout=0.5)
    limiter.finish_write()


@pytest.mark.parametrize("method", ["HEAD", "GET"])
@pytest.mark.parametrize(
    "profile", [None, HTTPProfile(response_delay_ms=20, body_bytes_per_second=1000000)]
)
def test_client_half_close_still_receives_complete_response(tmp_path, method, profile):
    path = binary(tmp_path)
    with RangeServer(path, identity(path.stat()), profile=profile) as server:
        connection = socket.create_connection(("127.0.0.1", server.port))
        connection.settimeout(3)
        connection.sendall(f"{method} {server.route} HTTP/1.1\r\nHost: localhost\r\n\r\n".encode())
        connection.shutdown(socket.SHUT_WR)
        response = http.client.HTTPResponse(connection, method=method)
        response.begin()
        assert response.status == 200
        assert response.read() == (b"" if method == "HEAD" else path.read_bytes())
        connection.close()
    record = server.observations[0]
    assert record.complete and record.termination == "completed"
    assert int(record.written_body_bytes) == (0 if method == "HEAD" else path.stat().st_size)
