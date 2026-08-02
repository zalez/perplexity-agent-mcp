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
import unittest.mock

import perplexity_agent_mcp as srv

from .fake_perplexity import FakePerplexity

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SERVER = REPO_ROOT / "perplexity_agent_mcp.py"
SENTINEL = "pplx-SENTINEL-must-never-appear-anywhere"
# A distinct sentinel for the stdout-rebind test below: it stands in for "a
# stray print() anywhere in the process", not for the API key, so it is kept
# textually unrelated to SENTINEL to avoid the two properties being confused
# for one another in a failure message.
PRINT_SENTINEL = "PRINT-SENTINEL-must-land-on-stderr-only"

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
PING = {"jsonrpc": "2.0", "id": 2, "method": "ping"}


def _write_unroutable_copy(directory: pathlib.Path) -> pathlib.Path:
    """A throwaway copy of the server: API_BASE repointed at an unroutable
    local address, and the retry backoff neutralised so all three real
    attempts complete in milliseconds instead of several real seconds.

    Why a patched COPY, run as its own script, rather than a
    `python -c "exec(open(...).read().replace(...))"` one-liner: that
    construction depends on the exec'd code sharing the `-c` script's
    own module namespace so `__name__ == "__main__"` still holds -- true
    today, but an incidental property of how a globals-less `exec()`
    resolves, not something worth this test depending on. Writing a real
    .py file and running it with `[sys.executable, str(path)]` is exactly
    how a script is normally invoked (matching test_mcp_protocol.py's
    `run_server` helper), so `__name__` is "__main__" for the ordinary
    reason, and there is no shell-quoting of the repo path to get wrong.

    Why _MAX_ATTEMPTS is left at its real value of 3, unpatched, rather than
    trimmed to 1 to save time: trimming it would make _request's own
    retry/backoff branch -- `if attempt < _MAX_ATTEMPTS - 1: ...` --
    structurally unreachable, because with _MAX_ATTEMPTS=1 that condition is
    `0 < 0`, always False. A leak planted inside that branch would then be
    invisible to this test no matter what it asserts, because the mutated
    code would never run. Left at 3, attempts 0 and 1 both take that branch
    before the loop's third and final attempt raises, so a leak planted
    there sits on a path this test actually walks -- confirmed by planting
    one there and watching this test catch it.

    What IS genuinely slow, and safe to remove, is the real
    exponential-backoff time.sleep() BETWEEN retries
    (2**attempt + random.random() seconds, several real seconds total
    across two backoffs): connecting to 127.0.0.1:9 itself is fast, an
    instant ECONNREFUSED rather than a socket timeout, so patching only the
    backoff DELAY down to zero keeps every attempt and every branch
    completely real while removing only the dead time between them -- the
    Authorization header is still rebuilt from a real _api_key() call on
    every one of the three attempts, and the final exception is still the
    product of three real failed connections, not a shortcut.

    A copy rather than an in-place edit: the tracked server file must
    never be touched, even transiently, by a test run.
    """
    source = SERVER.read_text(encoding="utf-8")
    patched = source.replace(
        'API_BASE = "https://api.perplexity.ai"', 'API_BASE = "http://127.0.0.1:9"', 1
    )
    if patched == source:
        # Fails loudly rather than silently. A plain `assert` here would be
        # stripped under `python -O` -- turning "the patch stopped
        # matching" into a SILENT no-op that leaves API_BASE pointed at the
        # real Perplexity API, i.e. a live network call with a real key on
        # every future run of this test, instead of a clean failure right
        # now.
        raise AssertionError("API_BASE assignment not found; server source changed shape")

    before_backoff = patched
    patched = patched.replace("backoff = 2**attempt + random.random()", "backoff = 0.0", 1)
    if patched == before_backoff:
        raise AssertionError("backoff assignment not found; server source changed shape")

    copy_path = directory / "perplexity_agent_mcp_unroutable.py"
    copy_path.write_text(patched, encoding="utf-8")
    return copy_path


