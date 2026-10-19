"""An independent, streaming, single-file HTTP observer owned by one run."""

import os
import socket
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .intervals import UnsatisfiableRange, parse_range
from .metadata import identity
from .models import ServerObservation, span


class RangeServer:
    def __init__(self, path: Path, expected_identity: tuple):
        self.path = path
        self.expected_identity = expected_identity
        self.fd = os.open(path, os.O_RDONLY)
        self.size = expected_identity[2]
        self.observations: list[ServerObservation] = []
        self.lock = threading.Lock()
        self.changed = False
        self.truncated = False
        self.started = time.monotonic()
        self.route = f"/{uuid.uuid4().hex}/input.parquet"
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
                if self.path != owner.route:
                    self.send_error(404)
                    return
                start_time = time.monotonic()
                request_id = uuid.uuid4().hex
                requested = self.headers.get("Range")
                response_span = None
                written = 0
                complete = False
                error = None
                status = 200
                begin, end = 0, owner.size
                if not owner.unchanged():
                    status = 409
                # RFC 9110: Range is defined for GET; HEAD ignores it.
                elif self.command == "GET" and requested:
                    # A server MAY ignore Range. We explicitly ignore unsupported
                    # units, malformed syntax and multi-range and send full 200.
                    if requested.startswith("bytes=") and "," not in requested:
                        try:
                            begin, end = parse_range(requested, owner.size)
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
                    self.send_header("ETag", '"' + str(owner.expected_identity[3]) + '"')
                    self.send_header("X-ParquetScope-Request-ID", request_id)
                    self.send_header("Connection", "close")
                    if status == 206:
                        self.send_header("Content-Range", f"bytes {begin}-{end - 1}/{owner.size}")
                    elif status == 416:
                        self.send_header("Content-Range", f"bytes */{owner.size}")
                    self.end_headers()
                    if self.command == "GET" and body_size:
                        cursor = begin
                        while cursor < end:
                            if not owner.unchanged():
                                raise OSError("Input changed during capture")
                            block = os.pread(owner.fd, min(65536, end - cursor), cursor)
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
                        if len(owner.observations) < 100000:
                            owner.observations.append(observation)
                        else:
                            owner.truncated = True

        class Server(ThreadingHTTPServer):
            daemon_threads = False
            block_on_close = True

        self.server = Server(("127.0.0.1", 0), Handler)
        self.port = self.server.server_port
        self.url = f"http://127.0.0.1:{self.port}{self.route}"
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.05}
        )

    def unchanged(self):
        try:
            same = (
                identity(self.path.stat()) == self.expected_identity
                and identity(os.fstat(self.fd)) == self.expected_identity
            )
        except OSError:
            same = False
        if not same:
            self.changed = True
        return same

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
        self.unchanged()
        os.close(self.fd)
