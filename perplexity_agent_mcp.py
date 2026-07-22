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
if sys.version_info < (3, 10):  # pragma: no cover - version-dependent
    sys.stderr.write(
        "perplexity-agent-mcp requires Python 3.10 or newer; "
        f"this is {sys.version.split()[0]}.\n"
    )
    raise SystemExit(1)

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


def main() -> int:
    """Entry point. Implemented in Task 6."""
    raise NotImplementedError


if __name__ == "__main__":
    raise SystemExit(main())