def _forbidden_stdout_writes(tree: ast.AST) -> list[str]:
    """Every statically-detectable way this source could write to stdout.

    Three patterns, each catching a different way a stdout write could be
    spelled to evade a naive name-only check:

    1. `print(...)` called directly, by name.
    2. `print` reached through attribute access, on ANY base expression --
       `sys.modules["builtins"].print(...)`, `builtins.print(...)`,
       `__builtins__.print(...)`, whatever it is spelled as. Matching only
       the final `.print` attribute, not what it hangs off of, is
       deliberate: the base expression is exactly what an evasion gets to
       choose, so there is no fixed list of "the ways to spell builtins"
       worth enumerating.
    3. `sys.stdout.write(...)` written out literally.

    What this can NEVER catch: an alias (`p = print; p(...)`, or
    `w = sys.stdout.write; w(...)`). By the time that call happens, the
    callable is just an ordinary name, indistinguishable at parse time from
    any other function call -- no static check closes that gap.
    TestKeyNeverLeaks.test_stray_print_is_redirected_to_stderr_not_the_protocol_stream
    covers it instead, by pinning the RUNTIME mechanism (the
    `sys.stdout = sys.stderr` rebind at import) that neutralises a stray
    print() -- aliased or not -- regardless of how it was spelled.
    """
    violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id == "print":
            violations.append(f"line {node.lineno}: print(...)")
        elif isinstance(func, ast.Attribute) and func.attr == "print":
            violations.append(f"line {node.lineno}: *.print(...)")
        elif (
            isinstance(func, ast.Attribute)
            and func.attr == "write"
            and isinstance(func.value, ast.Attribute)
            and func.value.attr == "stdout"
            and isinstance(func.value.value, ast.Name)
            and func.value.value.id == "sys"
        ):
            violations.append(f"line {node.lineno}: sys.stdout.write(...)")
    return violations


