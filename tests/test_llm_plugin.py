"""Tests for the optional `llm` adapter in `perplexity_agent_llm.py`.

Skipped entirely when `llm` is not installed, which is the normal state for
anyone using only the MCP server — the adapter is an opt-in extra and its
absence must never fail the suite.

The argument translation is tested in-process against the fake upstream
rather than by driving the real `llm` CLI: every one of these cases is a
rejection that should cost nothing, and a test that reaches the real API to
prove an argument is invalid would be both slow and billable.
"""

from __future__ import annotations

import unittest
import unittest.mock
from typing import ClassVar

import perplexity_agent_mcp as core

from .fake_perplexity import FakePerplexity

try:
    import llm

    import perplexity_agent_llm as adapter

    HAVE_LLM = True
except ImportError:  # pragma: no cover - depends on the environment
    HAVE_LLM = False


@unittest.skipUnless(HAVE_LLM, "the `llm` extra is not installed")
class TestArgumentTranslation(unittest.TestCase):
    """`llm` options are flat scalars; the core wants typed arguments."""

    def test_domains_split_on_commas(self) -> None:
        self.assertEqual(
            adapter._split_domains("nasa.gov, -reddit.com"),
            ["nasa.gov", "-reddit.com"],
        )

    def test_blank_domains_become_none_not_an_empty_filter(self) -> None:
        """An empty list would send `filters: {}`, which the core omits
        entirely on purpose — see `_submit`."""
        for value in (None, "", "   ", ",,, ,"):
            with self.subTest(value=value):
                self.assertIsNone(adapter._split_domains(value))

    def test_too_many_domains_is_rejected(self) -> None:
        many = ",".join(f"d{i}.com" for i in range(core._MAX_DOMAINS + 1))
        with self.assertRaises(llm.ModelError) as ctx:
            adapter._split_domains(many)
        self.assertIn(str(core._MAX_DOMAINS), str(ctx.exception))

    def test_recency_is_checked_against_the_core_enum(self) -> None:
        self.assertEqual(adapter._validate_recency("week"), "week")
        self.assertIsNone(adapter._validate_recency(None))
        with self.assertRaises(llm.ModelError):
            adapter._validate_recency("fortnight")

    def test_preset_is_passed_through_unvalidated(self) -> None:
        """Upstream declares no enum; allowlisting would reject future presets."""
        self.assertEqual(adapter._require_preset("some-future-preset"), "some-future-preset")

    def test_absent_preset_defaults_but_empty_preset_is_an_error(self) -> None:
        """`None` and `""` are not the same thing.

        Folding them together (`value or "medium"`) silently ran a
        medium-preset job — real money, on a preset the caller never asked
        for — and left the emptiness check below it unreachable.
        """
        self.assertEqual(adapter._require_preset(None), "medium")
        for empty in ("", "   "):
            with self.subTest(value=empty):
                with self.assertRaises(llm.ModelError):
                    adapter._require_preset(empty)

    def test_timeout_is_clamped_not_trusted(self) -> None:
        self.assertEqual(adapter._clamp_timeout(None), adapter.DEFAULT_TIMEOUT_SECONDS)
        self.assertEqual(adapter._clamp_timeout(10), 10)
        self.assertEqual(adapter._clamp_timeout(10**9), adapter.MAX_TIMEOUT_SECONDS)
        for bad in (0, -1):
            with self.subTest(value=bad):
                with self.assertRaises(llm.ModelError):
                    adapter._clamp_timeout(bad)


@unittest.skipUnless(HAVE_LLM, "the `llm` extra is not installed")
class TestKeyBridge(unittest.TestCase):
    """`llm` keeps keys in its own store; the core reads an env var."""

    def test_key_is_set_for_the_call_and_restored_afterwards(self) -> None:
        with unittest.mock.patch.dict("os.environ", {core._KEY_ENV_VAR: "pre-existing"}):
            with adapter._api_key_from_llm("from-llm-store"):
                import os

                self.assertEqual(os.environ[core._KEY_ENV_VAR], "from-llm-store")
            import os

            self.assertEqual(
                os.environ[core._KEY_ENV_VAR],
                "pre-existing",
                "the plugin must leave the environment as it found it",
            )

    def test_absent_variable_is_removed_again_not_left_behind(self) -> None:
        import os

        with unittest.mock.patch.dict("os.environ", {}, clear=True):
            with adapter._api_key_from_llm("from-llm-store"):
                self.assertEqual(os.environ[core._KEY_ENV_VAR], "from-llm-store")
            self.assertNotIn(
                core._KEY_ENV_VAR,
                os.environ,
                "a variable that did not exist before must not exist after",
            )

    def test_no_key_leaves_the_environment_untouched(self) -> None:
        """With no key from `llm`, the core raises its own clear error."""
        import os

        with unittest.mock.patch.dict("os.environ", {}, clear=True):
            with adapter._api_key_from_llm(None):
                self.assertNotIn(core._KEY_ENV_VAR, os.environ)


