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
        """Not in the brief verbatim — added because the brief's own prose flags this
        exact confusion ("detect by absent `id`, not by a `notifications/` prefix")
        and the given suite doesn't actually discriminate the two implementations:
        a detector keyed on method-prefix instead of `id`-absence passes all 14
        brief tests unchanged (verified by mutation before adding this).

        A message with a real, registered method (`ping`) but no `id` must still
        get no reply — nothing about "notifications/" naming is involved.
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


if __name__ == "__main__":
    unittest.main()
