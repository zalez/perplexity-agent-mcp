"""Conformance for MCP `2026-07-28` — the stateless, handshake-free revision.

Sibling of test_mcp_protocol.py, which owns the legacy (`initialize`) era, and
driven the same way: a real subprocess over real pipes, reusing that file's
`run_server` rather than growing a second copy.

**The trap this file exists inside.** Nearly every test next door opens with
`run_server(INIT, ...)` and then reads `replies[1]`, because reply 0 is the
handshake. There is no handshake here. Modern tests send no `initialize` and
read `replies[0]`, and an off-by-one produces an IndexError that reads like a
server crash rather than a test bug.

What this file deliberately does NOT cover: that legacy behaviour is unchanged.
That is test_mcp_protocol.py's job, and it does it with exact-equality
assertions that would catch any leakage from here — with one exception, the
cross-era isolation test below, which needs both eras in one process and so
cannot live on either side alone.
"""

from __future__ import annotations

import json
import unittest

import perplexity_agent_mcp as srv

from .test_mcp_protocol import INIT, run_server

# The `_meta` a well-formed modern request carries. Both keys are REQUIRED by
# the spec on every request; `clientInfo` is optional and deliberately absent
# here, so its absence is exercised by every test in the file.
MODERN_META: dict[str, object] = {
    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
    "io.modelcontextprotocol/clientCapabilities": {},
}


def modern(method: str, request_id: object = 1, **params: object) -> dict[str, object]:
    """A modern request: `_meta` plus whatever params the method needs."""
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": method,
        "params": {"_meta": dict(MODERN_META), **params},
    }


def legacy(method: str, request_id: object = 1, **params: object) -> dict[str, object]:
    """A legacy request: no `_meta`, which is the entire distinguishing mark."""
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}