class TestKeyNeverLeaks(unittest.TestCase):
    def test_key_absent_from_stdout_and_stderr_on_upstream_failure(self) -> None:
        """Point a throwaway copy of the server at an unroutable address so
        the three real connection attempts it makes all genuinely fail,
        then prove the key reaches neither stream.

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
        stderr, never print() -- checked via _forbidden_stdout_writes,
        which parses the source into an AST rather than scanning for
        substrings like "print(": this module's own stdout-discipline
        comments talk ABOUT print() in prose (e.g. "a stray print(): stdout
        is reserved..."), so a plain substring scan flags those comments as
        violations of a property they are actually documenting. Walking
        the AST for real ast.Call nodes checks what the property actually
        means: no CALL that reaches stdout, prose be damned. See
        _forbidden_stdout_writes's own docstring for exactly what it does
        and does not catch, and why.

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
        violations = _forbidden_stdout_writes(tree)
        self.assertEqual(violations, [], f"stdout write(s) found: {violations}")

        for line in source.splitlines():
            if "_log(" in line or "stderr.write(" in line.lower():
                self.assertNotIn("_api_key", line)
                self.assertNotIn("Authorization", line)

    def test_stray_print_during_request_handling_lands_on_stderr(self) -> None:
        """Pin the actual defence by its runtime effect, rather than relying
        solely on the static check above, which an aliased call
        (`p = print; p(...)`) can evade -- see _forbidden_stdout_writes's own
        docstring for why that gap can never close statically.

        `main()` claims the real stdout for the protocol and points
        sys.stdout at stderr, so any print() anywhere in the process after
        that lands on stderr no matter how it was written.

        The print is injected into a REQUEST HANDLER rather than at import
        time, because that is where a stray debugging print actually gets
        written. (It also matches where the guarantee now begins: claiming
        stdout moved from import time into `main()` so that merely importing
        this module no longer redirects the output of the importing program
        -- see _claim_stdout. The remaining window, between `import` and the
        `main()` call, is a single statement in the shipped entry points and
        cannot print.)

        Proves both halves at once: the print()'d text reaches stderr
        (so the call genuinely ran and was genuinely redirected, rather than
        merely "not appearing on stdout" for some unrelated reason), and
        stdout carries nothing but valid JSON-RPC.
        """
        driver = (
            "import sys\n"
            f"sys.path.insert(0, {str(REPO_ROOT)!r})\n"
            "import perplexity_agent_mcp as server\n"
            # Stand in for a stray debugging print left inside handler code.
            #
            # Goes in via _replace rather than by assigning a bare function:
            # HANDLERS maps a method to a _Method record (handler, era,
            # cacheable), not to a callable. Assigning a plain function here
            # instead fails in a memorably unhelpful way -- dispatch raises
            # AttributeError, the broad except turns it into a -32603, the
            # frame count and both frame ids still match, and the only
            # assertion that fails is the sentinel one below, whose message
            # says this test proved nothing.
            "_real = server.HANDLERS['ping']\n"
            "def _noisy(params):\n"
            f"    print({PRINT_SENTINEL!r}, flush=True)\n"
            "    return _real.handler(params)\n"
            "server.HANDLERS['ping'] = _real._replace(handler=_noisy)\n"
            "raise SystemExit(server.main())\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", driver],
            input=json.dumps(INIT) + "\n" + json.dumps(PING) + "\n",
            capture_output=True,
            text=True,
            timeout=30,
            env={"PATH": "/usr/bin:/bin"},
            check=False,
        )
        self.assertEqual(proc.returncode, 0, f"driver crashed; stderr: {proc.stderr}")
        self.assertIn(
            PRINT_SENTINEL,
            proc.stderr,
            "the print() call never reached stderr -- this test proved nothing",
        )
        self.assertNotIn(PRINT_SENTINEL, proc.stdout)

        lines = [line for line in proc.stdout.splitlines() if line.strip()]
        self.assertEqual(len(lines), 2, f"expected exactly two frames, got: {lines}")
        frames = [json.loads(line) for line in lines]
        self.assertEqual(frames[0].get("id"), 1)
        self.assertEqual(frames[1].get("id"), 2)

    def test_importing_the_module_does_not_touch_process_stdout(self) -> None:
        """Importing must have no side effect on the importing program.

        Claiming stdout used to happen at import time, which silently
        redirected the output of ANY program that imported this module --
        including the `llm` plugin adapter that reuses the Perplexity client
        in here, where it would have sent every one of that tool's models'
        output to stderr. Claiming a process-wide resource is a decision for
        whoever runs the server, not for whoever imports it.
        """
        driver = (
            "import sys\n"
            f"sys.path.insert(0, {str(REPO_ROOT)!r})\n"
            "import perplexity_agent_mcp\n"
            "print('STDOUT_INTACT' if sys.stdout is sys.__stdout__ else 'STDOUT_HIJACKED')\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", driver],
            capture_output=True,
            text=True,
            timeout=30,
            env={"PATH": "/usr/bin:/bin"},
            check=False,
        )
        self.assertEqual(proc.returncode, 0, f"driver crashed; stderr: {proc.stderr}")
        self.assertIn("STDOUT_INTACT", proc.stdout)


class TestKeyStaysOutOfUpstreamRequests(unittest.TestCase):
    """None of TestKeyNeverLeaks above inspects what actually goes out over
    the wire: its process-level property never completes a real connection
    at all, and its source-level properties only ever look at source text.
    A key planted into `_request`'s own `url` construction, for instance,
    would pass every one of those tests untouched -- the only thing that
    would catch it is incidental: three unrelated exact-path assertions in
    test_perplexity_client.py (TestSubmit/TestPoll/TestCancel), protection
    that would silently vanish the moment any one of those assertions was
    loosened for an unrelated reason.

    This class owns the property directly: drive real submit/poll/cancel
    calls against the in-process fake upstream, then inspect exactly what
    FakePerplexity recorded -- method, path, body, and (now that the fake
    also records them) headers -- and assert the key appears in the
    Authorization header of every request, and nowhere else: not the path,
    not the query string, not the body, not any other header.
    """

    def setUp(self) -> None:
        self.fake = FakePerplexity()
        self._real_base = srv.API_BASE
        srv.API_BASE = self.fake.url
        self._env = unittest.mock.patch.dict("os.environ", {"PERPLEXITY_API_KEY": SENTINEL})
        self._env.start()
        self.addCleanup(self._env.stop)
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        srv.API_BASE = self._real_base
        self.fake.close()

    def test_key_appears_only_in_the_authorization_header(self) -> None:
        self.fake.script((200, {"id": "resp_1", "status": "completed", "output": []}))

        response_id = srv._submit("why?", preset="medium", recency=None, domains=None)
        srv._poll(response_id, budget=0.01)
        srv._cancel(response_id)

        self.assertGreaterEqual(
            len(self.fake.requests), 3, "submit + poll + cancel must all reach the fake"
        )
        self.assertEqual(len(self.fake.requests), len(self.fake.request_headers))

        for (method, path, body), headers in zip(
            self.fake.requests, self.fake.request_headers, strict=True
        ):
            with self.subTest(method=method, path=path):
                self.assertNotIn(SENTINEL, path, "leaked into the request path or query string")
                self._assert_absent(body)

                header_map = {name.lower(): value for name, value in headers.items()}
                for name, value in header_map.items():
                    if name != "authorization":
                        self.assertNotIn(SENTINEL, value, f"leaked via header {name!r}")

                # Positive half: the key DOES legitimately travel, exactly
                # here, turning this from a negative check into a complete
                # one -- the key must appear in Authorization AND nowhere
                # else, not merely "nowhere I happened to look".
                auth = header_map.get("authorization")
                if auth is None:
                    raise AssertionError("every upstream request must carry Authorization")
                self.assertIn(SENTINEL, auth)

    def test_redirect_never_reaches_a_second_host(self) -> None:
        """The concrete demonstration behind the redirect fix (see
        `_OPENER`'s comment in perplexity_agent_mcp.py, and `TestRedirect`
        in test_perplexity_client.py for `_request`'s own behaviour):
        urllib's default HTTPRedirectHandler carries Authorization across
        hosts UNCHANGED, even on an https -> http downgrade. A hostile or
        merely compromised api.perplexity.ai could have used nothing more
        than a 302 to steal the key from any server that called bare
        urlopen(). Stand up a second fake server -- the "attacker's" host
        -- point the first fake's Location header at it, and prove it is
        never contacted AT ALL: not with the key, not without it.

        This is the strongest form of the property: not "the key wasn't in
        what this server received" (that could just mean nobody looked)
        but "this server never received anything to look at".
        """
        redirect_target = FakePerplexity()
        self.addCleanup(redirect_target.close)
        self.fake.script_redirect(302, redirect_target.url + "/steal")

        with self.assertRaises(srv.PerplexityError) as ctx:
            srv._submit("why?", preset="medium", recency=None, domains=None)

        self.assertEqual(ctx.exception.status, 302)
        self.assertEqual(
            len(redirect_target.requests),
            0,
            "the redirect target must never receive a request, key or no key",
        )

    def _assert_absent(self, value: object) -> None:
        """Recursively confirm SENTINEL is not hiding anywhere in a JSON body."""
        if isinstance(value, str):
            self.assertNotIn(SENTINEL, value)
        elif isinstance(value, dict):
            for item in value.values():
                self._assert_absent(item)
        elif isinstance(value, list):
            for item in value:
                self._assert_absent(item)


if __name__ == "__main__":
    unittest.main()
