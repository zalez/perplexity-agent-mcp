"""A stdlib HTTP server that impersonates api.perplexity.ai.

Tests point the module's API_BASE at this in-process. Deliberately NOT wired
through an environment variable: a configurable base URL in the shipped server
would be an API-key exfiltration vector.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any


class FakePerplexity:
    """Serves a scripted sequence of responses, and records what it was sent."""

    def __init__(self) -> None:
        self._responses: list[tuple[int, dict[str, Any]]] = []
        self._index = 0
        self.requests: list[tuple[str, str, dict[str, Any]]] = []
        self._lock = threading.Lock()

        fake = self

        class Handler(BaseHTTPRequestHandler):
            def _handle(self, method: str) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                try:
                    body = json.loads(raw) if raw else {}
                except json.JSONDecodeError:
                    body = {"__unparseable__": raw.decode("utf-8", "replace")}
                with fake._lock:
                    fake.requests.append((method, self.path, body))
                    status, payload = fake._next()
                encoded = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def do_GET(self) -> None:  # BaseHTTPRequestHandler API
                self._handle("GET")

            def do_POST(self) -> None:  # BaseHTTPRequestHandler API
                self._handle("POST")

            def log_message(self, *args: Any) -> None:
                pass  # keep the test output clean

        self._server = HTTPServer(("127.0.0.1", 0), Handler)
        # serve_forever()'s default poll_interval is 0.5s: it polls the socket
        # with that timeout and only checks for a pending shutdown() request
        # between polls. With every test in the suite spinning up and tearing
        # down one of these, that default turns close() into a ~0.5s wait,
        # nearly every time - a fixed tax on every test, unrelated to what any
        # of them actually verify. A short interval keeps shutdown() responsive
        # without changing anything about what gets served.
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        self._thread.start()

    def _next(self) -> tuple[int, dict[str, Any]]:
        if not self._responses:
            return 200, {}
        # The last scripted response repeats forever, so a poll loop that runs
        # one extra iteration doesn't spuriously fail.
        index = min(self._index, len(self._responses) - 1)
        self._index += 1
        return self._responses[index]

    def script(self, *responses: tuple[int, dict[str, Any]]) -> None:
        """Queue responses, served in order. The last one repeats."""
        self._responses = list(responses)
        self._index = 0

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        # typeshed types server_address as str | bytes | bytearray to cover
        # AF_UNIX sockets; this server is always AF_INET, bound with a str
        # host, so the host half is always a str at runtime.
        assert isinstance(host, str)
        return f"http://{host}:{port}"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)
