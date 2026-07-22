#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Zero-dependency MCP server for the Perplexity Agent API.

Exposes Perplexity's Agent API (multi-step web research with citations) to any
MCP client over stdio. Standard library only: no MCP SDK, no HTTP library, no
build-time code generation. The file you are reading is the entire program, and
that is the point — it holds an API key and talks to the network on your behalf,
so it should be short enough to audit in one sitting.

Reads PERPLEXITY_API_KEY from the environment. Talks to exactly one host,
https://api.perplexity.ai, over TLS with certificate verification enabled.
Writes JSON-RPC to stdout and nothing else; all logging goes to stderr.

Homepage: https://github.com/zalez/perplexity-agent-mcp
License: BSD-3-Clause
"""

from __future__ import annotations

import sys

# --- Python version guard ----------------------------------------------------
# Checked before anything else runs. A clear message beats a SyntaxError from
# deep inside the file, which is what an older interpreter would otherwise emit.
#
# ruff's UP036 calls this dead code because target-version is py310 — but that
# is precisely the assumption this guard exists to check. It runs on exactly
# the interpreters where ruff's premise is false, so the check stays.
if sys.version_info < (3, 10):  # noqa: UP036  # pragma: no cover - version-dependent
    sys.stderr.write(
        f"perplexity-agent-mcp requires Python 3.10 or newer; this is {sys.version.split()[0]}.\n"
    )
    raise SystemExit(1)

import json
import os
import random
import re
import ssl
import time
import urllib.error
import urllib.request
from collections.abc import Callable

__version__ = "0.1.0"

# =============================================================================
# BAND 1 — CONFIG.  Constants only, no logic.
# =============================================================================

# The ONLY host this program will ever contact. Deliberately not configurable:
# an environment-variable override would let anyone who can edit an MCP client
# config redirect the API key to a host of their choosing. Tests reassign this
# in-process instead (see tests/test_perplexity_client.py).
API_BASE = "https://api.perplexity.ai"

# MCP revision we implement. See docs/specs — 2026-07-28 is a breaking change we
# deliberately do not yet implement.
PROTOCOL_VERSION = "2025-11-25"
SUPPORTED_PROTOCOL_VERSIONS = frozenset({"2025-11-25", "2025-06-18", "2025-03-26"})

SERVER_NAME = "perplexity-agent"
SERVER_TITLE = "Perplexity Agent"

# How long a blocking call waits before handing back a response_id. Defaults to
# 55s because Claude Desktop enforces a 60s tool-call timeout that its users
# cannot change. Clients with looser limits should raise this — see README.
WAIT_SECONDS_DEFAULT = 55

# =============================================================================
# BAND 2 — HTTP.  The only code in this program that touches the network.
#
# If you are auditing where your API key can go, this section is the entire
# answer. Nothing above it makes requests; nothing below it constructs headers.
# =============================================================================

# Explicit rather than implicit. urllib would use this context by default, but
# writing it out means a reviewer can SEE that certificate verification is on
# instead of having to know that it is the default.
_SSL_CONTEXT = ssl.create_default_context()

# Per-request socket timeout. Every individual call is short — we poll rather
# than holding one socket open for the length of a research run.
_SOCKET_TIMEOUT = 30.0

_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
_MAX_ATTEMPTS = 3

# Deep-research responses are large but not unbounded. This cap will never fire
# in normal operation; it exists so a misbehaving upstream cannot grow this
# process's memory without limit.
_MAX_RESPONSE_BYTES = 32 * 1024 * 1024

# Response ids come back from Perplexity and are interpolated into a URL path,
# so they are untrusted input even though the source is trusted. Constrain them.
_RESPONSE_ID_RE = re.compile(r"\A[A-Za-z0-9_-]{1,128}\Z")


class PerplexityError(Exception):
    """An upstream or transport failure, carrying a message safe to show a model.

    `message` never contains the API key, request headers, or a stack trace.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def _api_key() -> str:
    """Read the key at call time so an unset key is a tool error, not a crash."""
    key = os.environ.get("PERPLEXITY_API_KEY", "").strip()
    if not key:
        raise PerplexityError(
            "PERPLEXITY_API_KEY is not set. Add it to the 'env' block of this "
            "server's entry in your MCP client configuration."
        )
    return key


