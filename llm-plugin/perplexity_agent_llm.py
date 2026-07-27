"""Perplexity Agent API as a model for Simon Willison's `llm` CLI.

    llm -m perplexity-agent 'What changed in MCP 2026-07-28?'

A second adapter on the same core. `perplexity_agent_mcp.py` is built in four
bands — CONFIG, HTTP, PERPLEXITY, MCP — of which only the last is protocol
specific. Bands 1-3 are a complete Perplexity Agent client, so this module is
a thin translation layer over them rather than a reimplementation: every API
quirk, retry rule, redirect refusal and payload bound is shared, and a fix in
one adapter is a fix in both.

Optional. `llm` is an extra (`pip install perplexity-agent-mcp[llm]`); the MCP
server never imports this file and has no third-party dependencies of any
kind. Installing it does not change the server.

Why this exists at all: `llm` has no MCP support (simonw/llm#696, open since
January 2025), and the existing `llm-perplexity` plugin wraps the older Sonar
chat models rather than the Agent API — the same gap the MCP server fills, in
a different tool.
"""

from __future__ import annotations

import os
import sys
import time
from typing import Any

import llm

# `llm.Options` IS a pydantic model and `from pydantic import Field` is the
# pattern llm's own plugin documentation uses, so pydantic is part of llm's
# plugin API rather than an incidental transitive import. It is a direct
# dependency of llm, so it is present whenever this module can be imported
# at all — which is why the `llm` extra does not list it separately.
from pydantic import Field

import perplexity_agent_mcp as core

__version__ = core.__version__

# Long enough that a deep run finishes without anyone thinking about it. The
# MCP server defaults to 55s for one specific reason — Claude Desktop kills a
# tool call at 60s and its users cannot change that — and none of that applies
# to a terminal, which waits as long as you let it.
DEFAULT_TIMEOUT_SECONDS = 300

# Keep the ceiling the same as the server's, so neither adapter can be talked
# into waiting absurdly long by a stray argument.
MAX_TIMEOUT_SECONDS = core._WAIT_SECONDS_MAX


@llm.hookimpl
def register_models(register: Any) -> None:
    register(PerplexityAgent())


class PerplexityAgent(llm.Model):
    """One-shot research. Not a chat model: every call is an independent run."""

    model_id = "perplexity-agent"
    can_stream = False

    # Deliberately the same key alias `llm-perplexity` uses. It is the same
    # Perplexity API key, so anyone who has already run `llm keys set
    # perplexity` for that plugin gets this one working with no extra setup.
    needs_key = "perplexity"
    # And this is the variable the MCP server reads, so a shell that is
    # already configured for the server needs no setup either.
    key_env_var = "PERPLEXITY_API_KEY"

    class Options(llm.Options):
        preset: str | None = Field(
            default="medium",
            description=(
                "Research depth: fast, low, medium, high, xhigh, wide-research. "
                "Passed through unvalidated — Perplexity's schema declares no "
                "enum, so a preset added tomorrow works today."
            ),
        )
        recency: str | None = Field(
            default=None,
            description="Only use sources published within: hour, day, week, month, year.",
        )
        domains: str | None = Field(
            default=None,
            description=(
                "Comma-separated domains to restrict sources to, e.g. "
                "'nasa.gov,-reddit.com'. Prefix with '-' to exclude. Max 20."
            ),
        )
        spotlight: bool | None = Field(
            default=False,
            description=(
                "Wrap the answer in a delimiter marking it as untrusted web "
                "content. Off by default here; see the module docstring."
            ),
        )
        timeout: int | None = Field(
            default=DEFAULT_TIMEOUT_SECONDS,
            description=f"Seconds to wait before giving up (max {MAX_TIMEOUT_SECONDS}).",
        )

    def __str__(self) -> str:
        return f"Perplexity Agent: {self.model_id}"

    def execute(
        self,
        prompt: llm.Prompt,
        stream: bool,
        response: llm.Response,
        conversation: Any = None,
    ) -> Any:
        # `llm.Prompt.options` is annotated as the base `Options`, so narrow it
        # to this model's own subclass. A real isinstance check rather than a
        # cast: `llm` always builds the model's own Options, so this is true at
        # runtime, and if that ever stops being true a clear error beats an
        # AttributeError from three lines further down.
        options = prompt.options
        if not isinstance(options, self.Options):
            raise llm.ModelError(f"Expected {self.Options.__name__}, got {type(options).__name__}.")

        query = (prompt.prompt or "").strip()
        if not query:
            raise llm.ModelError("A query is required.")

        budget = _clamp_timeout(options.timeout)
        deadline = time.monotonic() + budget

        with _api_key_from_llm(self.get_key()):
            response_id = core._submit(
                query,
                _require_preset(options.preset),
                _validate_recency(options.recency),
                _split_domains(options.domains),
                deadline=deadline,
            )
            payload, terminal = core._poll(response_id, budget=budget, notify=_progress)

            if not terminal:
                # The run keeps going server-side and keeps costing money, so
                # say so and name it rather than implying it stopped.
                raise llm.ModelError(
                    f"Timed out after {budget}s. Perplexity run {response_id} is still "
                    "running server-side and will finish (and bill) whether or not "
                    "anyone collects it. Raise -o timeout, or stop it with the "
                    "perplexity_agent_cancel MCP tool."
                )

            status = payload.get("status")
            if status == "failed":
                raise llm.ModelError(f"The research run failed: {_upstream_error(payload)}")
            if status == "cancelled":
                raise llm.ModelError("The research run was cancelled.")

        body = core._answer_body(payload)
        yield core._spotlight(body) if options.spotlight else body


