"""The API key must never escape. Asserted, not assumed."""

from __future__ import annotations

import ast
import json
import pathlib
import re
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SERVER = REPO_ROOT / "perplexity_agent_mcp.py"
SENTINEL = "pplx-SENTINEL-must-never-appear-anywhere"

INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-11-25",
        "capabilities": {},
        "clientInfo": {"name": "t", "version": "1"},
    },
}


def _write_unroutable_copy(directory: pathlib.Path) -> pathlib.Path:
    """A throwaway copy of the server: API_BASE repointed at an unroutable
    local address, and retries trimmed from 3 to 1.

    Why a patched COPY, run as its own script, rather than the plan's
    original `python -c "exec(open(...).read().replace(...))"` one-liner:
    that construction depends on the exec'd code sharing the `-c` script's
    own module namespace so `__name__ == "__main__"` still holds -- true
    today, but an incidental property of how a globals-less `exec()`
    resolves, not something worth this test depending on. Writing a real
    .py file and running it with `[sys.executable, str(path)]` is exactly
    how a script is normally invoked (matching test_mcp_protocol.py's
    `run_server` helper), so `__name__` is "__main__" for the ordinary
    reason, and there is no shell-quoting of the repo path to get wrong.

    Why _MAX_ATTEMPTS is also trimmed here: connecting to 127.0.0.1:9
    (nothing listens there) fails instantly with ECONNREFUSED, not a
    socket timeout, so that part is already fast. What is NOT fast is
    _request's real exponential-backoff time.sleep() BETWEEN retries --
    left at the real value of 3, this one test costs several real seconds
    for a code path that adds nothing to the property being proved: the
    Authorization header is rebuilt from the same _api_key() on every
    attempt, so attempt 1 alone already exercises the exact place a leak
    would happen. The upstream call this test makes is still completely
    real -- a real socket, a real connection refusal, a real
    PerplexityError -- only the redundant extra attempts (and their sleeps)
    are removed, keeping the whole suite comfortably under its time budget.

    A copy rather than an in-place edit: the tracked server file must
    never be touched, even transiently, by a test run.
    """
    source = SERVER.read_text(encoding="utf-8")
    patched = source.replace(
        'API_BASE = "https://api.perplexity.ai"', 'API_BASE = "http://127.0.0.1:9"', 1
    )
    # Fails loudly rather than silently: if this ever stops matching (the
    # server's source shape changed), the fallback would otherwise be a
    # SILENT no-op patch that leaves API_BASE pointed at the real
    # Perplexity API -- turning this security test into a live network
    # call on every run instead of failing the way a broken assumption
    # should.
    assert patched != source, "API_BASE assignment not found; server source changed shape"
    patched = patched.replace("_MAX_ATTEMPTS = 3", "_MAX_ATTEMPTS = 1", 1)
    assert "_MAX_ATTEMPTS = 1" in patched, "_MAX_ATTEMPTS assignment not found; shape changed"

    copy_path = directory / "perplexity_agent_mcp_unroutable.py"
    copy_path.write_text(patched, encoding="utf-8")
    return copy_path


class TestKeyNeverLeaks(unittest.TestCase):
    def test_key_absent_from_stdout_and_stderr_on_upstream_failure(self) -> None:
        """Point a throwaway copy of the server at an unroutable address so
        the one upstream request it makes genuinely fails, then prove the
        key reaches neither stream.

        The shipped server deliberately has no environment-variable
        base-URL override -- that would itself be a key-exfiltration
        vector -- so redirecting it at all requires patching a copy; see
        _write_unroutable_copy for why a copy, not the server file itself.
        """
        call = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "perplexity_agent", "arguments": {"query": "x", "wait": False}},
        }
        stdin = json.dumps(INIT) + "\n" + json.dumps(call) + "\n"
        with tempfile.TemporaryDirectory() as tmp:
            unroutable_server = _write_unroutable_copy(pathlib.Path(tmp))
            proc = subprocess.run(
                [sys.executable, str(unroutable_server)],
                input=stdin,
                capture_output=True,
                text=True,
                timeout=30,
                env={"PATH": "/usr/bin:/bin", "PERPLEXITY_API_KEY": SENTINEL},
                check=False,
            )
        self.assertEqual(proc.returncode, 0, f"server crashed; stderr: {proc.stderr}")
        self.assertNotIn(SENTINEL, proc.stdout)
        self.assertNotIn(SENTINEL, proc.stderr)
        self.assertNotIn("Traceback", proc.stdout)

    def test_no_key_pattern_committed_anywhere_in_the_tree(self) -> None:
        pattern = re.compile(r"pplx-[A-Za-z0-9]{20,}")
        tracked = subprocess.run(
            ["git", "ls-files"],  # noqa: S607 -- must resolve via PATH, like the dev's own git
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()
        for name in tracked:
            path = REPO_ROOT / name
            if not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            with self.subTest(file=name):
                self.assertIsNone(pattern.search(text), f"possible API key in {name}")

    def test_source_never_prints_the_key(self) -> None:
        """stdout belongs to the protocol; diagnostics go through _log() to
        stderr, never print().

        The print() check parses the source into an AST rather than
        scanning for the substring "print(" -- this module's own
        stdout-discipline comments talk ABOUT print() in prose (e.g. "a
        stray print(): stdout is reserved..."), so a plain substring scan
        flags those comments as violations of a property they are
        actually documenting. Walking the AST for real ast.Call nodes
        checks what the property actually means: no CALL to print, prose
        be damned.

        Second half: no line that writes to stderr may interpolate the key
        or the Authorization header into its message. Matched against
        "stderr.write(" case-INsensitively: the real call site, _log's own
        write, is `_STDERR.write(...)` (the module's "stdout discipline"
        preamble aliases the real stderr to _STDERR at import time,
        precisely so a stray print() lands there harmlessly instead of
        corrupting the protocol stream on stdout) -- a case-sensitive
        match against lowercase "stderr.write" would silently never
        inspect that line at all.
        """
        source = SERVER.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(SERVER))
        print_calls = [
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "print"
        ]
        self.assertEqual(print_calls, [], f"print() call(s) at line(s) {print_calls}; use _log()")

        for line in source.splitlines():
            if "_log(" in line or "stderr.write(" in line.lower():
                self.assertNotIn("_api_key", line)
                self.assertNotIn("Authorization", line)


if __name__ == "__main__":
    unittest.main()
