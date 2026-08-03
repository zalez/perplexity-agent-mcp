"""Protocol conformance, driven through a real subprocess over real pipes.

Testing the module in-process would miss exactly the bugs that matter here:
stdout contamination, framing, and EOF handling.
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import unittest
from typing import Any

# Imported only to read _MAX_LINE_CHARS below, so the oversized-line tests
# don't hardcode a second copy of the real cap. Every assertion in this file
# still drives actual behaviour through the subprocess, per the module
# docstring above -- nothing here is called in-process.
import perplexity_agent_mcp as srv

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SERVER = REPO_ROOT / "perplexity_agent_mcp.py"

INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-11-25",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "1.0"},
    },
}


def run_server(
    *messages: dict[str, Any], env: dict[str, str] | None = None
) -> list[dict[str, Any]]:
    """Pipe messages into a fresh server and collect its JSON-RPC replies.

    Typed `dict[str, Any]` rather than `dict[str, object]` — matching
    tests/fake_perplexity.py's existing convention — because these are
    loosely-shaped JSON-RPC replies that call sites index into two and three
    levels deep (`reply["result"]["serverInfo"]["name"]`); `object` would
    force an `isinstance` narrowing at every level for no safety this test
    file actually wants, since asserting on the wrong shape is precisely what
    the tests below exist to catch.
    """
    stdin = "".join(json.dumps(m) + "\n" for m in messages)
    proc = subprocess.run(
        [sys.executable, str(SERVER)],
        input=stdin,
        capture_output=True,
        text=True,
        timeout=60,
        env={"PATH": "/usr/bin:/bin", **(env or {})},
        check=False,
    )
    replies: list[dict[str, Any]] = []
    for line in proc.stdout.splitlines():
        if line.strip():
            replies.append(json.loads(line))
    return replies


class TestLifecycle(unittest.TestCase):
    def test_initialize_echoes_a_supported_version(self) -> None:
        (reply,) = run_server(INIT)
        self.assertEqual(reply["id"], 1)
        self.assertEqual(reply["result"]["protocolVersion"], "2025-11-25")
        self.assertEqual(reply["result"]["capabilities"], {"tools": {}})
        self.assertEqual(reply["result"]["serverInfo"]["name"], "perplexity-agent")
        self.assertIn("version", reply["result"]["serverInfo"])

    def test_initialize_echoes_an_older_supported_version(self) -> None:
        older = json.loads(json.dumps(INIT))
        older["params"]["protocolVersion"] = "2025-06-18"
        (reply,) = run_server(older)
        self.assertEqual(reply["result"]["protocolVersion"], "2025-06-18")

    def test_unsupported_version_returns_ours_rather_than_erroring(self) -> None:
        """The spec says MUST respond with a version we support, not an error."""
        future = json.loads(json.dumps(INIT))
        future["params"]["protocolVersion"] = "1.0.0"
        (reply,) = run_server(future)
        self.assertNotIn("error", reply)
        self.assertEqual(reply["result"]["protocolVersion"], "2025-11-25")

    def test_missing_protocol_version_is_tolerated(self) -> None:
        bare = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        (reply,) = run_server(bare)
        self.assertEqual(reply["result"]["protocolVersion"], "2025-11-25")

    def test_ping_returns_an_empty_result(self) -> None:
        replies = run_server(INIT, {"jsonrpc": "2.0", "id": "abc", "method": "ping"})
        self.assertEqual(replies[1], {"jsonrpc": "2.0", "id": "abc", "result": {}})

    def test_string_and_integer_ids_are_preserved(self) -> None:
        replies = run_server(
            {"jsonrpc": "2.0", "id": "s", "method": "ping"},
            {"jsonrpc": "2.0", "id": 7, "method": "ping"},
        )
        self.assertEqual(replies[0]["id"], "s")
        self.assertEqual(replies[1]["id"], 7)


class TestNotifications(unittest.TestCase):
    def test_initialized_notification_gets_no_reply(self) -> None:
        replies = run_server(INIT, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assertEqual(len(replies), 1, "notifications must never be answered")

    def test_notification_status_is_id_absence_not_method_naming(self) -> None:
        """A notification is defined by the ABSENCE of `id`, per JSON-RPC 2.0
        and the MCP spec — not by a `notifications/`-prefixed method name. A
        detector keyed on the method prefix instead would pass every other
        test in this file unchanged (confirmed by mutation), since none of
        them sends a non-"notifications/" method with no `id`.

        A message with a real, registered method (`ping`) but no `id` must
        still get no reply — nothing about "notifications/" naming is
        involved.
        """
        replies = run_server(INIT, {"jsonrpc": "2.0", "method": "ping"})
        self.assertEqual(len(replies), 1, "id-less messages are notifications, whatever their name")

    def test_unknown_notification_gets_no_reply(self) -> None:
        replies = run_server(INIT, {"jsonrpc": "2.0", "method": "notifications/nonsense"})
        self.assertEqual(len(replies), 1)


class TestErrors(unittest.TestCase):
    def test_unknown_method_returns_method_not_found(self) -> None:
        replies = run_server(INIT, {"jsonrpc": "2.0", "id": 2, "method": "resources/list"})
        self.assertEqual(replies[1]["error"]["code"], -32601)

    def test_unparseable_line_returns_parse_error_with_null_id(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(SERVER)],
            input="{not json\n",
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        reply = json.loads(proc.stdout.strip())
        self.assertEqual(reply["error"]["code"], -32700)
        self.assertIsNone(reply["id"])

    def test_json_array_is_invalid_since_batching_was_removed(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(SERVER)],
            input='[{"jsonrpc":"2.0","id":1,"method":"ping"}]\n',
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        reply = json.loads(proc.stdout.strip())
        self.assertEqual(reply["error"]["code"], -32600)

    def test_server_survives_a_bad_line_and_keeps_serving(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(SERVER)],
            input='garbage\n{"jsonrpc":"2.0","id":9,"method":"ping"}\n',
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        replies = [json.loads(x) for x in proc.stdout.splitlines() if x.strip()]
        self.assertEqual(replies[-1]["id"], 9)


class TestTransport(unittest.TestCase):
    def test_eof_exits_cleanly(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(SERVER)],
            input="",
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(proc.returncode, 0)

    def test_every_stdout_line_is_valid_json_with_no_embedded_newlines(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(SERVER)],
            input=json.dumps(INIT) + "\n",
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        for line in proc.stdout.splitlines():
            if line.strip():
                json.loads(line)  # raises if a frame was split across lines


class TestReadLoopSurvival(unittest.TestCase):
    """Each of these reproduces one way the read loop could die. The
    assertion that matters most in every case is the LAST one -- that a
    well-formed request sent right after the bad input is still answered --
    since that's what proves the loop survived, not merely that one error
    frame happened to look right.
    """

    def test_deeply_nested_array_yields_parse_error_and_loop_survives(self) -> None:
        """CPython's C-accelerated json.loads raises RecursionError, not
        json.JSONDecodeError, on sufficiently deep nesting. Left uncaught,
        this would kill the process outright: exit code 1, a bare traceback
        on stderr, zero stdout lines -- a ping sent right after would never
        get answered. 500,000 levels reproduces it in well under a second,
        generated in memory; no on-disk fixture needed.
        """
        deeply_nested = "[" * 500_000 + "]" * 500_000
        stdin = f'{deeply_nested}\n{{"jsonrpc":"2.0","id":9,"method":"ping"}}\n'
        proc = subprocess.run(
            [sys.executable, str(SERVER)],
            input=stdin,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        replies = [json.loads(x) for x in proc.stdout.splitlines() if x.strip()]
        self.assertEqual(proc.returncode, 0, f"server crashed; stderr: {proc.stderr}")
        self.assertEqual(replies[0]["error"]["code"], -32700)
        self.assertIsNone(replies[0]["id"])
        self.assertEqual(replies[-1], {"jsonrpc": "2.0", "id": 9, "result": {}})

    def test_invalid_utf8_yields_error_and_loop_survives_under_strict_decoding(self) -> None:
        """`for line in stdin:` decodes UTF-8 itself, outside any try/except
        this module could wrap around json.loads. Relying on that alone
        would only survive by accident of Python's UTF-8 mode currently
        defaulting stdin to errors="surrogateescape" -- an environment
        default, not a guarantee. PYTHONIOENCODING=utf-8:strict (set
        explicitly below -- a legitimate, unexotic configuration) is enough
        to turn the same input into an uncaught UnicodeDecodeError under
        that naive approach: exit code 1, zero stdout lines. main()'s
        explicit stdin.reconfigure(errors="replace") is what keeps this
        deterministic regardless of the ambient default.

        The garbage line is invalid UTF-8 standing alone, not embedded inside
        an otherwise-valid JSON string, so once errors="replace" turns it
        into U+FFFD characters it still fails to parse as JSON -- a
        deterministic -32700, regardless of which decode-error mode was
        ambient beforehand.
        """
        raw = b"\xff\xfe\xfd\n" + b'{"jsonrpc":"2.0","id":9,"method":"ping"}\n'
        proc = subprocess.run(
            [sys.executable, str(SERVER)],
            input=raw,
            capture_output=True,
            timeout=30,
            env={"PATH": "/usr/bin:/bin", "PYTHONIOENCODING": "utf-8:strict"},
            check=False,
        )
        stdout = proc.stdout.decode("utf-8")
        replies = [json.loads(x) for x in stdout.splitlines() if x.strip()]
        self.assertEqual(proc.returncode, 0, f"server crashed; stderr: {proc.stderr!r}")
        self.assertEqual(replies[0]["error"]["code"], -32700)
        self.assertEqual(replies[-1], {"jsonrpc": "2.0", "id": 9, "result": {}})

    def test_invalid_utf8_behaves_the_same_without_the_strict_override(self) -> None:
        """Same fixture, ambient decode-error mode (no PYTHONIOENCODING
        override at all). Confirms the fix does not merely paper over strict
        mode specifically: main() now always reconfigures stdin's error
        handler itself, so both configurations produce the identical
        observable outcome instead of the loop's survival depending on
        whichever default happened to be active.
        """
        raw = b"\xff\xfe\xfd\n" + b'{"jsonrpc":"2.0","id":9,"method":"ping"}\n'
        proc = subprocess.run(
            [sys.executable, str(SERVER)],
            input=raw,
            capture_output=True,
            timeout=30,
            env={"PATH": "/usr/bin:/bin"},
            check=False,
        )
        stdout = proc.stdout.decode("utf-8")
        replies = [json.loads(x) for x in stdout.splitlines() if x.strip()]
        self.assertEqual(proc.returncode, 0, f"server crashed; stderr: {proc.stderr!r}")
        self.assertEqual(replies[0]["error"]["code"], -32700)
        self.assertEqual(replies[-1], {"jsonrpc": "2.0", "id": 9, "result": {}})

    def test_oversized_line_yields_error_and_loop_survives(self) -> None:
        """A single incoming line needs its own bound, distinct from BAND
        2's _MAX_RESPONSE_BYTES for HTTP responses -- an unbounded line
        could otherwise grow this process's memory without limit. This is
        hardening, not a crash reproduction, so this test only characterises
        the guarded behaviour: the cap fires at _MAX_LINE_CHARS + 1
        characters, the rest of the line is drained rather than left to
        desynchronise the stream, and the next line is still answered.
        """
        oversized = "x" * (srv._MAX_LINE_CHARS + 1)
        stdin = f'{oversized}\n{{"jsonrpc":"2.0","id":9,"method":"ping"}}\n'
        proc = subprocess.run(
            [sys.executable, str(SERVER)],
            input=stdin,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        replies = [json.loads(x) for x in proc.stdout.splitlines() if x.strip()]
        self.assertEqual(proc.returncode, 0, f"server crashed; stderr: {proc.stderr}")
        self.assertEqual(replies[0]["error"]["code"], -32600)
        self.assertIsNone(replies[0]["id"])
        self.assertEqual(replies[-1], {"jsonrpc": "2.0", "id": 9, "result": {}})

    def test_line_of_exactly_the_cap_is_accepted(self) -> None:
        """The cap must be inclusive: exactly _MAX_LINE_CHARS characters of
        content is a legitimate (if extreme) line, not an oversized one --
        proving the boundary sits where the code claims, not off by one in
        whichever direction happens to be safe.
        """
        request = json.dumps({"jsonrpc": "2.0", "id": 9, "method": "ping"})
        line = request + " " * (srv._MAX_LINE_CHARS - len(request))
        self.assertEqual(len(line), srv._MAX_LINE_CHARS)
        proc = subprocess.run(
            [sys.executable, str(SERVER)],
            input=line + "\n",
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        replies = [json.loads(x) for x in proc.stdout.splitlines() if x.strip()]
        self.assertEqual(replies, [{"jsonrpc": "2.0", "id": 9, "result": {}}])

    def test_final_unterminated_line_of_exactly_the_cap_is_accepted(self) -> None:
        """Same boundary, but with no trailing newline at all -- true EOF
        right after exactly _MAX_LINE_CHARS characters, nothing more on
        stdin. This is the ONE case that actually distinguishes `>` from
        `>=` in the overflow check: a padded line WITH a trailing newline
        (the test above) always short-circuits on chunk.endswith("\\n")
        before the length comparison can matter, so only this unterminated
        variant proves the boundary isn't off by one.
        """
        request = json.dumps({"jsonrpc": "2.0", "id": 9, "method": "ping"})
        line = request + " " * (srv._MAX_LINE_CHARS - len(request))
        self.assertEqual(len(line), srv._MAX_LINE_CHARS)
        proc = subprocess.run(
            [sys.executable, str(SERVER)],
            input=line,  # deliberately no trailing "\n"
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        replies = [json.loads(x) for x in proc.stdout.splitlines() if x.strip()]
        self.assertEqual(replies, [{"jsonrpc": "2.0", "id": 9, "result": {}}])


class TestToolListing(unittest.TestCase):
    def test_all_three_tools_are_listed(self) -> None:
        replies = run_server(INIT, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = [t["name"] for t in replies[1]["result"]["tools"]]
        self.assertEqual(
            names, ["perplexity_agent", "perplexity_agent_result", "perplexity_agent_cancel"]
        )

    def test_schemas_are_well_formed(self) -> None:
        replies = run_server(INIT, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        for tool in replies[1]["result"]["tools"]:
            with self.subTest(tool=tool["name"]):
                self.assertEqual(tool["inputSchema"]["type"], "object")
                self.assertIn("description", tool)
                self.assertIs(tool["inputSchema"]["additionalProperties"], False)

    def test_annotations_are_honest(self) -> None:
        """Honest annotations over convenient ones. perplexity_agent creates
        durable, billable, cancellable upstream state, so it is NOT
        read-only — clients use readOnlyHint to decide whether a call needs
        the user's approval, and claiming read-only would be false.
        destructiveHint is explicitly False (it defaults to True once
        readOnlyHint is False) because this tool destroys nothing.
        perplexity_agent_result genuinely only reads, so it keeps
        readOnlyHint: true. perplexity_agent_cancel changes upstream state
        and is not idempotent, so both hints say so.
        """
        replies = run_server(INIT, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        by_name = {t["name"]: t for t in replies[1]["result"]["tools"]}

        agent = by_name["perplexity_agent"]["annotations"]
        self.assertIs(agent["readOnlyHint"], False)
        self.assertIs(agent["destructiveHint"], False)
        self.assertIs(agent["idempotentHint"], False)
        self.assertIs(agent["openWorldHint"], True)

        result = by_name["perplexity_agent_result"]["annotations"]
        self.assertIs(result["readOnlyHint"], True)

        # Cancel changes upstream state and is not idempotent — say so.
        cancel = by_name["perplexity_agent_cancel"]["annotations"]
        self.assertIs(cancel["readOnlyHint"], False)
        self.assertIs(cancel["destructiveHint"], True)
        self.assertIs(cancel["idempotentHint"], False)

    def test_wait_seconds_schema_maximum_matches_the_actual_wait_budget(self) -> None:
        """The schema must state the TRUE ceiling: a `maximum` that matches
        what `_wait_budget()` actually enforces, not just a `minimum: 0`
        with an unclamped-sounding description while the real ceiling is
        enforced silently.
        """
        replies = run_server(INIT, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        by_name = {t["name"]: t for t in replies[1]["result"]["tools"]}
        wait_seconds = by_name["perplexity_agent_result"]["inputSchema"]["properties"][
            "wait_seconds"
        ]
        self.assertEqual(wait_seconds["maximum"], srv.WAIT_SECONDS_DEFAULT)
        self.assertIn("clamp", wait_seconds["description"].lower())

    def test_wait_seconds_schema_maximum_tracks_a_custom_wait_budget(self) -> None:
        """Same schema field, a differently configured server: proves the
        maximum is genuinely COMPUTED from the environment, not a number
        that happens to equal WAIT_SECONDS_DEFAULT by coincidence — the
        budget is itself configurable via PERPLEXITY_AGENT_WAIT_SECONDS, so
        a hardcoded schema literal could silently drift from it.
        """
        replies = run_server(
            INIT,
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            env={"PERPLEXITY_AGENT_WAIT_SECONDS": "120"},
        )
        by_name = {t["name"]: t for t in replies[1]["result"]["tools"]}
        wait_seconds = by_name["perplexity_agent_result"]["inputSchema"]["properties"][
            "wait_seconds"
        ]
        self.assertEqual(wait_seconds["maximum"], 120)


class TestToolCallErrors(unittest.TestCase):
    def test_unknown_tool_is_a_protocol_error(self) -> None:
        replies = run_server(
            INIT,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "nope", "arguments": {}},
            },
        )
        self.assertEqual(replies[1]["error"]["code"], -32602)

    def test_missing_api_key_is_a_tool_error_not_a_protocol_error(self) -> None:
        replies = run_server(
            INIT,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "perplexity_agent", "arguments": {"query": "x"}},
            },
        )
        result = replies[1]["result"]
        self.assertIs(result["isError"], True)
        self.assertIn("PERPLEXITY_API_KEY", result["content"][0]["text"])
        self.assertNotIn("Traceback", result["content"][0]["text"])

    def test_bad_argument_is_a_tool_error_not_invalid_params(self) -> None:
        """SEP-1303: validation errors are isError so the model can self-correct."""
        replies = run_server(
            INIT,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "perplexity_agent", "arguments": {"query": ""}},
            },
            env={"PERPLEXITY_API_KEY": "pplx-unused"},
        )
        self.assertNotIn("error", replies[1])
        self.assertIs(replies[1]["result"]["isError"], True)

    def test_malformed_response_id_is_a_tool_error_not_a_protocol_error(self) -> None:
        """A second PerplexityError raise site (_validate_response_id, deep
        inside _poll), reached via perplexity_agent_result rather than
        perplexity_agent's _api_key() — proves handle_tools_call's
        PerplexityError catch isn't somehow specific to the one path the
        test above happens to exercise. No network access occurs: the
        malformed id is rejected before any request is made.

        Also asserts on the message text, not just isError — a broad
        `except Exception` fallback would ALSO produce isError: true for an
        uncaught PerplexityError, but with a useless generic message instead
        of the actionable one. Checking the text is what actually proves the
        specific (ToolInputError, PerplexityError) catch is doing its job,
        rather than the fallback silently doing it instead.
        """
        replies = run_server(
            INIT,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "perplexity_agent_result",
                    "arguments": {"response_id": "../etc/passwd"},
                },
            },
            env={"PERPLEXITY_API_KEY": "pplx-unused"},
        )
        result = replies[1]["result"]
        self.assertNotIn("error", replies[1])
        self.assertIs(result["isError"], True)
        self.assertIn("response_id", result["content"][0]["text"])
        self.assertNotIn("unexpectedly", result["content"][0]["text"])

    def test_unknown_argument_key_is_a_tool_error(self) -> None:
        """additionalProperties: false is declared in every schema (see
        TestToolListing.test_schemas_are_well_formed), so it must actually
        be enforced — {"query": "x", "bogus": "yes"} must not be silently
        accepted. Concretely: a model typing "domain" instead of "domains"
        must not get an UNFILTERED search silently presented as a filtered
        one. No PERPLEXITY_API_KEY is set: the rejection must happen before
        any upstream call, purely from the argument shape.
        """
        replies = run_server(
            INIT,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "perplexity_agent",
                    "arguments": {"query": "x", "domain": "nasa.gov"},
                },
            },
        )
        result = replies[1]["result"]
        self.assertNotIn("error", replies[1])
        self.assertIs(result["isError"], True)
        text = result["content"][0]["text"]
        # Split first. `assertIn("domain", text)` on the whole message would be
        # satisfied by "domains" in the ACCEPTED list, so it would pass even if
        # the offending key were never named -- proving nothing while claiming
        # to prove the thing that matters. Mutation-checked: blanking the
        # offender from the message must fail this.
        offending, _, accepted = text.partition("Accepted:")
        self.assertIn(
            "domain", offending, "the offending key must be named so the model self-corrects"
        )
        self.assertIn("domains", accepted, "the accepted key should be named too")

    def test_unknown_argument_key_is_rejected_on_a_differently_shaped_tool_too(self) -> None:
        """Proves the check is driven off EACH tool's own declared schema,
        not one hardcoded key list: perplexity_agent_cancel's schema (just
        response_id) rejects an unrelated extra key exactly like
        perplexity_agent's does above, with no per-tool code required.
        """
        replies = run_server(
            INIT,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "perplexity_agent_cancel",
                    "arguments": {"response_id": "resp_x", "bogus": True},
                },
            },
        )
        result = replies[1]["result"]
        self.assertIs(result["isError"], True)
        self.assertIn("bogus", result["content"][0]["text"])

    def test_a_neighbouring_tools_argument_name_is_rejected_not_ignored(self) -> None:
        """The third tool, and the one where the realistic typo is not a typo
        at all but a name borrowed from the tool next door.

        `perplexity_agent` takes `wait` (a boolean). `perplexity_agent_result`
        takes `wait_seconds` (an integer). A model that has just called the
        first with `wait: false` and is now collecting the answer has every
        reason to reach for `wait` again — the two tools are used in sequence,
        by the same caller, seconds apart.

        Silently accepting it is the bad outcome, and a quiet one: the caller
        believes it asked to wait, the server applies its default instead, and
        nothing anywhere reports a disagreement. That is the same shape as the
        `domain`/`domains` case above, which is why this is worth pinning
        per-tool rather than trusting the shared implementation.

        No PERPLEXITY_API_KEY is set, so the rejection must come from the
        argument shape alone, before any upstream call.
        """
        replies = run_server(
            INIT,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "perplexity_agent_result",
                    "arguments": {"response_id": "resp_x", "wait": False},
                },
            },
        )
        result = replies[1]["result"]
        self.assertNotIn("error", replies[1])
        self.assertIs(result["isError"], True)
        text = result["content"][0]["text"]
        # Split on "Accepted:" for the same reason as the test above, and with
        # more force here: "wait" is a literal prefix of "wait_seconds", so
        # asserting it against the whole message would be satisfied by the
        # accepted list alone -- the one key that is guaranteed to be there.
        offending, _, accepted = text.partition("Accepted:")
        self.assertIn(
            "wait", offending, "the offending key must be named so the model self-corrects"
        )
        self.assertIn(
            "wait_seconds", accepted, "and the key it should have used must be named alongside it"
        )


if __name__ == "__main__":
    unittest.main()