def _clamp_timeout(value: int | None) -> int:
    if value is None:
        return DEFAULT_TIMEOUT_SECONDS
    if value <= 0:
        raise llm.ModelError("-o timeout must be a positive number of seconds.")
    return min(int(value), MAX_TIMEOUT_SECONDS)


def _require_preset(value: str | None) -> str:
    """Empty is a mistake; anything else goes upstream unexamined.

    `None` (not supplied) and `""` (supplied but empty) are deliberately not
    the same thing. Folding them together with `value or "medium"` makes the
    empty case silently run a medium-preset job — spending real money on a
    preset the caller did not ask for — and leaves the check below it dead.
    """
    if value is None:
        return "medium"
    preset = value.strip()
    if not preset:
        raise llm.ModelError("-o preset must not be empty.")
    return preset


def _validate_recency(value: str | None) -> str | None:
    """The one closed enum in the set — upstream rejects anything else."""
    if value is None:
        return None
    recency = value.strip()
    if recency not in core._RECENCY_VALUES:
        allowed = ", ".join(sorted(core._RECENCY_VALUES))
        raise llm.ModelError(f"-o recency must be one of: {allowed}.")
    return recency


def _split_domains(value: str | None) -> list[str] | None:
    """`llm` options are scalars, so a list arrives comma-separated."""
    if not value:
        return None
    domains = [part.strip() for part in value.split(",") if part.strip()]
    if not domains:
        return None
    if len(domains) > core._MAX_DOMAINS:
        raise llm.ModelError(f"-o domains accepts at most {core._MAX_DOMAINS} entries.")
    return domains


def _upstream_error(payload: dict[str, object]) -> str:
    error = payload.get("error")
    if isinstance(error, dict):
        message = error.get("message")
        if isinstance(message, str) and message:
            return core._truncate(message, core._MAX_ERROR_CHARS)
    return "no reason given"


def _progress(message: str, _elapsed: float) -> None:
    """Poll progress goes to stderr, never into the answer.

    Deep research takes minutes and a silent terminal looks hung. stderr keeps
    it visible while `llm -m perplexity-agent ... > out.txt` still writes a
    clean answer, and `2>/dev/null` silences it. It must not be yielded: the
    answer is what `llm` logs to its SQLite history, and progress chatter is
    not part of the answer.
    """
    sys.stderr.write(f"[perplexity-agent] {message}\n")
    sys.stderr.flush()


class _api_key_from_llm:  # noqa: N801 - a context manager, named like one
    """Bridge `llm`'s key store to the server's environment-variable lookup.

    The core resolves the key from PERPLEXITY_API_KEY at call time, which is
    the right design for a server whose whole configuration is its MCP client's
    env block. `llm` instead resolves keys from its own store, `--key`, or the
    environment. This sets the variable for the duration of the call and puts
    it back afterwards, so the process is left exactly as it was found — no
    permanent mutation of os.environ just because a plugin ran.
    """

    def __init__(self, key: str | None) -> None:
        self._key = key
        self._previous: str | None = None
        self._was_set = False

    def __enter__(self) -> None:
        if not self._key:
            # Leave the environment alone and let the core raise its own clear
            # "PERPLEXITY_API_KEY is not set" error.
            return
        self._was_set = core._KEY_ENV_VAR in os.environ
        self._previous = os.environ.get(core._KEY_ENV_VAR)
        os.environ[core._KEY_ENV_VAR] = self._key

    def __exit__(self, *exc: object) -> None:
        if not self._key:
            return
        if self._was_set and self._previous is not None:
            os.environ[core._KEY_ENV_VAR] = self._previous
        else:
            os.environ.pop(core._KEY_ENV_VAR, None)
