"""A deliberately unreliable "external API" for trying the orchestrator locally.

Standard library only:  python mock_external/server.py   (listens on :9000)

Endpoints (all JSON)
  GET  /ok                              always 200
  GET  /slow?delay=5                    answers 200 after `delay` seconds
  GET  /flaky?rate=0.5                  503 with probability `rate`, else 200
  GET  /fail-then-succeed?fails=2&key=k 503 for the first `fails` calls per key, then 200
  GET  /rate-limited?limit=2&key=k      429 + Retry-After: 1 for the first `limit` calls, then 200
  GET  /always-500                      always 500
  GET  /not-found                       always 404
  POST /echo   (also PUT/PATCH/DELETE)  echoes the JSON body back

Control
  GET  /admin/outage/on                 every other endpoint now returns 503
  GET  /admin/outage/off                back to normal
  GET  /admin/reset                     clear per-key counters and the outage flag
"""

from __future__ import annotations

import json
import os
import random
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

_lock = threading.Lock()
_counters: dict[str, int] = {}
_outage = False


def _bump(key: str) -> int:
    with _lock:
        _counters[key] = _counters.get(key, 0) + 1
        return _counters[key]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        sys.stderr.write(f"[mock-upstream] {self.command} {self.path} -> {fmt % args}\n")

    # ---------------------------------------------------------------- helpers
    def _send(self, status: int, body: dict, headers: dict[str, str] | None = None) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    # ---------------------------------------------------------------- routing
    def _handle(self) -> None:
        global _outage
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        raw = self._read_body()  # always drain the body so keep-alive connections stay in sync

        if url.path == "/admin/outage/on":
            _outage = True
            return self._send(200, {"outage": True})
        if url.path == "/admin/outage/off":
            _outage = False
            return self._send(200, {"outage": False})
        if url.path == "/admin/reset":
            with _lock:
                _counters.clear()
            _outage = False
            return self._send(200, {"reset": True})

        if _outage:
            return self._send(503, {"error": "simulated outage"})

        if url.path == "/ok":
            return self._send(200, {"message": "ok", "time": time.time()})

        if url.path == "/slow":
            time.sleep(float(q.get("delay", "5")))
            return self._send(200, {"message": "finally"})

        if url.path == "/flaky":
            if random.random() < float(q.get("rate", "0.5")):
                return self._send(503, {"error": "random failure"})
            return self._send(200, {"message": "got lucky"})

        if url.path == "/fail-then-succeed":
            n = _bump("fts:" + q.get("key", "default"))
            if n <= int(q.get("fails", "2")):
                return self._send(503, {"error": "not yet", "call": n})
            return self._send(200, {"message": "recovered", "call": n})

        if url.path == "/rate-limited":
            n = _bump("rl:" + q.get("key", "default"))
            if n <= int(q.get("limit", "2")):
                return self._send(429, {"error": "slow down", "call": n}, {"Retry-After": "1"})
            return self._send(200, {"message": "allowed", "call": n})

        if url.path == "/always-500":
            return self._send(500, {"error": "always broken"})

        if url.path == "/not-found":
            return self._send(404, {"error": "no such thing"})

        if url.path == "/echo":
            try:
                body = json.loads(raw) if raw else None
            except ValueError:
                return self._send(400, {"error": "invalid JSON"})
            return self._send(200, {"method": self.command, "echo": body})

        return self._send(404, {"error": f"unknown path {url.path}"})

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _handle  # noqa: N815


def main() -> None:
    host = os.environ.get("MOCK_HOST", "127.0.0.1")
    port = int(os.environ.get("MOCK_PORT", "9000"))
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    print(f"[mock-upstream] listening on http://{host}:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
