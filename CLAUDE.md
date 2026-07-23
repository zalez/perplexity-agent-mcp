# Working on perplexity-agent-mcp

This file is guardrails for an agent **contributing to** this repository, not
a summary of what it does — read [README.md](README.md) for that. It exists
because this project has a specific, easy-to-trip failure mode: a
well-intentioned change that looks like an obvious improvement and quietly
breaks the one property the whole repo exists to have. Two concrete examples
that would each pass a casual glance:

- Adding `requests` (or any other third-party package) because it's more
  ergonomic than `urllib.request`. This project's entire pitch is **zero
  runtime dependencies** — see [SECURITY.md](SECURITY.md#supply-chain) for
  why that's a security property, not a style preference.
- Adding a `print()` for debugging and forgetting to remove it, or having any
  code path write to the real stdout. stdout is the JSON-RPC protocol
  stream; one stray line corrupts it, and the client-side symptom is a
  baffling parse error, not an obvious crash.

Both are mechanically caught before merge (`tests/test_no_dependencies.py`;
the stdout-rebind + `tests/test_no_secrets.py`), but catching a mistake in
CI is worse than not making it. This file exists so you don't make it.

Also read: [CONTRIBUTING.md](CONTRIBUTING.md) for the contributor workflow,
[SECURITY.md](SECURITY.md) for the full threat model, and
[docs/specs/2026-07-22-perplexity-agent-mcp-design.md](docs/specs/2026-07-22-perplexity-agent-mcp-design.md)
for the original design rationale. **If any of those, or this file, disagree
with `perplexity_agent_mcp.py` itself, the code is correct and the doc is
stale — fix the doc, don't trust it over the source.**

## 1. Hard invariants

Every one of these applies to every change, no exceptions without discussing
it with the maintainer first. Quoted from the implementation plan's Global
Constraints:

- **Zero third-party runtime dependencies.** stdlib only. No
  `requirements.txt`, no MCP SDK, no `pip install` for users. Enforced by
  `tests/test_no_dependencies.py`.
- **Python floor `>=3.10`.** No `match` statements requiring 3.11+, no
  `tomllib` in the server itself (3.11+), no PEP 695 generics. Develop on
  3.14.6; CI matrix covers 3.10–3.14.
- **Single outbound host:** `https://api.perplexity.ai` only. Hardcoded
  constant. **No environment-variable base-URL override** — it is an
  API-key exfiltration vector (SECURITY.md §Network).
- **The API key is never printed, logged, echoed, or attached to an
  exception.** Read from `PERPLEXITY_API_KEY` at call time, not import time.
- **stdout is exclusively JSON-RPC.** All logging goes to stderr.
  `sys.stdout` is rebound to `sys.stderr` at startup.
- **MCP protocol revision `2025-11-25`.** Accept `2025-11-25`, `2025-06-18`,
  `2025-03-26`; echo on match, else return `2025-11-25`. Never error on
  version negotiation.
- **Validation errors are `isError: true`, never JSON-RPC `-32602`**
  (SEP-1303 — see [§3](#3-error-handling) below).
- **Line length 100.** Ruff-formatted. `mypy --strict` clean. Every public
  function annotated.
- **Comment density is high by design.** This file's product is
  auditability; a reader must follow it top to bottom without
  cross-referencing.
- **Copyright:** `Copyright (c) 2026, Constantin Gonzalez`. **License:**
  BSD-3-Clause.
- **Never claim cancellation reduces the bill** — in code comments, tool
  descriptions, or docs.

## 2. Decisions and why

Condensed from the design spec's §4. Read the full rationale there before
reversing any of these — most were arrived at after rejecting something
simpler that turned out to be wrong.

| # | Decision | Why |
|---|---|---|
| D1 | Single file + PEP 723, plus an opt-in `pyproject.toml` | The audit boundary stays exactly one file. Packaging ships the same bytes, not extra code. |
| D2 | Target MCP `2025-11-25`, flat dispatch dict | Every shipping client speaks it today. stdio framing is unchanged in the next revision, so upgrading later is additive, not a rewrite. |
| D3 | Python floor 3.10, develop/gate on 3.14 | Client configs invoke bare `python3`; stock macOS `python3` is 3.9.6. Nothing here needs newer syntax, and documenting an EOL floor on a security repo looks bad. |
| D4 | stdlib `unittest`, fake upstream via `http.server` | `git clone && python3 -m unittest` with nothing installed. The zero-supply-chain claim holds for contributors too, not just users. |
| D5 | Background + poll upstream, always | One code path for every preset. Each HTTP call is short, so a network blip can't kill a long-running job. |
| D6 | Params: `query`, `preset`, `recency`, `domains` — never `model` | `anthropic/*` models 400 without `max_output_tokens`, and model ids drift. `preset` tracks Perplexity's own updates instead. |
| D7 | Full CI gate set, Actions pinned to SHAs | The "zero dependencies" claim must be mechanically enforced, not just asserted. Tag-pinned Actions are a live supply-chain hole. |
| D8 | `flit_core` build backend | Resolves to one package, zero transitive deps. Reads `__version__` and the module docstring directly — no separate version to drift. |
| D9 | Spotlighting via a randomized delimiter | A *fixed* tag has an obvious break-out (the hostile page just includes the closing tag); a per-response nonce structurally prevents it. |
| D10 | Delimiting, not datamarking | The paper's stronger recommendation interleaves markers through whitespace, mangling URLs and making answers unquotable — wrong trade for a tool whose output is meant to be read and cited. See SECURITY.md. |
| D11 | Three tools, not one (`_agent`, `_result`, `_cancel`) | A >60s synchronous call is dead on arrival on Claude Desktop and unfixable by the user. Submit/poll/cancel is one coherent lifecycle, not feature creep. |
| D12 | `wait=true` default, 55s budget, overridable to 300s | Tuned to the tightest real client (Claude Desktop's unconfigurable 60s ceiling) so the out-of-box config works everywhere; `medium` finishes in ~12s in practice, so the common case stays one call. |
| D13 | A blown deadline hands back an id; it never cancels | Cancel-on-timeout would destroy work already paid for. A blown budget must degrade into the async path, never into nothing. |
| D14 | Opportunistic `notifications/progress` | Sent only when the request carries a `progressToken`. Pure upside where supported, a no-op elsewhere. |
| D15 | Never claim cancellation saves money | Cancelled runs report no `usage` and no `cost` at all; the docs are silent on whether you're still billed. The tool says it stops the run and nothing more. |

## 3. Error handling

From the design spec's §10, and directly reflected in `dispatch()` /
`handle_tools_call()`. **The one rule to internalize:** a bad *tool argument*
is never a JSON-RPC error — it's a tool result with `isError: true`, so the
calling model can read the message and correct itself. JSON-RPC error codes
are reserved for failures in the protocol layer itself (unknown method,
unknown tool *name*, malformed `params` shape) — never for anything that
happens *inside* a tool that was found and dispatched correctly.

| Situation | Response |
|---|---|
| Unknown method | `-32601` |
| Unknown tool name, malformed `params` shape (`arguments` not an object) | `-32602` |
| **Bad argument value** (empty query, bad `recency`, malformed `response_id`) | **`isError: true`** |
| Missing / empty `PERPLEXITY_API_KEY` | `isError: true` |
| Perplexity 401 / 429 / 400, timeout, network failure | `isError: true` |
| `status: "failed"` | `isError: true`, surfacing upstream `error.message` |
| **Run still in progress** (`_result`, or `_agent`'s wait budget expired) | **`isError: false`** — a legitimate state, not a failure. Returns the id, a progress summary, and the follow-up call to make. Marking it an error would invite the caller to restart the whole run. |
| Cancel on an already-terminal run (upstream 400) | **`isError: false`** — the goal state is already achieved; benign, not a failure |
| Cancel with an unknown / foreign id (upstream 404) | `isError: true` |
| `status: "incomplete"` | Partial answer **plus an explicit leading note** — a half answer silently presented as whole is the worst outcome |
| Unhandled exception inside a tool body | `isError: true` (never a traceback) |
| Unhandled exception in the dispatcher itself | `-32603` |
| A line parses to a JSON array (batching was removed from MCP) | `-32600` |
| Unparsable line | `-32700`, `id: null` |
| Any notification, known or unknown | **no response at all** — replying to a notification violates the spec |
| EOF on stdin | clean exit `0` |

The read loop must never die, for any input. Error text is sanitised —
upstream messages are surfaced (length-bounded), but headers and tracebacks
never are.

## 4. Perplexity API gotchas

Facts about Perplexity's Agent API that are easy to get wrong, verified
against the live API (not just its docs, which disagree with reality in
several of these):

- **There is no `output_text` field in the HTTP response.** It's a
  convenience property Perplexity's own SDKs synthesize client-side. You
  have to reconstruct it: walk `output[]` for items where `type ==
  "message"`, then their `content[]` for parts where `type ==
  "output_text"`, and concatenate `text`. See `_extract_answer`. Assuming
  `output_text` exists on the wire is the single easiest way to ship a
  server that silently returns empty answers.
- **Citations live in `search_results` output items, not a `citations`
  field.** `output[]` entries with `type == "search_results"` carry a
  `results[]` list of `{url, title, ...}`. The inline `annotations` array is
  empty in every observed example and must not be relied on. See
  `_extract_sources`.
- **Recency and domain filters nest under `tools[].filters`**, as
  `search_recency_filter` / `search_domain_filter` — not top-level request
  fields. See `_submit`. Omit the `filters` object entirely when neither is
  set; an empty `filters: {}` is a needless deviation from the documented
  shape.
- **The output-length control is `max_output_tokens`, not `max_tokens`.** A
  field literally named `max_tokens` does exist, but only inside the
  `web_search` tool object, and it means something unrelated (a budget for
  that tool's own use, not the response). This server does not currently
  send either — no tool exposes an output-length knob — but if you ever add
  one, use `max_output_tokens`, top-level.
- **Reasoning effort is `reasoning: {"effort": ...}`, a nested object — not
  a top-level `reasoning_effort`.** Same caveat as above: this server
  doesn't currently send it, but get the shape right if you add it.
- **Never send `model`.** `anthropic/*` models return HTTP 400 unless
  `max_output_tokens` is also supplied (which this server doesn't send), and
  model ids drift over time regardless. `preset` selects the model on
  Perplexity's side and tracks their own updates for free. See `_submit`'s
  docstring. (This is D6 in [§2](#2-decisions-and-why) above.)
- **`preset` is an open string, not a closed enum.** Perplexity's own
  OpenAPI schema declares no enum for it, and `wide-research` already
  falsified an earlier guessed list. Don't add client-side allowlisting —
  upstream validates it, and a preset added tomorrow should work today
  without a server change.
- **Parse the response envelope tolerantly.** The live payload carries
  roughly 25 top-level fields absent from Perplexity's own published
  OpenAPI spec, and its `usage` object contradicts that spec outright. BAND
  3 reads only the handful of fields it needs and ignores the rest — don't
  add strict schema validation against a shape that's demonstrably not
  stable.
- **Background mode (`background: true`) is required for `wide-research`**
  and is what this server sends for every request regardless of preset —
  poll `GET /v1/agent/{id}`, cancel via `POST /v1/agent/{id}/cancel`. No
  request here ever holds a socket open for the length of a research run.
- **No `Retry-After` or rate-limit headers are documented.** Perplexity's
  own docs prescribe exponential backoff with jitter on 429/5xx, which is
  exactly what `_request` does — don't "simplify" that into a fixed delay.

## 5. Exact local commands

These match `.github/workflows/ci.yml` job-for-job. Tool versions below are
what's pinned **today** in `.pre-commit-config.yaml` and `ci.yml`
(kept identical between the two, enforced by `tests/test_tooling_parity.py`)
— if they look different from what's actually in those two files, the files
are correct and this doc is stale.

```bash
# `test` job — matrix 3.10, 3.11, 3.12, 3.13, 3.14; run on whichever
# interpreters you have locally
python3 -m unittest discover -v

# `lint` job
pip install ruff==0.15.22 mypy==2.3.0
ruff check .
ruff format --check .
mypy --strict perplexity_agent_mcp.py

# `pre-commit` job — the other 14 hooks (file hygiene, gitleaks, actionlint,
# zizmor, codespell); ruff/mypy/unittest are skipped here since the two jobs
# above already cover them against the same pinned versions
SKIP=ruff-check,ruff-format,mypy,unittest pre-commit run --all-files --show-diff-on-failure

# equivalent, and what you actually want locally day to day: run every hook
pre-commit run --all-files

# `package` job — build the wheel, install into a clean venv, drive the real
# console script over pipes
uv build
uv venv /tmp/smoke
uv pip install --python /tmp/smoke/bin/python dist/*.whl
printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"ci","version":"1"}}}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' \
  | /tmp/smoke/bin/perplexity-agent-mcp
```

`mypy --strict` is scoped to `perplexity_agent_mcp.py` only, in both
pre-commit and CI — not `tests/`, which uses looser annotations by design
(see the `per-file-ignores` in `pyproject.toml`). Running `mypy --strict .`
against the whole tree happens to be clean too as of this writing, but
nothing gates that — it can go stale without CI ever noticing. If you want
that confidence, check it yourself; don't assume CI has your back on it.

## 6. Before you commit

Run **both** of these — they catch different things on this repo, not the
same thing twice:

```bash
pre-commit run --all-files
```

and the individual commands from [§5](#5-exact-local-commands) above,
**separately**. Concretely, here's what a green `pre-commit run --all-files`
does *not* prove:

- **Nothing about Python versions other than the one on your `PATH`.**
  Pre-commit's local `unittest` hook runs on whichever `python3` you have —
  one interpreter. CI's `test` job runs the identical command across all
  five supported versions (3.10–3.14). If your change touches anything that
  could plausibly behave differently across that range, run it on more than
  one interpreter yourself.
- **Nothing about packaging.** Pre-commit never runs `uv build`. CI's
  `package` job does, and then goes further — installs the built wheel into
  a clean environment and drives the real `perplexity-agent-mcp` console
  script over pipes. A change to `pyproject.toml`, `__version__`, or
  anything about how the module is imported or executed needs `uv build`
  run by hand; a clean pre-commit run is silent on all of it.

A green `pre-commit run --all-files` is necessary. It is not sufficient.
Reproduce CI for real before you push, especially on anything
version-sensitive or packaging-related — "passed pre-commit" and "passes
CI" are different claims on this repo, and the gap between them is exactly
the two bullets above.
