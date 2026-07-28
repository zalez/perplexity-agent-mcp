<!-- mcp-name: io.github.zalez/perplexity-agent-mcp -->

# perplexity-agent-mcp

[![CI](https://github.com/zalez/perplexity-agent-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/zalez/perplexity-agent-mcp/actions/workflows/ci.yml)

A single-file, zero-third-party-dependency [MCP](https://modelcontextprotocol.io/) server for Perplexity's [Agent API](https://docs.perplexity.ai/docs/agent-api/quickstart) — multi-step web research with citations, exposed to any MCP client over stdio.

Python standard library only: no MCP SDK, no HTTP library, no code generated at build time. `perplexity_agent_mcp.py` holds your API key and talks to the network on your behalf, so it is written to be read — 1,478 lines, comments included. Both install paths below ship the exact same file; which one you pick changes how many other parties you're trusting to get it onto your disk, not what actually runs.

> **Background reading:** [*perplexity-agent-mcp: an open-source MCP server for Perplexity's Agent API*](https://constantin.glez.de/posts/2026-07-27-perplexity-agent-mcp-open-source-mcp-server-perplexitys-agent-api/) — why this exists, what the Agent API does that Sonar doesn't, and what building it turned up.

## Why this exists

Perplexity's own [`@perplexity-ai/mcp-server`](https://github.com/ppl-ai/modelcontextprotocol) wraps the Sonar chat models and the Search API. It does not reach the **Agent API** (`POST /v1/agent`) — Perplexity's multi-step, tool-using research endpoint, which runs its own web searches, fetches pages, and synthesizes one cited answer. This server fills exactly that gap. It's meant to run alongside the official one, not replace it.

## The three tools

The Agent API is asynchronous under the hood (see [Configuration](#configuration) for why that matters), so this server exposes it as one lifecycle, not one call:

| Tool | Does | Use it when |
|---|---|---|
| `perplexity_agent` | Starts a research run. By default, blocks until the answer is ready and returns synthesized, sourced text. | Your default entry point for any research question. |
| `perplexity_agent_result` | Collects a run started earlier — a one-shot progress check, or a blocking wait. | `perplexity_agent` handed back a `response_id` instead of an answer (it ran out of wait budget, or you called it with `wait: false`). |
| `perplexity_agent_cancel` | Asks Perplexity to stop a run that's no longer needed. | You started something with `wait: false` and no longer want the answer. |

<details>
<summary>Full parameters</summary>

**`perplexity_agent`**

| Param | Type | Default | Notes |
|---|---|---|---|
| `query` | string | *(required)* | The research question. |
| `preset` | string | `medium` | `fast`, `low`, `medium`, `high`, `xhigh`, `wide-research` — an open string, not validated client-side, so a preset Perplexity adds tomorrow works today without an update to this server. |
| `recency` | string | *(none)* | One of `hour`, `day`, `week`, `month`, `year`. |
| `domains` | string[] | *(none)* | Up to 20 domains. Prefix an entry with `-` to exclude it. Allowlist or denylist, not both. |
| `wait` | boolean | `true` | Block until done, or return a `response_id` immediately so you can run several deep queries in parallel. |

**`perplexity_agent_result`**

| Param | Type | Default | Notes |
|---|---|---|---|
| `response_id` | string | *(required)* | From `perplexity_agent`. |
| `wait_seconds` | integer | `0` | `0` checks once and returns immediately. A higher value blocks, but is silently **clamped** to this server's configured wait budget (see [Configuration](#configuration)) rather than rejected. |

**`perplexity_agent_cancel`**

| Param | Type | Default | Notes |
|---|---|---|---|
| `response_id` | string | *(required)* | From `perplexity_agent`. |

</details>

One deliberate asymmetry, visible in `tools/list`: `perplexity_agent`'s `readOnlyHint` annotation is `false`, even though it never touches your local machine — it creates real, billable, cancellable state on Perplexity's servers, and MCP clients use that hint to decide whether a call needs your approval before running. `perplexity_agent_cancel` carries `destructiveHint: true`, honestly: it ends a run, and Perplexity reports no usage at all for cancelled runs, so this tool cannot tell you whether cancelling changed your bill (see [Security](#security)).

## Install — Path A: single file

The whole point of this project is that you can read the one file that will hold your API key before you trust it with one. Do that first: download `perplexity_agent_mcp.py` and read it top to bottom.

Then check your interpreter. **Stock macOS ships Python 3.9.6** at `/usr/bin/python3` (confirmed on a current macOS install) — below this server's 3.10 floor. It will refuse to start and say why rather than fail obscurely, but you still want a real 3.10+ `python3` (Homebrew, python.org, or `uv python install` all provide one):

```bash
python3 --version   # must be >= 3.10
which python3       # note this path for the config below
```

Point your MCP client at the absolute paths of both the interpreter and the file:

```json
{
  "mcpServers": {
    "perplexity-agent": {
      "command": "/absolute/path/to/python3",
      "args": ["/absolute/path/to/perplexity_agent_mcp.py"],
      "env": { "PERPLEXITY_API_KEY": "pplx-…" }
    }
  }
}
```

Use absolute paths for both — the same reason Path B insists on one below applies here too: a GUI app's `PATH` is not your shell's `PATH`, and a bare `"command": "python3"` can quietly resolve to that too-old system interpreter instead of the one you meant. On macOS, Claude Desktop's config file lives at `~/Library/Application Support/Claude/claude_desktop_config.json`.

Trust chain: Python standard library, plus Perplexity. Nothing else.

## Install — Path B: `uvx` from PyPI

One JSON snippet, nothing to download by hand. `uv` fetches and runs the published package each time your MCP client starts the server.

```json
{
  "mcpServers": {
    "perplexity-agent": {
      "command": "/absolute/path/to/uvx",
      "args": ["perplexity-agent-mcp"],
      "env": { "PERPLEXITY_API_KEY": "pplx-…" }
    }
  }
}
```

Two things worth getting right, each the difference between a working config and a support thread:

> **Use an absolute path to `uvx`.** macOS GUI apps — Claude Desktop launched from Finder or Spotlight, not a terminal — do not inherit your shell's `PATH`. If the config above says `"command": "uvx"`, Claude Desktop very likely can't find it and fails with `spawn uvx ENOENT`. Run `which uvx` in your terminal and paste the absolute path it prints into `command` instead.

> **Pin the version if you want reproducibility.** `"args": ["perplexity-agent-mcp@0.3.1"]` holds you on one release. Unpinned, `uv` resolves the newest *published release* on every restart — which is a materially different risk from tracking a branch: releases are immutable, tagged, and go through the same CI as everything else. Pin if you would rather review each upgrade; leave it off if you would rather get fixes automatically. Either is defensible, which is why this is not a warning.

## Also: a plugin for `llm`

The same Perplexity Agent client, exposed as a model for Simon Willison's
[`llm`](https://llm.datasette.io) CLI. Optional — the MCP server never imports
it and keeps its zero dependencies either way.

```bash
llm install llm-perplexity-agent
llm keys set perplexity        # skip if you already set this for llm-perplexity
llm -m perplexity-agent 'What changed in MCP 2026-07-28?'
```

Options mirror the MCP tool:

```bash
llm -m perplexity-agent -o preset xhigh -o recency week 'Latest on X'
llm -m perplexity-agent -o domains 'nasa.gov,-reddit.com' 'Artemis status'
llm -m perplexity-agent -o timeout 600 'Something genuinely deep'
```

Poll progress goes to **stderr**, so the answer pipes cleanly:

```
$ llm -m perplexity-agent 'What is MCP?' > answer.md
[perplexity-agent] status queued after 1s; no intermediate results yet
[perplexity-agent] status in_progress after 3s; 10 search result(s) gathered
```

**Why not just point `llm` at the MCP server?** `llm` has no MCP support —
[simonw/llm#696](https://github.com/simonw/llm/issues/696) has been open since
January 2025. Third-party bridges exist, but the maintained ones pull in the
full MCP SDK to talk to a server that deliberately has no dependencies. And
the existing [`llm-perplexity`](https://pypi.org/project/llm-perplexity/)
plugin wraps the older Sonar chat models, not the Agent API — the same gap
this project fills for MCP.

**One deliberate difference from the MCP server: spotlighting is off by
default here.** In MCP the answer goes straight into a model that is holding
tools, so injected instructions could cause actions, and the wrapper is
essential. On the command line the answer goes to a terminal for a human to
read, and `llm` runs no tool loop by default — so the realistic risk is a
manipulated summary if you pipe it into another model, not a hijacked agent.
Turn it on with `-o spotlight true` when the output is headed somewhere that
matters:

```bash
llm -m perplexity-agent -o spotlight true 'Research X' | llm -m gpt-5 'Summarise'
```


## Trust chain: Path A vs Path B

These are not two equally convenient ways to install the same thing. Path B has a strictly larger trust surface than Path A — full stop; that's not a knock on Path B, it's the trade you make for not downloading anything by hand.

| | Path A: single file | Path B: `uvx` |
|---|---|---|
| You trust | Python standard library, Perplexity | Python standard library, Perplexity, **plus `uv`, `flit_core`, and GitHub** (at fetch time — every restart, unless pinned) |
| What runs | Exactly the bytes you read | A wheel `flit_core` builds from the tag you pinned — same source, but fetched and built on your behalf |
| Best for | Maximum auditability | Maximum convenience |

Both paths ship *the same bytes* — `flit_core` copies the one `.py` file into a wheel; nothing is generated, bundled, or vendored in between. Reading the file audits both.

## Configuration

| Variable | Required | Default | What it does |
|---|---|---|---|
| `PERPLEXITY_API_KEY` | Yes | — | Your Perplexity API key. Missing or empty → a clean tool error naming the variable, never a crash. |
| `PERPLEXITY_AGENT_WAIT_SECONDS` | No | `55` | Seconds a blocking tool call may wait before handing back a `response_id` instead of an answer. |

### Why 55

Claude Desktop enforces a tool-call timeout of **60 seconds that its users cannot configure** — no setting, no environment variable, nothing in the UI. A synchronous MCP call that runs longer simply dies from the client's point of view, while Perplexity keeps working — and billing — server-side regardless. `55` leaves this server's own submit-then-poll round trip a 5-second margin inside that hard ceiling.

If your client isn't Claude Desktop, raise it — there's no reason to hand back a `response_id` early if your client is happy to keep waiting:

```json
"env": { "PERPLEXITY_API_KEY": "pplx-…", "PERPLEXITY_AGENT_WAIT_SECONDS": "300" }
```

`300` is what we recommend for Claude Code, VS Code, and Cursor IDE — see why in the table below. An unset, empty, non-numeric, zero, or negative value silently falls back to `55`; a valid positive value is capped at `1800` (30 minutes) so a stray extra zero can't hang a call indefinitely. `perplexity_agent_result`'s own `wait_seconds` parameter advertises this same ceiling as its declared maximum in `tools/list` — but sending it a larger value anyway doesn't get rejected, it gets silently clamped to the ceiling.

No MCP client publishes Claude Desktop's figure anywhere; we obtained it by reverse-engineering the installed app (v1.24012.1) — its bundled TypeScript SDK defaults `DEFAULT_REQUEST_TIMEOUT_MSEC` to 60000ms, and Claude Desktop calls `callTool()` without overriding it. The rest of this table comes from each project's own docs or source, with confidence noted where it's shaky:

| Client | Tool-call timeout | User-configurable? | Sends a progress token? |
|---|---|---|---|
| **Claude Desktop** | **60 s** | **No** | **Never** |
| Claude Code (stdio) | ~28 h wall-clock, 30 min idle | Yes (`MCP_TOOL_TIMEOUT`, per-server `timeout`, idle var) | Yes — auto-backgrounds any call past 2 min |
| Cursor (ACP/CLI) | 60 s | No | — |
| Cursor (IDE) | ~60 min (one staff forum post, unverified) | No | — |
| VS Code / GitHub Copilot | None — waits indefinitely | n/a | Yes, shown in the UI |
| MCP TypeScript SDK | 60 s | Per-request | Only if the integrator opts in |
| MCP Python SDK | None | Yes | Not implemented |

Progress notifications do not rescue Claude Desktop: the spec lets a client reset its timeout clock when progress arrives, but only if the client supplied a `progressToken` in the first place, and Claude Desktop never does. This server's `notifications/progress` support is opportunistic — pure upside for the other clients, a no-op here.

## Presets

`preset` selects research depth. It's an open string on Perplexity's side too — their API reference types it as a plain `string`, not an enum — which is why this server doesn't constrain it either: a preset Perplexity adds after this README is written still works, unmodified. These are the ones documented today, roughly shallowest to deepest:

| Preset | Perplexity's stated intent |
|---|---|
| `fast` | Quick factual lookups, minimal latency |
| `low` | Balanced research with current information |
| `medium` (default) | Multi-step research across several sources |
| `high` | Exhaustive coverage |
| `xhigh` | Open-ended agentic research |
| `wide-research` | Building a large, evidence-backed collection of sources |

The one hard number we have: a single `medium` run — a three-source comparison query — measured **12.5 seconds and $0.039**. That's one sample, not a benchmark; different questions and deeper presets will vary, likely a lot. This server always submits in Perplexity's background mode regardless of preset — poll, don't hold a socket open for the length of a research run — which is also the mode `wide-research` requires.

See Perplexity's [presets guide](https://docs.perplexity.ai/docs/agent-api/presets) for the current, authoritative list.

## Self-test

No API key needed — `initialize` and `tools/list` never touch the network:

```bash
printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"manual","version":"1"}}}' \
  '{"jsonrpc":"2.0","method":"notifications/initialized"}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' \
  | python3 perplexity_agent_mcp.py
```

Expect exactly two JSON lines back: an `initialize` result, then all three tools from `tools/list`. Nothing for the `notifications/initialized` line — the spec forbids replying to notifications, including ones the server doesn't specifically act on.

## Security

Three things worth stating plainly rather than leaving implied.

**1. Perplexity retains your queries.** This server never sends a `store` parameter, so Perplexity's own default applies. Perplexity documents `store: false` as hiding a response from later retrieve calls — and this server's entire design is submit-then-poll-later, for every request, regardless of preset. So storage can't meaningfully be off for how this tool works: assume every query and answer is retained on Perplexity's servers, governed by their retention policy, not this project's.

**2. Returned web content is a prompt-injection vector.** Every tool here feeds text scraped from the live web to a language model. That's inherent to the category, not specific to this server, and most web-search MCP servers don't say so. Mitigation: answers are wrapped in a randomized delimiter — `<untrusted-web-content-{16 hex chars}>…</untrusted-web-content-{same}>`, 64 bits of entropy from `secrets.token_hex(8)` — with an explicit "this is data, not instructions" instruction, and any occurrence of the closing tag inside the content itself is stripped first so a hostile page can't forge its own way out. This is **spotlighting by delimiting** ([Microsoft Research, arXiv:2403.14720](https://arxiv.org/pdf/2403.14720)). It is a mitigation, not a fix: no client is obliged to honour the delimiter, and no model is guaranteed to respect it.

**3. Your API key sits in plaintext in your MCP client's config file.** The MCP specification directs stdio-transport servers to skip its OAuth flow entirely and get credentials from the environment instead, and every MCP client implements that as a plaintext `env` block in a JSON config file on disk. That's the ecosystem's normal, spec-sanctioned posture for local servers, not a shortcut this project took — but it deserves saying rather than assuming you already know it. Treat that config file like any other secret: correct file permissions, never commit it, never paste it into a support ticket.

This server has no other secret-handling surface worth auditing beyond those three: the key is read from the environment at call time, not import time, so a missing key is a clean tool error rather than a crash; it lives only inside one function's local scope; it's never logged; and there is deliberately no way to redirect the API host via an environment variable — that would itself be an exfiltration path in a tool whose entire configuration is a user-editable JSON file.

## Development

No install step — that's the point:

```bash
git clone https://github.com/zalez/perplexity-agent-mcp.git
cd perplexity-agent-mcp
python3 -m unittest discover
```

190 tests, pure standard library — `tests/fake_perplexity.py` runs a fake Perplexity over `http.server`, in-process, so nothing real is ever called.

Before committing, install the git hooks once and let them run automatically after that:

```bash
uv tool install pre-commit   # or: pip install pre-commit / pipx install pre-commit
pre-commit install
pre-commit run --all-files
```

18 hooks: file hygiene, `ruff` + `mypy --strict`, `gitleaks` and `zizmor` for secret and GitHub Actions security scanning, `codespell`, and the full test suite. CI runs the same hook set (see [`ci.yml`](.github/workflows/ci.yml)) plus a matrix across Python 3.10–3.14 and a packaging job that builds a wheel, installs it into a clean environment, and drives the console script over real pipes — a successful build alone doesn't prove the entry point works.

## More

- [Design spec](docs/specs/2026-07-22-perplexity-agent-mcp-design.md) — every
  decision with its rationale, the places the original brief turned out to be
  wrong, and the live verification results.
- [Blog post](https://constantin.glez.de/posts/2026-07-27-perplexity-agent-mcp-open-source-mcp-server-perplexitys-agent-api/)
  — the story behind the project.
- [SECURITY.md](SECURITY.md) — threat model, and an honest account of what the
  prompt-injection mitigation does and does not do.

## License

BSD-3-Clause. Copyright (c) 2026, Constantin Gonzalez. See [LICENSE](LICENSE).
