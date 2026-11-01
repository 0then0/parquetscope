"""An independent, streaming HTTP observer for a fixed set of files owned by one run.

Only routes of the prepared files are served: no listing, no path interpretation, no fallback.
Each request opens its file, verifies that its identity is still the prepared one and streams
with pread, so open descriptors are bounded by concurrent connections, not by set size.
"""

import os
import socket
import threading
import time
import uuid
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

from .intervals import UnsatisfiableRange, parse_range
from .metadata import identity
from .models import ServerObservation, span
from .models_v2 import split_logical_path

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
    def __init__(self, files: list[ServedFile], max_connections: int = MAX_CONNECTIONS):
        self.files = list(files)
        # Lookup is by exact decoded route, so '..', encoded slashes and listings never match.
        self.routes = {
            "/".join(unquote(part) for part in f.route.split("/")): f for f in self.files
        }
        if len(self.routes) != len(self.files):
            raise ValueError("Duplicate routes")
        self.max_connections = max_connections
        self.observations: list[ServerObservation] = []
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
                    if self.command == "GET" and body_size:
                        cursor = begin
                        while cursor < end:
                            if not owner.verify(file, fd):
                                raise OSError("Input changed during capture")
                            block = os.pread(fd, min(65536, end - cursor), cursor)
                            if not block:
                                raise OSError("Unexpected EOF")
                            self.wfile.write(block)
                            written += len(block)
                            cursor += len(block)
                        self.wfile.flush()
                    complete = True
                except (OSError, TimeoutError) as exc:
                    error = str(exc)
                finally:
                    if fd is not None:
                        os.close(fd)
                    observation = ServerObservation(
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

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_args):
        self.server.shutdown()
        with self.lock:
            for connection in list(self.active):
                try:
                    connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
        self.server.server_close()
        self.thread.join(timeout=3)
        self.verify_all()


class RangeServer(FileSetServer):
    """The v1 single-file observer: one route, fixed `input.parquet` name."""

    def __init__(self, path: Path, expected_identity: tuple):
        self.path = path
        self.expected_identity = expected_identity
        self.size = expected_identity[2]
        self.route = f"/{uuid.uuid4().hex}/input.parquet"
        file = ServedFile("input", path, expected_identity, self.route)
        super().__init__([file])
        self.url = self.url_for(file)
