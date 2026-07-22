"""Tests for the HTTP and Perplexity bands, exercised in-process."""

from __future__ import annotations

import unittest
import unittest.mock

import perplexity_agent_mcp as srv

from .fake_perplexity import FakePerplexity


class ClientTestCase(unittest.TestCase):
    """Base: stands up a fake upstream and points the module at it."""

    def setUp(self) -> None:
        self.fake = FakePerplexity()
        self._real_base = srv.API_BASE
        srv.API_BASE = self.fake.url
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        srv.API_BASE = self._real_base
        self.fake.close()


class TestApiKey(ClientTestCase):
    def test_missing_key_raises_clean_error(self) -> None:
        with unittest.mock.patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(srv.PerplexityError) as ctx:
                srv._api_key()
        self.assertIn("PERPLEXITY_API_KEY", ctx.exception.message)

    def test_empty_key_raises_clean_error(self) -> None:
        with unittest.mock.patch.dict("os.environ", {"PERPLEXITY_API_KEY": "   "}):
            with self.assertRaises(srv.PerplexityError):
                srv._api_key()


class TestRequest(ClientTestCase):
    def setUp(self) -> None:
        super().setUp()
        self._env = unittest.mock.patch.dict("os.environ", {"PERPLEXITY_API_KEY": "pplx-test-key"})
        self._env.start()
        self.addCleanup(self._env.stop)

    def test_post_sends_body_and_returns_parsed_json(self) -> None:
        self.fake.script((200, {"id": "resp_1", "status": "queued"}))
        result = srv._request("POST", "/v1/agent", {"input": "hello"})
        self.assertEqual(result["id"], "resp_1")
        method, path, body = self.fake.requests[0]
        self.assertEqual(method, "POST")
        self.assertEqual(path, "/v1/agent")
        self.assertEqual(body["input"], "hello")

    def test_non_2xx_raises_with_upstream_message(self) -> None:
        self.fake.script((401, {"error": {"message": "Invalid API key provided.", "code": 401}}))
        with self.assertRaises(srv.PerplexityError) as ctx:
            srv._request("GET", "/v1/agent/resp_1")
        self.assertIn("Invalid API key", ctx.exception.message)

    def test_error_code_as_string_is_tolerated(self) -> None:
        """Their OpenAPI says code is a string; the live API returns an int."""
        self.fake.script((400, {"error": {"message": "bad", "code": "400"}}))
        with self.assertRaises(srv.PerplexityError):
            srv._request("GET", "/v1/agent/resp_1")

    def test_key_never_appears_in_exception(self) -> None:
        self.fake.script((500, {"error": {"message": "boom"}}))
        with self.assertRaises(srv.PerplexityError) as ctx:
            srv._request("GET", "/v1/agent/resp_1")
        self.assertNotIn("pplx-test-key", str(ctx.exception))
        self.assertNotIn("pplx-test-key", ctx.exception.message)

    def test_network_error_message_never_leaks_key(self) -> None:
        """Worst case for the redaction in `_request`: the transport-level
        exception's own message echoes the request (as real socket/TLS
        errors sometimes do). Only `type(exc).__name__` may survive into
        PerplexityError - never the original message.

        This is distinct from test_key_never_appears_in_exception above,
        which only exercises the urllib.error.HTTPError branch (an upstream
        JSON error body, which can't contain the key because we control what
        we send). This test exercises the other except clause in `_request`
        - urllib.error.URLError/TimeoutError/ssl.SSLError/OSError - which is
        the one the redaction comment is actually about.
        """
        poisoned = OSError("reset while sending 'Authorization: Bearer pplx-test-key'")
        with unittest.mock.patch("urllib.request.urlopen", side_effect=poisoned):
            with self.assertRaises(srv.PerplexityError) as ctx:
                srv._request("GET", "/v1/agent/resp_1")
        self.assertNotIn("pplx-test-key", str(ctx.exception))
        self.assertNotIn("pplx-test-key", ctx.exception.message)
        self.assertIn("OSError", ctx.exception.message)

    def test_retries_5xx_then_succeeds(self) -> None:
        self.fake.script(
            (500, {"error": {"message": "transient"}}),
            (200, {"id": "resp_2", "status": "queued"}),
        )
        result = srv._request("POST", "/v1/agent", {"input": "x"})
        self.assertEqual(result["id"], "resp_2")

    def test_does_not_retry_4xx(self) -> None:
        self.fake.script((400, {"error": {"message": "bad request"}}))
        with self.assertRaises(srv.PerplexityError):
            srv._request("POST", "/v1/agent", {"input": "x"})
        self.assertEqual(len(self.fake.requests), 1, "4xx must not be retried")


if __name__ == "__main__":
    unittest.main()