class TestEraDetection(unittest.TestCase):
    """The same method, both ways, from what the request itself carries."""

    def test_meta_protocol_version_is_what_selects_the_modern_era(self) -> None:
        modern_reply, legacy_reply = run_server(modern("tools/list", 1), legacy("tools/list", 2))
        self.assertEqual(modern_reply["result"]["resultType"], "complete")
        self.assertNotIn("resultType", legacy_reply["result"])

    def test_a_request_with_no_params_at_all_is_legacy(self) -> None:
        """The absence of `params` must not be mistaken for a malformed modern
        request. dispatch() coerces a missing or non-object `params` to `{}`,
        which carries no `_meta`, which is legacy — the same answer a client
        that predates the modern revision has always got."""
        (reply,) = run_server({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        self.assertNotIn("resultType", reply["result"])

    def test_client_capabilities_alone_does_not_make_a_request_modern(self) -> None:
        """The documented honest limit of a structural detector, pinned so it
        stays a decision rather than becoming a surprise: only
        `protocolVersion` selects the era. A request carrying the other
        protocol key but not that one is indistinguishable from a legacy
        request and is served as one, rather than being rejected."""
        (reply,) = run_server(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/list",
                "params": {"_meta": {"io.modelcontextprotocol/clientCapabilities": {}}},
            }
        )
        self.assertNotIn("error", reply)
        self.assertNotIn("resultType", reply["result"])


class TestServerDiscover(unittest.TestCase):
    """`server/discover` is mandatory for servers, and on stdio it doubles as
    the backward-compatibility probe a dual-era client sends first."""

    def test_discover_answers_a_bare_probe_that_carries_no_meta(self) -> None:
        """The single most consequential behaviour in this file.

        A dual-era client probes with `server/discover` and reads the answer to
        decide what we are. Anything that is not a recognised modern reply --
        including the -32601 a strict era check would produce -- tells it
        "legacy server", and per the spec it then caches that verdict for the
        lifetime of the process and never sends a modern request again. One
        strict-looking line here would silently cost the entire modern era.
        """
        (reply,) = run_server({"jsonrpc": "2.0", "id": 1, "method": "server/discover"})
        self.assertNotIn("error", reply)
        self.assertEqual(reply["result"]["resultType"], "complete")
        self.assertIn("2026-07-28", reply["result"]["supportedVersions"])

    def test_discover_carries_every_field_the_result_type_requires(self) -> None:
        (reply,) = run_server(modern("server/discover"))
        result = reply["result"]
        for field in ("resultType", "supportedVersions", "capabilities", "ttlMs", "cacheScope"):
            with self.subTest(field=field):
                self.assertIn(field, result)
        self.assertEqual(result["capabilities"], {"tools": {}})
        info = result["_meta"]["io.modelcontextprotocol/serverInfo"]
        self.assertEqual(info["name"], "perplexity-agent")

    def test_supported_versions_leads_with_the_newest_revision(self) -> None:
        """The client picks from this list in order, so the order is a
        preference, not an accident."""
        (reply,) = run_server(modern("server/discover"))
        versions = reply["result"]["supportedVersions"]
        self.assertEqual(versions[0], "2026-07-28")
        self.assertEqual(versions, list(srv.ADVERTISED_PROTOCOL_VERSIONS))

    def test_supported_versions_order_survives_a_different_hash_seed(self) -> None:
        """Guard against rebuilding this list from a set.

        `SUPPORTED_PROTOCOL_VERSIONS` is a frozenset, and deriving the
        advertised order from it would reorder the output per PYTHONHASHSEED --
        a flake that appears in roughly one CI run in three and never
        reproduces locally. Two seeds, byte-identical answers.
        """
        orders = []
        for seed in ("0", "1", "3", "12345"):
            (reply,) = run_server(modern("server/discover"), env={"PYTHONHASHSEED": seed})
            orders.append(reply["result"]["supportedVersions"])
        for seed_order in orders[1:]:
            self.assertEqual(seed_order, orders[0])

    def test_instructions_describe_the_lifecycle_rather_than_the_tools(self) -> None:
        """The spec asks `instructions` for guidance that is NOT already in the
        tool descriptions. The async submit/poll/cancel lifecycle spans all
        three tools and is in none of them individually."""
        (reply,) = run_server(modern("server/discover"))
        instructions = reply["result"]["instructions"]
        for name in ("perplexity_agent_result", "perplexity_agent_cancel"):
            with self.subTest(tool=name):
                self.assertIn(name, instructions)


class TestVersionNegotiation(unittest.TestCase):
    """-32022, and the carve-out it lives inside."""

    def test_an_unknown_version_returns_32022_with_a_retryable_payload(self) -> None:
        (reply,) = run_server(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/list",
                "params": {
                    "_meta": {
                        "io.modelcontextprotocol/protocolVersion": "1999-01-01",
                        "io.modelcontextprotocol/clientCapabilities": {},
                    }
                },
            }
        )
        self.assertEqual(reply["error"]["code"], -32022)
        self.assertEqual(reply["error"]["data"]["requested"], "1999-01-01")
        self.assertEqual(
            reply["error"]["data"]["supported"], list(srv.ADVERTISED_PROTOCOL_VERSIONS)
        )

    def test_every_advertised_version_is_actually_accepted(self) -> None:
        """Advertising a version we then refuse would send a client into a
        retry loop against a list that cannot satisfy it."""
        for version in srv.ADVERTISED_PROTOCOL_VERSIONS:
            with self.subTest(version=version):
                (reply,) = run_server(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/list",
                        "params": {
                            "_meta": {
                                "io.modelcontextprotocol/protocolVersion": version,
                                "io.modelcontextprotocol/clientCapabilities": {},
                            }
                        },
                    }
                )
                self.assertNotIn("error", reply)

    def test_a_non_string_version_is_refused_rather_than_coerced(self) -> None:
        (reply,) = run_server(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/list",
                "params": {
                    "_meta": {
                        "io.modelcontextprotocol/protocolVersion": 20260728,
                        "io.modelcontextprotocol/clientCapabilities": {},
                    }
                },
            }
        )
        self.assertEqual(reply["error"]["code"], -32022)

    def test_legacy_initialize_still_never_errors_on_an_unknown_version(self) -> None:
        """The two negotiation rules are opposites, and both are required.

        `initialize` MUST answer with a version it supports rather than an
        error; the modern path MUST return -32022. They coexist because they
        govern different code paths. This asserts the legacy half from inside
        the modern test file, because unifying the two is exactly the tidy-up
        someone will eventually attempt.
        """
        (reply,) = run_server(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "1999-01-01", "capabilities": {}},
            }
        )
        self.assertNotIn("error", reply)
        self.assertEqual(reply["result"]["protocolVersion"], "2025-11-25")


