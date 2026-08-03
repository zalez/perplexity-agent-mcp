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

- **Zero third-party runtime dependencies** in the server, and no extras
  either. stdlib only. No `requirements.txt`, no MCP SDK, no `pip install`
  for users. The `llm` adapter has dependencies, but it is a **separate
  distribution** built from `llm-plugin/` — installing the server never
  pulls it in. Enforced by `tests/test_no_dependencies.py`, which allows the
  adapter exactly two dependencies and the server none.
- **Python floor `>=3.10`.** No `match` statements requiring 3.11+, no
  `tomllib` in the server itself (3.11+), no PEP 695 generics. Develop on
  3.14.6; CI matrix covers 3.10–3.14.
- **Single outbound host:** `https://api.perplexity.ai` only. Hardcoded
  constant. **No environment-variable base-URL override** — it is an
  API-key exfiltration vector (SECURITY.md §Network).
- **The API key is never printed, logged, echoed, or attached to an
  exception.** Read from `PERPLEXITY_API_KEY` at call time, not import time.
- **stdout is exclusively JSON-RPC.** All logging goes to stderr. `main()`
  calls `_claim_stdout()`, which grabs the real stdout and points
  `sys.stdout` at stderr. It is deliberately NOT an import-time side effect:
  the `llm` adapter imports this module, and hijacking stdout on import
  would redirect that whole program's output. Don't move it back.
- **Two MCP protocol revisions, one process.** Modern `2026-07-28`
  (stateless, no handshake) and legacy `2025-11-25` / `2025-06-18` /
  `2025-03-26`. **Era is decided per request, structurally** — never by a
  flag, a setting, or state carried from an earlier request. The three
  precedence rules live above `HANDLERS` in the source; that comment is the
  authority, not this line.
- **Never error on version negotiation — in `initialize` only.** Echo the
  requested legacy version on a match, else return `2025-11-25`. The modern
  path has the opposite rule and MUST return `-32022` for a revision it does
  not speak. **Do not unify these two.** They are pinned by tests from
  opposite sides precisely because unifying them looks like a tidy-up.
