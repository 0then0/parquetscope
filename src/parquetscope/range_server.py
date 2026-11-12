"""An independent, streaming HTTP observer for a fixed set of files owned by one run.

Only routes of the prepared files are served: no listing, no path interpretation, no fallback.
Each request opens its file, verifies that its identity is still the prepared one and streams
with pread, so open descriptors are bounded by concurrent connections, not by set size.
"""

import os
import select
import socket
import threading
import time
import uuid
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

from .http_profile import (
    POLL_SECONDS,
    BodyLimiter,
    HTTPProfile,
    TransferInterrupted,
    profile_evidence,
)
from .intervals import UnsatisfiableRange, parse_range
from .metadata import identity
from .models import span
from .models_v2 import split_logical_path
from .models_v4 import ServerObservation4

MAX_OBSERVATIONS = 100000
MAX_CONNECTIONS = 64


@dataclass(frozen=True)
class ServedFile:
    file_id: str
    path: Path
    identity: tuple
    route: str


def encode_route(token: str, logical_path: str) -> str:
    """Percent-encode every segment; `=` stays readable so Hive segments keep their shape."""
    segments = split_logical_path(logical_path)
    return f"/{token}/" + "/".join(quote(segment, safe="=") for segment in segments)


class FileSetServer:
    def __init__(
        self,
        files: list[ServedFile],
        max_connections: int = MAX_CONNECTIONS,
        profile: HTTPProfile | None = None,
        cancel: threading.Event | None = None,
        deadline_at: float | None = None,
    ):
        self.files = list(files)
        # Lookup is by exact decoded route, so '..', encoded slashes and listings never match.
        self.routes = {
            "/".join(unquote(part) for part in f.route.split("/")): f for f in self.files
        }
        if len(self.routes) != len(self.files):
            raise ValueError("Duplicate routes")
        self.max_connections = max_connections
        self.profile_evidence = profile_evidence((profile or HTTPProfile()).model_copy(deep=True))
        self._cancel = cancel or threading.Event()
        self._stopped = threading.Event()
        self._stop_reason = "cancelled"
        self.deadline_at = deadline_at
        rate = self.profile_evidence.effective.body_bytes_per_second
        self.limiter = BodyLimiter(rate, int(self.profile_evidence.burst_bytes)) if rate else None
        self.observations: list[ServerObservation4] = []
        self.observation_files: list[str] = []  # file_id of each observation, same index
        self.lock = threading.Lock()
        self.changed = False
        self.changed_files: set[str] = set()
        self.truncated = False
        self.unmatched = 0
        self.rejected_connections = 0
        self.open_connections = 0
        self.started = time.monotonic()
        self.active: set[socket.socket] = set()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):
                pass

            def setup(self):
                super().setup()
                self.connection.settimeout(2)
                with owner.lock:
                    owner.active.add(self.connection)

            def finish(self):
                try:
                    super().finish()
                finally:
                    with owner.lock:
                        owner.active.discard(self.connection)

            def do_HEAD(self):
                self.serve_file()

            def do_GET(self):
                self.serve_file()

            def serve_file(self):
                self.close_connection = True
                file = owner.lookup(self.path)
                if file is None:
                    with owner.lock:
                        owner.unmatched += 1
                    self.send_error(404)
                    return
                size = file.identity[2]
                start_time = time.monotonic()
                request_id = uuid.uuid4().hex
                requested = self.headers.get("Range")
                response_span = None
                written = 0
                complete = False
                error = None
                status = 200
                begin, end = 0, size
                fd = None
                delay_started = None
                delay_ms = 0.0
                headers_sent = None
                budget_wait_ms = 0.0
                write_started = None
                write_finished = None
                write_ms = 0.0
                reserved = 0
                termination = "completed"

                def check():
                    owner.check_running()
                    # A TCP reset can interrupt a wait. EOF alone is a valid half-close:
                    # the client may have finished sending while still reading our response.
                    readable, _, _ = select.select([self.connection], [], [], 0)
                    if readable:
                        try:
                            self.connection.recv(1, socket.MSG_PEEK)
                        except OSError as exc:
                            raise TransferInterrupted("disconnect") from exc

                def wait(seconds):
                    owner._stopped.wait(seconds)

                try:
                    if owner.verify(file):
                        fd = os.open(file.path, os.O_RDONLY)
                        if not owner.verify(file, fd):
                            status = 409
                    else:
                        status = 409
                except OSError:
                    status = 409
                    owner.mark_changed(file)
                # RFC 9110: Range is defined for GET; HEAD ignores it.
                if status == 200 and self.command == "GET" and requested:
                    # A server MAY ignore Range. We explicitly ignore unsupported
                    # units, malformed syntax and multi-range and send full 200.
                    if requested.startswith("bytes=") and "," not in requested:
                        try:
                            begin, end = parse_range(requested, size)
                            status = 206
                        except UnsatisfiableRange:
                            status = 416
                body_size = end - begin if status in (200, 206) else 0
                if self.command == "GET" and status in (200, 206) and end > begin:
                    response_span = span(begin, end)
                try:
                    check()
                    delay_started = time.monotonic()
                    try:
                        delay_end = delay_started + (
                            owner.profile_evidence.effective.response_delay_ms / 1000
                        )
                        while True:
                            check()
                            remaining = delay_end - time.monotonic()
                            if remaining <= 0:
                                break
                            wait(min(remaining, POLL_SECONDS))
                    finally:
                        delay_ms = (time.monotonic() - delay_started) * 1000
                    self.send_response(status)
                    self.send_header("Accept-Ranges", "bytes")
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Content-Length", str(body_size))
                    self.send_header("ETag", '"' + str(file.identity[3]) + '"')
                    self.send_header("X-ParquetScope-Request-ID", request_id)
                    self.send_header("Connection", "close")
                    if status == 206:
                        self.send_header("Content-Range", f"bytes {begin}-{end - 1}/{size}")
                    elif status == 416:
                        self.send_header("Content-Range", f"bytes */{size}")
                    self.end_headers()
                    headers_sent = time.monotonic()
                    if self.command == "GET" and body_size:
                        cursor = begin
                        while cursor < end:
                            check()
                            if not owner.verify(file, fd):
                                raise OSError("Input changed during capture")
                            block = os.pread(
                                fd,
                                min(owner.profile_evidence.write_chunk_bytes, end - cursor),
                                cursor,
                            )
                            if not block:
                                raise OSError("Unexpected EOF")
                            if owner.limiter:
                                budget_started = time.monotonic()
                                try:
                                    owner.limiter.begin_write(len(block), check, wait)
                                finally:
                                    budget_wait_ms += (time.monotonic() - budget_started) * 1000
                            reserved += len(block)
                            try:
                                check()
                                write_start = time.monotonic()
                                if write_started is None:
                                    write_started = write_start
                                try:
                                    count = owner.write_body(self.wfile, block)
                                    written += count
                                    cursor += count
                                    if count != len(block):
                                        raise OSError("Short body write")
                                finally:
                                    write_finished = time.monotonic()
                                    write_ms += (write_finished - write_start) * 1000
                            finally:
                                if owner.limiter:
                                    owner.limiter.finish_write()
                        self.wfile.flush()
                    complete = True
                except TransferInterrupted as exc:
                    termination = exc.reason
                    error = str(exc)
                except (OSError, TimeoutError) as exc:
                    if owner._stopped.is_set() or owner._cancel.is_set():
                        termination = owner._stop_reason
                    elif owner.deadline_at is not None and time.monotonic() >= owner.deadline_at:
                        termination = "timeout"
                    elif isinstance(exc, (BrokenPipeError, ConnectionResetError)):
                        termination = "disconnect"
                    elif isinstance(exc, TimeoutError):
                        termination = "timeout"
                    else:
                        termination = "write_error"
                    error = str(exc)
                finally:
                    if fd is not None:
                        os.close(fd)
                    observation = ServerObservation4(
                        id=request_id,
                        method=self.command,
                        requested_range=requested,
                        response_span=response_span,
                        status=status,
                        start_ms=(start_time - owner.started) * 1000,
                        duration_ms=(time.monotonic() - start_time) * 1000,
                        declared_body_bytes=str(body_size if self.command == "GET" else 0),
                        written_body_bytes=str(written),
                        complete=complete,
                        error=error,
                        profile_delay_started_ms=(
                            (delay_started - owner.started) * 1000
                            if delay_started is not None
                            else None
                        ),
                        profile_delay_ms=delay_ms,
                        headers_sent_ms=(
                            (headers_sent - owner.started) * 1000
                            if headers_sent is not None
                            else None
                        ),
                        body_budget_wait_ms=budget_wait_ms,
                        body_write_started_ms=(
                            (write_started - owner.started) * 1000
                            if write_started is not None
                            else None
                        ),
                        body_write_finished_ms=(
                            (write_finished - owner.started) * 1000
                            if write_finished is not None
                            else None
                        ),
                        body_write_ms=write_ms,
                        reserved_body_bytes=str(reserved),
                        termination=termination,
                    )
                    with owner.lock:
                        if len(owner.observations) < MAX_OBSERVATIONS:
                            owner.observations.append(observation)
                            owner.observation_files.append(file.file_id)
                        else:
                            owner.truncated = True

        class Server(ThreadingHTTPServer):
            daemon_threads = False
            block_on_close = True

            def process_request(self, request, client_address):
                # A finite concurrency budget: excess connections are closed and reported.
                with owner.lock:
                    reject = owner.open_connections >= owner.max_connections
                    if reject:
                        owner.rejected_connections += 1
                    else:
                        owner.open_connections += 1
                if reject:
                    self.shutdown_request(request)
                    return
                super().process_request(request, client_address)

            def process_request_thread(self, request, client_address):
                try:
                    super().process_request_thread(request, client_address)
                finally:
                    with owner.lock:
                        owner.open_connections -= 1

        self.server = Server(("127.0.0.1", 0), Handler)
        self.port = self.server.server_port
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.05}
        )

    def url_for(self, file: ServedFile) -> str:
        return f"http://127.0.0.1:{self.port}{file.route}"

    def lookup(self, target: str) -> ServedFile | None:
        if "?" in target or "#" in target:
            return None
        try:
            # Decode per segment: an encoded slash is data inside a segment, never a separator.
            segments = [unquote(segment, errors="strict") for segment in target.split("/")]
        except UnicodeDecodeError:
            return None
        if any("/" in segment for segment in segments):
            return None
        return self.routes.get("/".join(segments))

    def file_for_url(self, url: str) -> ServedFile | None:
        """Attribute a client-side URL to a served file; foreign hosts and ports never match."""
        parts = urlsplit(url)
        if parts.scheme != "http" or parts.netloc != f"127.0.0.1:{self.port}":
            return None
        return self.lookup(parts.path + (f"?{parts.query}" if parts.query else ""))

    def mark_changed(self, file: ServedFile):
        with self.lock:
            self.changed = True
            self.changed_files.add(file.file_id)

    def verify(self, file: ServedFile, fd: int | None = None) -> bool:
        try:
            same = identity(file.path.stat()) == file.identity and (
                fd is None or identity(os.fstat(fd)) == file.identity
            )
        except OSError:
            same = False
        if not same:
            self.mark_changed(file)
        return same

    def verify_all(self):
        for file in self.files:
            self.verify(file)

    @staticmethod
    def write_body(stream, block: bytes) -> int:
        """Count only completed writes (sendall failures have no known byte prefix)."""
        return stream.write(block)

    def check_running(self):
        if self._stopped.is_set():
            raise TransferInterrupted(self._stop_reason)
        if self._cancel.is_set():
            raise TransferInterrupted("cancelled")
        if self.deadline_at is not None and time.monotonic() >= self.deadline_at:
            raise TransferInterrupted("timeout")

    def cancel(self, reason: str = "cancelled"):
        with self.lock:
            if not self._stopped.is_set():
                self._stop_reason = reason
                self._stopped.set()
            active = list(self.active)
        for connection in active:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_args):
        self.cancel()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.verify_all()


class RangeServer(FileSetServer):
    """The v1 single-file observer: one route, fixed `input.parquet` name."""

    def __init__(
        self,
        path: Path,
        expected_identity: tuple,
        profile: HTTPProfile | None = None,
        cancel: threading.Event | None = None,
        deadline_at: float | None = None,
    ):
        self.path = path
        self.expected_identity = expected_identity
        self.size = expected_identity[2]
        self.route = f"/{uuid.uuid4().hex}/input.parquet"
        file = ServedFile("input", path, expected_identity, self.route)
        super().__init__([file], profile=profile, cancel=cancel, deadline_at=deadline_at)
        self.url = self.url_for(file)