class TestRequiredMetaFields(unittest.TestCase):
    """Spec-strict: a modern request missing a required `_meta` field is
    malformed and MUST be rejected with -32602."""

    def test_missing_client_capabilities_is_rejected_and_names_the_field(self) -> None:
        (reply,) = run_server(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/list",
                "params": {"_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}},
            }
        )
        self.assertEqual(reply["error"]["code"], -32602)
        self.assertEqual(
            reply["error"]["data"]["missing"],
            ["io.modelcontextprotocol/clientCapabilities"],
        )

    def test_client_capabilities_must_be_an_object_not_merely_present(self) -> None:
        (reply,) = run_server(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/list",
                "params": {
                    "_meta": {
                        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                        "io.modelcontextprotocol/clientCapabilities": "none",
                    }
                },
            }
        )
        self.assertEqual(reply["error"]["code"], -32602)

    def test_protocol_validation_precedes_tool_validation(self) -> None:
        """Ordering, pinned as behaviour.

        A malformed modern `tools/call` naming a tool that does not exist has
        two things wrong with it. If the `_meta` check ran inside
        handle_tools_call it would sit after the unknown-tool check, and this
        would come back as "Unknown tool" -- the same code, an unrelated
        message, and a client with no idea its `_meta` was at fault.
        """
        (reply,) = run_server(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28"},
                    "name": "no_such_tool",
                    "arguments": {},
                },
            }
        )
        self.assertEqual(reply["error"]["code"], -32602)
        self.assertIn(
            "io.modelcontextprotocol/clientCapabilities", reply["error"]["data"]["missing"]
        )
        self.assertNotIn("Unknown tool", reply["error"]["message"])

    def test_unknown_meta_keys_are_tolerated_not_rejected(self) -> None:
        """A required-fields check, never a deny-unknown one. `_meta` is an
        extension point; rejecting keys we do not recognise would reject
        `progressToken` and everything the spec adds after this was written."""
        (reply,) = run_server(
            modern("tools/list", 1, _unused=None)
            | {
                "params": {
                    "_meta": {
                        **MODERN_META,
                        "progressToken": "tok-1",
                        "com.example/whatever": {"anything": True},
                    }
                }
            }
        )
        self.assertNotIn("error", reply)


class TestResultShaping(unittest.TestCase):
    """What the modern revision requires of every result, and of only some."""

    def test_every_modern_result_identifies_the_server_in_its_meta(self) -> None:
        for method in ("ping", "tools/list", "server/discover"):
            with self.subTest(method=method):
                (reply,) = run_server(modern(method))
                info = reply["result"]["_meta"]["io.modelcontextprotocol/serverInfo"]
                self.assertEqual(info["name"], "perplexity-agent")
                self.assertEqual(info["version"], srv.__version__)

    def test_cacheable_results_carry_the_cache_hints_and_others_do_not(self) -> None:
        """`tools/list` and `server/discover` are CacheableResults;
        `CallToolResult` extends plain `Result`. Attaching a TTL to a research
        answer would invite a client to serve a stale one."""
        for method, expected in (("tools/list", True), ("server/discover", True)):
            with self.subTest(method=method):
                (reply,) = run_server(modern(method))
                self.assertEqual("ttlMs" in reply["result"], expected)

        (call,) = run_server(
            modern("tools/call", 1, name="perplexity_agent", arguments={"query": ""})
        )
        self.assertNotIn("ttlMs", call["result"])
        self.assertNotIn("cacheScope", call["result"])

    def test_the_tool_list_is_scoped_private_because_it_depends_on_the_environment(
        self,
    ) -> None:
        """`wait_seconds.maximum` comes from PERPLEXITY_AGENT_WAIT_SECONDS, so
        this answer is true for one process's configuration and not another's.
        A shared cache must not hand it to a differently-configured consumer."""
        (reply,) = run_server(modern("tools/list"))
        self.assertEqual(reply["result"]["cacheScope"], "private")

    def test_an_error_result_is_still_a_complete_result(self) -> None:
        """`isError: true` is a successful protocol exchange reporting a failed
        tool. `input_required` means something else entirely — an elicitation
        this server never performs."""
        (reply,) = run_server(
            modern("tools/call", 1, name="perplexity_agent", arguments={"query": ""})
        )
        self.assertEqual(reply["result"]["resultType"], "complete")
        self.assertTrue(reply["result"]["isError"])

    def test_the_clients_own_meta_is_not_echoed_back_in_the_result(self) -> None:
        """The result's `_meta` is built fresh, not copied from the request.
        Reflecting the client's `progressToken` back at it would be noise at
        best, and mutating the request dict at worst."""
        (reply,) = run_server(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/list",
                "params": {"_meta": {**MODERN_META, "progressToken": "tok-1"}},
            }
        )
        self.assertNotIn("progressToken", reply["result"]["_meta"])
        self.assertNotIn("io.modelcontextprotocol/protocolVersion", reply["result"]["_meta"])

    def test_protocol_errors_are_not_decorated_as_results(self) -> None:
        """-32022 and friends are error frames. An error frame has no
        `resultType` and no `result` at all."""
        (reply,) = run_server(modern("no/such/method"))
        self.assertEqual(reply["error"]["code"], -32601)
        self.assertNotIn("result", reply)