def _validate_response_id(response_id: str) -> str:
    """Reject anything that could escape the URL path it is interpolated into."""
    if not _RESPONSE_ID_RE.match(response_id):
        raise PerplexityError(
            "Malformed response_id. Expected the identifier returned by perplexity_agent."
        )
    return response_id


def _error_message(payload: dict[str, object], status: int) -> str:
    """Pull a human-readable message out of an upstream error body.

    Their OpenAPI declares error.code as a string; the live API returns an
    integer. We read only `message` and ignore `code` entirely, which sidesteps
    the disagreement.
    """
    error = payload.get("error")
    if isinstance(error, dict):
        message = error.get("message")
        if isinstance(message, str) and message:
            return message
    return f"Perplexity returned HTTP {status}."


def _read_capped(read: Callable[[int], bytes], limit: int) -> bytes:
    """Read at most `limit` bytes, detecting overflow instead of guessing at it.

    A body of exactly `limit` bytes is legitimate and must not raise, so we ask
    for one byte more than the cap: getting it back proves the real body is
    larger, without ever holding more than `limit + 1` bytes in memory to find
    that out. Callers pass a bound `.read` method rather than the response
    object itself — the only thing this needs is a callable that reads up to
    n bytes.
    """
    chunk = read(limit + 1)
    if len(chunk) > limit:
        raise PerplexityError(f"Perplexity response exceeded the {limit}-byte limit.")
    return chunk


def _request(method: str, path: str, body: dict[str, object] | None = None) -> dict[str, object]:
    """Make one API call. The single choke point for all network access.

    The Authorization header exists only inside this function. It is never
    attached to an exception, never logged, and never returned.
    """
    url = API_BASE + path
    data = json.dumps(body).encode("utf-8") if body is not None else None

    last_error = "request failed"
    for attempt in range(_MAX_ATTEMPTS):
        request = urllib.request.Request(url, data=data, method=method)  # noqa: S310
        request.add_header("Authorization", "Bearer " + _api_key())
        request.add_header("User-Agent", f"perplexity-agent-mcp/{__version__}")
        if data is not None:
            request.add_header("Content-Type", "application/json")

        try:
            with urllib.request.urlopen(  # noqa: S310
                request, timeout=_SOCKET_TIMEOUT, context=_SSL_CONTEXT
            ) as response:
                return _decode(_read_capped(response.read, _MAX_RESPONSE_BYTES))
        except urllib.error.HTTPError as exc:
            # HTTPError is itself a file-like response object wrapping a live
            # connection. Close it explicitly on every path out of this block,
            # including the re-raise below, rather than leaving it to garbage
            # collection: CPython's refcounting happens to finalize it
            # promptly, but that is an implementation detail the language does
            # not guarantee, and skipping the close emits a real ResourceWarning.
            with exc:
                payload = _safe_json(_read_capped(exc.read, _MAX_RESPONSE_BYTES))
                message = _error_message(payload, exc.code)
                if exc.code not in _RETRY_STATUSES:
                    # A bad request retried is just a slower bad request.
                    raise PerplexityError(message) from None
                last_error = message
        except (urllib.error.URLError, TimeoutError, ssl.SSLError, OSError) as exc:
            # Deliberately record only the exception TYPE. Messages from socket
            # and TLS errors can echo the request, and the request has our key.
            last_error = f"network error ({type(exc).__name__})"

        if attempt < _MAX_ATTEMPTS - 1:
            # Exponential backoff with jitter. Perplexity documents no
            # Retry-After header, and their docs prescribe exactly this.
            time.sleep(2**attempt + random.random())  # noqa: S311

    raise PerplexityError(f"{last_error} (after {_MAX_ATTEMPTS} attempts)")


def _decode(raw: bytes) -> dict[str, object]:
    payload = _safe_json(raw)
    if not isinstance(payload, dict):
        raise PerplexityError("Perplexity returned a non-object JSON response.")
    return payload


def _safe_json(raw: bytes) -> dict[str, object]:
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def main() -> int:
    """Entry point. Implemented in Task 5."""
    raise NotImplementedError


if __name__ == "__main__":
    raise SystemExit(main())