- **`2026-07-28` must never enter `SUPPORTED_PROTOCOL_VERSIONS`**, which is
  the legacy-negotiable set. It is derived from the tail of
  `ADVERTISED_PROTOCOL_VERSIONS` so this is impossible rather than merely
  forbidden; keep the derivation.
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
| D2 | Dual-era: MCP `2026-07-28` and `2025-11-25` from one flat dispatch dict | The bet in the original D2 paid off. stdio framing was unchanged across the revision, and the flat `HANDLERS` dict plus the deliberate absence of any session state meant adding the stateless era was additive — one more entry, plus era/shaping in `dispatch()`. `HANDLERS` values became records so a new method cannot be added without declaring its era. See D18. |
| D3 | Python floor 3.10, develop/gate on 3.14 | Client configs invoke bare `python3`; stock macOS `python3` is 3.9.6. Nothing here needs newer syntax, and documenting an EOL floor on a security repo looks bad. |
| D4 | stdlib `unittest`, fake upstream via `http.server` | `git clone && python3 -m unittest` with nothing installed. The zero-supply-chain claim holds for contributors too, not just users. |
| D5 | Background + poll upstream, always | One code path for every preset. Each HTTP call is short, so a network blip can't kill a long-running job. |
| D6 | Params: `query`, `preset`, `recency`, `domains` — never `model` | `anthropic/*` models 400 without `max_output_tokens`, and model ids drift. `preset` tracks Perplexity's own updates instead. |
| D7 | Full CI gate set, Actions pinned to SHAs | The "zero dependencies" claim must be mechanically enforced, not just asserted. Tag-pinned Actions are a live supply-chain hole. |
| D8 | `setuptools` build backend (was `flit_core`) | flit was chosen for resolving to one package with zero transitive deps — but it builds exactly ONE module per distribution. setuptools is the only backend that keeps the property flit was chosen for: still one package, still zero transitive deps. hatchling would have cost five. `__version__` is still the single source of truth, read via `[tool.setuptools.dynamic]`. |
| D9 | Spotlighting via a randomized delimiter | A *fixed* tag has an obvious break-out (the hostile page just includes the closing tag); a per-response nonce structurally prevents it. |
| D10 | Delimiting, not datamarking | The paper's stronger recommendation interleaves markers through whitespace, mangling URLs and making answers unquotable — wrong trade for a tool whose output is meant to be read and cited. See SECURITY.md. |
| D11 | Three tools, not one (`_agent`, `_result`, `_cancel`) | A >60s synchronous call is dead on arrival on Claude Desktop and unfixable by the user. Submit/poll/cancel is one coherent lifecycle, not feature creep. |
| D12 | `wait=true` default, 55s budget, overridable to 300s | Tuned to the tightest real client (Claude Desktop's unconfigurable 60s ceiling) so the out-of-box config works everywhere; `medium` finishes in ~12s in practice, so the common case stays one call. |
| D13 | A blown deadline hands back an id; it never cancels | Cancel-on-timeout would destroy work already paid for. A blown budget must degrade into the async path, never into nothing. |
| D14 | Opportunistic `notifications/progress` | Sent only when the request carries a `progressToken`. Pure upside where supported, a no-op elsewhere. |
| D15 | Never claim cancellation saves money | Cancelled runs report no `usage` and no `cost` at all; the docs are silent on whether you're still billed. The tool says it stops the run and nothing more. |
| D16 | An `llm` adapter, same repo, **separate distribution** | `llm` has no MCP support (simonw/llm#696, open since Jan 2025) and `llm-perplexity` wraps only Sonar. Bands 1-3 are already a reusable Perplexity client, so the adapter is a thin second adapter rather than a fork. It ships as its own package, `llm-perplexity-agent` from `llm-plugin/`, because llm users find plugins by name — 52 of the 55 entries in llm's directory are `llm-*`, and an extra of a package called `…-mcp` is undiscoverable. Same repo, one tag, one version, exact pin: the adapter can never run against a core it was not tested against. |
| D17 | Spotlighting OFF by default in the `llm` adapter | In MCP the answer goes into a model that is holding tools, so injection can cause actions. In `llm -m perplexity-agent` it goes to a terminal for a human, and llm runs no tool loop by default — the realistic risk is a manipulated summary if piped, not a hijacked agent. `-o spotlight true` turns it on. |
| D18 | Era detection is structural and per-request, with exactly two method-level exemptions | `initialize` is ALWAYS legacy (a modern-`_meta` `initialize` would otherwise get `resultType` bolted on — a frame belonging to neither revision) and `server/discover` is ALWAYS modern, answered even with no `_meta` at all. That second one is not politeness: on stdio `server/discover` is the backward-compatibility probe, and any error reply tells a dual-era client "legacy server", which per the spec it then caches for the life of the process. A strict-looking era check there would silently cost the entire modern era. Everything else keys on `_meta.io.modelcontextprotocol/protocolVersion`. |

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
| **Modern request declaring a revision we don't speak** | **`-32022`**, `data: {supported, requested}`. The client picks from `supported` and retries; without that payload it has nothing to retry with. Applies ONLY to the modern `_meta` path — `initialize` still never errors. |
| **Modern request missing `_meta.io.modelcontextprotocol/clientCapabilities`** | **`-32602`**, `data: {missing: [...]}`. Spec-strict: a required field is missing, so the request is malformed. Checked in `dispatch()` **before** the handler runs — inside `handle_tools_call` it would land after the unknown-tool check and come back as "Unknown tool" instead. |
| Modern request omitting `protocolVersion` entirely | **Served as legacy.** Not an error: to a stateless server it is byte-identical to a legacy request, so there is nothing left to recognise it by. An honest limit of structural detection, pinned by a test so it stays a decision. |
| **Bad argument value** (empty query, bad `recency`, malformed `response_id`) | **`isError: true`** |
| Missing / empty `PERPLEXITY_API_KEY` | `isError: true` |
| Perplexity 401 / 429 / 400, timeout, network failure | `isError: true` |
| `status: "failed"` | `isError: true`, surfacing upstream `error.message` |
| **Run still in progress** (`_result`, or `_agent`'s wait budget expired) | **`isError: false`** — a legitimate state, not a failure. Returns the id, a progress summary, and the follow-up call to make. Marking it an error would invite the caller to restart the whole run. |
| Cancel on an already-terminal run (upstream 400) | **`isError: false`** — the goal state is already achieved; benign, not a failure |
| Cancel with an unknown / never-issued id | **Also upstream 400**, byte-identical to the line above. Perplexity's docs claim 404; live probing on 2026-07-23 (a never-issued UUID and a nonsense id) returned 400 with the same `"the run is already terminal"` message a real terminal run gets. **Nothing in the response distinguishes the two cases**, so the tool's wording says so rather than asserting a cancellation it cannot confirm. Do not "fix" this by inferring the difference — the information is not there. |
| Cancel with a genuinely foreign id (upstream 404, if it ever occurs) | `isError: true` |
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
pip install ruff==0.16.1 mypy==2.3.0
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
# console script over pipes, once per protocol era
uv build
uv venv /tmp/smoke
uv pip install --python /tmp/smoke/bin/python dist/*.whl

# legacy leg
printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"ci","version":"1"}}}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' \
  | /tmp/smoke/bin/perplexity-agent-mcp

# modern leg — server/discover deliberately carries no _meta, because that is
# the backward-compatibility probe a dual-era client actually sends
printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"server/discover"}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{"_meta":{"io.modelcontextprotocol/protocolVersion":"2026-07-28","io.modelcontextprotocol/clientCapabilities":{}}}}' \
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