class TestEraPrecedence(unittest.TestCase):
    """The two methods whose era is fixed by the method itself, not by `_meta`."""

    def test_initialize_carrying_modern_meta_is_still_a_legacy_result(self) -> None:
        """The chimera test.

        A purely structural rule would classify this as modern and bolt
        `resultType` and `serverInfo` onto an InitializeResult, producing a
        frame that belongs to neither revision. `initialize` does not exist in
        the modern era, so there is no such thing as a modern one.
        """
        (reply,) = run_server(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "_meta": dict(MODERN_META),
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                },
            }
        )
        result = reply["result"]
        self.assertEqual(result["protocolVersion"], "2025-11-25")
        for field in ("resultType", "_meta", "ttlMs", "cacheScope"):
            with self.subTest(field=field):
                self.assertNotIn(field, result)

    def test_ping_is_answered_in_both_eras(self) -> None:
        """Removed from the modern revision, but nothing forbids answering it,
        and refusing it can only break a client using it as a liveness check."""
        modern_reply, legacy_reply = run_server(modern("ping", 1), legacy("ping", 2))
        self.assertNotIn("error", modern_reply)
        self.assertNotIn("error", legacy_reply)
        self.assertEqual(legacy_reply["result"], {})


class TestCrossEraIsolation(unittest.TestCase):
    """Both eras in one process, which is the only place this can be seen."""

    def test_a_modern_request_does_not_contaminate_a_later_legacy_one(self) -> None:
        """Regression guard for shared module-level result data.

        handle_tools_list returns the same TOOL_SCHEMAS object every call. If
        the modern shaping step mutated the handler's return value in place
        rather than building a new dict, the first modern request would leave
        `resultType` behind permanently, and every later request -- including
        legacy ones, including ones from a completely different client --
        would carry it. Cross-request state, in a server whose entire claim is
        having none. Order matters: modern first, legacy second.
        """
        first, second, third = run_server(
            modern("tools/list", 1), legacy("tools/list", 2), modern("tools/list", 3)
        )
        self.assertEqual(first["result"]["resultType"], "complete")
        self.assertNotIn("resultType", second["result"])
        self.assertNotIn("ttlMs", second["result"])
        self.assertEqual(third["result"]["resultType"], "complete")

    def test_shaping_leaves_the_handlers_own_result_object_untouched(self) -> None:
        """The direct form of the guarantee above, asserted in-process.

        The wire test cannot currently fail: every handler today returns a
        fresh outer dict, so mutating it in place happens to be harmless.
        That makes this the test that actually bites — it fails the moment
        _shape_modern stops copying, without waiting for a future handler to
        return module-level data and turn a latent bug into a live one.
        """
        handler_result: dict[str, object] = {"tools": []}
        shaped = srv._shape_modern(handler_result, cacheable=True)
        self.assertEqual(handler_result, {"tools": []})
        self.assertIn("resultType", shaped)
        self.assertIn("ttlMs", shaped)

    def test_a_legacy_handshake_does_not_bind_later_modern_requests(self) -> None:
        """A dual-era server serves both concurrently. Having answered
        `initialize` must not put the process into a legacy "mode" — there are
        no modes, only per-request classification."""
        _init, reply = run_server(INIT, modern("tools/list", 2))
        self.assertEqual(reply["result"]["resultType"], "complete")