@unittest.skipUnless(HAVE_LLM, "the `llm` extra is not installed")
class TestOutputShaping(unittest.TestCase):
    """The two adapters share one rendering and differ only on wrapping."""

    PAYLOAD: ClassVar[dict[str, object]] = {
        "status": "completed",
        "output": [
            {
                "type": "search_results",
                "results": [{"url": "https://a.example/x", "title": "A"}],
            },
            {
                "type": "message",
                "content": [{"type": "output_text", "text": "The answer."}],
            },
        ],
    }

    def test_plain_body_carries_answer_and_sources_without_a_wrapper(self) -> None:
        body = core._answer_body(self.PAYLOAD)
        self.assertIn("The answer.", body)
        self.assertIn("https://a.example/x", body)
        self.assertNotIn("untrusted-web-content", body)

    def test_the_mcp_server_still_always_wraps(self) -> None:
        """The CLI default must not have changed the server's behaviour."""
        self.assertIn("untrusted-web-content", core._format_answer(self.PAYLOAD))

    def test_spotlighting_the_same_body_is_still_available(self) -> None:
        wrapped = core._spotlight(core._answer_body(self.PAYLOAD))
        self.assertIn("untrusted-web-content", wrapped)
        self.assertIn("The answer.", wrapped)


@unittest.skipUnless(HAVE_LLM, "the `llm` extra is not installed")
class TestModelRegistration(unittest.TestCase):
    def test_the_model_registers_under_a_stable_id(self) -> None:
        registered: list[adapter.PerplexityAgent] = []
        adapter.register_models(registered.append)
        self.assertEqual(len(registered), 1)
        self.assertEqual(registered[0].model_id, "perplexity-agent")

    def test_it_shares_the_key_alias_with_the_existing_perplexity_plugin(self) -> None:
        """So `llm keys set perplexity` configures both, and the MCP
        server's own variable works too."""
        model = adapter.PerplexityAgent()
        self.assertEqual(model.needs_key, "perplexity")
        self.assertEqual(model.key_env_var, core._KEY_ENV_VAR)


@unittest.skipUnless(HAVE_LLM, "the `llm` extra is not installed")
class TestAgainstFakeUpstream(unittest.TestCase):
    """One end-to-end pass with no real network and no spend."""

    def setUp(self) -> None:
        self.fake = FakePerplexity()
        self._real_base = core.API_BASE
        core.API_BASE = self.fake.url
        self.addCleanup(self._restore)
        self._env = unittest.mock.patch.dict("os.environ", {core._KEY_ENV_VAR: "pplx-test-key"})
        self._env.start()
        self.addCleanup(self._env.stop)

    def _restore(self) -> None:
        core.API_BASE = self._real_base
        self.fake.close()

    def test_a_completed_run_yields_a_plain_answer(self) -> None:
        self.fake.script(
            (200, {"id": "resp_x", "status": "queued"}),
            (200, TestOutputShaping.PAYLOAD),
        )
        model = adapter.PerplexityAgent()
        prompt = llm.Prompt("why?", model=model)
        prompt.options = model.Options()
        chunks = list(model.execute(prompt, stream=False, response=None))
        text = "".join(chunks)
        self.assertIn("The answer.", text)
        self.assertNotIn("untrusted-web-content", text, "spotlight is off by default")

    def test_submit_never_sends_model_through_this_path_either(self) -> None:
        self.fake.script(
            (200, {"id": "resp_x", "status": "queued"}),
            (200, TestOutputShaping.PAYLOAD),
        )
        model = adapter.PerplexityAgent()
        prompt = llm.Prompt("why?", model=model)
        prompt.options = model.Options(preset="xhigh", recency="week")
        list(model.execute(prompt, stream=False, response=None))
        _, path, body = self.fake.requests[0]
        self.assertEqual(path, "/v1/agent")
        self.assertNotIn("model", body)
        self.assertEqual(body["preset"], "xhigh")
        self.assertEqual(body["tools"][0]["filters"]["search_recency_filter"], "week")


if __name__ == "__main__":
    unittest.main()
