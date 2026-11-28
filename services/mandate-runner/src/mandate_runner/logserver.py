"""Serves run logs read-only, so the status link opens the run's log."""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .store import RUN_ID


def make_handler(logs_dir: Path) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/healthz":
                return self._send(200, b"ok\n")
            prefix, suffix = "/runs/", ".log"
            if self.path.startswith(prefix) and self.path.endswith(suffix):
                run_id = self.path[len(prefix) : -len(suffix)]
                if RUN_ID.match(run_id):
                    path = logs_dir / f"{run_id}.log"
                    if path.is_file():
                        return self._send(200, path.read_bytes())
            self._send(404, b"not found\n")

        def _send(self, code: int, body: bytes) -> None:
            self.send_response(code)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            return

    return Handler


def serve(logs_dir: Path, host: str, port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), make_handler(logs_dir))
    threading.Thread(target=server.serve_forever, name="logserver", daemon=True).start()
    return server