class TestModernNotifications(unittest.TestCase):
    """A notification gets no reply. Not even a malformed one."""

    def test_a_malformed_modern_notification_is_answered_with_silence(self) -> None:
        """The `id: null` trap.

        Era classification needs `params`, which dispatch reads late — after
        the notification early-returns. Hoisting the check to the top of the
        function makes this request produce a -32602 carrying `"id": null`:
        a reply to a notification, which the spec forbids, in a frame that
        looks like a parse error to the client. The trailing ping proves the
        loop is alive and that the silence was deliberate.
        """
        replies = run_server(
            {
                "jsonrpc": "2.0",
                "method": "tools/list",
                "params": {"_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}},
            },
            legacy("ping", 9),
        )
        self.assertEqual(replies, [{"jsonrpc": "2.0", "id": 9, "result": {}}])

    def test_a_notification_naming_an_unknown_version_is_also_silent(self) -> None:
        replies = run_server(
            {
                "jsonrpc": "2.0",
                "method": "tools/list",
                "params": {
                    "_meta": {
                        "io.modelcontextprotocol/protocolVersion": "1999-01-01",
                        "io.modelcontextprotocol/clientCapabilities": {},
                    }
                },
            },
            legacy("ping", 9),
        )
        self.assertEqual(replies, [{"jsonrpc": "2.0", "id": 9, "result": {}}])


class TestProgressTokenCoexistence(unittest.TestCase):
    """`progressToken` is spelled bare, in the same `_meta` that now carries
    the prefixed protocol keys. In-process, because the emitter is what is
    under test — the wire path is covered in test_async_lifecycle.py."""

    def test_the_token_is_still_found_beside_the_protocol_keys(self) -> None:
        notify = srv._progress_notifier({"_meta": {**MODERN_META, "progressToken": "tok-1"}})
        self.assertIsNotNone(notify)

    def test_a_modern_request_without_a_token_still_emits_nothing(self) -> None:
        self.assertIsNone(srv._progress_notifier({"_meta": dict(MODERN_META)}))


class TestConstantsGuards(unittest.TestCase):
    """Guard the guards. These assert properties of the constants themselves,
    which no wire test can reach once they are wrong in a consistent way."""

    def test_the_modern_revision_is_not_legacy_negotiable(self) -> None:
        """If 2026-07-28 ever enters SUPPORTED_PROTOCOL_VERSIONS, a legacy
        `initialize` asking for it gets it echoed back — the server answering a
        method the modern revision does not have by claiming to speak the
        modern revision. Deriving the frozenset from the tuple's tail makes it
        impossible; this asserts the derivation was not undone."""
        self.assertNotIn(srv.MODERN_PROTOCOL_VERSION, srv.SUPPORTED_PROTOCOL_VERSIONS)

    def test_the_advertised_list_is_ordered_and_not_a_set(self) -> None:
        self.assertIsInstance(srv.ADVERTISED_PROTOCOL_VERSIONS, tuple)
        self.assertEqual(srv.ADVERTISED_PROTOCOL_VERSIONS[0], srv.MODERN_PROTOCOL_VERSION)

    def test_every_legacy_negotiable_version_is_also_advertised(self) -> None:
        self.assertEqual(
            set(srv.ADVERTISED_PROTOCOL_VERSIONS),
            srv.SUPPORTED_PROTOCOL_VERSIONS | {srv.MODERN_PROTOCOL_VERSION},
        )

    def test_every_handler_declares_an_era_the_dispatcher_understands(self) -> None:
        """The point of making HANDLERS entries records is that a new method
        cannot be added without answering this. A typo'd era string would
        otherwise fall through _request_era and be returned verbatim."""
        for name, entry in srv.HANDLERS.items():
            with self.subTest(method=name):
                self.assertIn(entry.era, (srv._ERA_LEGACY, srv._ERA_MODERN, srv._ERA_BOTH))


class TestWireBytes(unittest.TestCase):
    """One end-to-end frame, read as bytes, because every other assertion here
    goes through json.loads and would not notice the stream itself breaking."""

    def test_a_modern_exchange_is_one_valid_json_object_per_line(self) -> None:
        replies = run_server(modern("server/discover", 1), modern("tools/list", 2))
        self.assertEqual(len(replies), 2)
        for reply in replies:
            with self.subTest(reply_id=reply.get("id")):
                self.assertEqual(reply["jsonrpc"], "2.0")
                self.assertNotIn("\n", json.dumps(reply))


if __name__ == "__main__":
    unittest.main()
