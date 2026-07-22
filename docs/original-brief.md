# Delegation Brief — Zero-Dependency MCP Server for the Perplexity Agent API

> **How to use this brief:** Paste it into a fresh Claude Code session in an empty directory (e.g. `~/tools/perplexity-agent-mcp/`). Claude Code should first **verify the live API against the doc links in the Toolbox**, then implement. Everything needed is below — the brief is self-contained.

---

## Intent

Build a **single-file, zero-third-party-dependency MCP server** that gives an MCP client (Claude Desktop / Cowork / Claude Code) one tool to run research through **Perplexity's Agent API** (`POST /v1/agent`) and get back a synthesized, cited answer.

Success looks like: I drop one Python file into my MCP config, restart the client, and a `perplexity_agent` tool appears that round-trips a query to Perplexity's agent and returns the answer plus its sources. I can **read the entire file in five minutes and trust it** — that auditability is the whole point of doing this instead of pulling an npm package. The server is the trust boundary, so it must have *no* foreign supply chain to attack.

## Context

- The existing Perplexity MCP on this machine (`@perplexity-ai/mcp-server`) only wraps the **Sonar** chat models \+ Search API. It does **not** reach the Agent API. This new server fills exactly that gap and runs *alongside* it.
- **Critical endpoint fact — do not get this wrong:** The Agent API is reached at `POST https://api.perplexity.ai/v1/agent`. Perplexity's OpenAI-compatibility alias is the **Responses API** (`/v1/responses`), *not* Chat Completions. A normal `chat.completions` call routes to Sonar, **not** the agent. To avoid this trap entirely, **call the raw `/v1/agent` endpoint directly** — do not build on any OpenAI chat-completions abstraction.
- Security posture is the reason this is bespoke: the API key must live **only** as an environment variable in the MCP server's config, never in code, logs, or model context.
- The owner is a senior engineer and will code-review the file before running it. Optimize for a reviewer who wants to be *certain* the file does nothing surprising.

## Deliverables

1. **`perplexity_agent_mcp.py`** — the server. Python 3.9+, **standard library only**. Single file, heavily commented, readable top-to-bottom, target \< \~180 lines.
2. **A short `README.md`** (or a comment header block) with: what it does, the required env var, how to run it, and the exact client config snippet (below).
3. **A self-test recipe** — a copy-pasteable shell snippet that pipes a manual JSON-RPC `initialize` → `tools/list` → `tools/call` sequence into the server and shows it working, so the server can be validated without any client.

## Navigation

### Tenets (decide ties this way)

- **Auditability over cleverness.** Boring, obvious, linear code beats abstraction.
- **Zero dependencies over convenience.** If a stdlib solution exists, use it. No `pip install`.
- **Fail loud and safe over silent partial success.** Surface errors as clean MCP tool errors; never leak the key or a raw stack trace into model-visible output.
- **Verify against live docs over trusting this brief.** API field names and model IDs drift; this brief is a July-2026 snapshot. Confirm before coding.

### Guardrails (hard constraints — non-negotiable)

- **Stdlib only:** `json`, `os`, `sys`, `urllib.request`/`urllib.error`, `typing`. **No** third-party packages, **no** `requirements.txt`, **no** `package.json`, **no** MCP SDK.
- **Single outbound host:** `https://api.perplexity.ai` only, HTTPS with default certificate verification **on**. No other network calls, no telemetry, no analytics, no auto-update.
- **Key handling:** read from env `PERPLEXITY_API_KEY`. If unset/empty, return a clear tool error. **Never** print, log, or echo the key or the `Authorization` header.
- **Timeouts:** set a request timeout (\~120 s — the `high`/`xhigh` presets are slow). On timeout or non-2xx, return a readable error, not a crash.
- **No global state / no files written** beyond stdout/stderr. stderr may carry minimal, key-free diagnostic logging; **stdout is reserved exclusively** for JSON-RPC frames.

## Functional spec

### The tool

Expose **one** tool (keep it lean; more can come later):

- **name:** `perplexity_agent`
- **description:** "Run a research query through Perplexity's Agent API (multi-step web research with citations). Use for deep or multi-hop questions where a single synthesized, sourced answer is wanted."
- **inputSchema** (JSON Schema):
  - `query` *(string, required)* → maps to the request `input`.
  - `preset` *(string, optional, enum: `fast|low|medium|high|xhigh`, default `medium`)* → the research-depth preset.
  - `model` *(string, optional)* → override the model; omit to let the preset decide.
- **Behavior:** POST to `/v1/agent` with `{ input, preset, model? }` and web search enabled (that's the point of the agent). Return the synthesized answer text **plus a compact list of the cited sources** (title \+ URL) as MCP text content.

### Request/response mapping (verify field names live — see Toolbox)

- **Request:** `POST /v1/agent`, headers `Authorization: Bearer <key>` and `Content-Type: application/json`. Body at minimum `{"model": "<id>", "input": "<query>"}`; add the preset and enable the built-in `web_search` tool per the current docs. Built-in tools available include `web_search`, `fetch_url`, `finance_search`, `people_search`.
- **Response:** a typed `output` array plus an `output_text` convenience field and citations. Extract `output_text` for the answer; extract the citation/source list for the sources block. Handle the case where `output_text` is absent by falling back to concatenating the text items in `output`.
- **Model IDs drift.** Prefer relying on the `preset` and leaving `model` unset by default. If a default model must be named, fetch a current valid ID from `GET /v1/models` rather than hardcoding a guess.

### MCP protocol (stdio JSON-RPC 2.0 — implement by hand)

Transport is **newline-delimited JSON** over stdin/stdout (one JSON-RPC message per line, UTF-8, no embedded newlines). Implement exactly these methods:

- **`initialize`** → respond with the negotiated `protocolVersion` (echo the client's requested version if you support it, else your latest), `capabilities: { "tools": {} }`, and `serverInfo` (name \+ version).
- **`notifications/initialized`** → no-op (it's a notification; send no response).
- **`tools/list`** → return `{ "tools": [ <the tool schema above> ] }`.
- **`tools/call`** → params `{ name, arguments }`. Run the query; return `{ "content": [ { "type": "text", "text": "<answer + sources>" } ], "isError": <bool> }`.
- **`ping`** → respond with an empty result.
- Unknown method → return a proper JSON-RPC error object (code `-32601`).
- Notifications (no `id`) get **no** response. Requests (with `id`) always get one response.

Reference: MCP spec at [https://modelcontextprotocol.io](https://modelcontextprotocol.io) (schema/basic lifecycle). Match the current protocol revision the doc lists.

## Toolbox (read these before coding — they are the source of truth over this brief)

- Agent API quickstart: [https://docs.perplexity.ai/docs/agent-api/quickstart](https://docs.perplexity.ai/docs/agent-api/quickstart)
- **Create Agent Response** (exact request/response schema — WebFetch this and confirm field names): [https://docs.perplexity.ai/api-reference/agent-post](https://docs.perplexity.ai/api-reference/agent-post)
- OpenAI compatibility (why `/v1/agent`, not chat.completions): [https://docs.perplexity.ai/docs/agent-api/openai-compatibility](https://docs.perplexity.ai/docs/agent-api/openai-compatibility)
- Agent API models \+ presets: [https://docs.perplexity.ai/docs/agent-api/models](https://docs.perplexity.ai/docs/agent-api/models)
- Built-in tools (web\_search etc.): [https://docs.perplexity.ai/docs/agent-api/tools/web-search](https://docs.perplexity.ai/docs/agent-api/tools/web-search)
- List models endpoint: `GET https://api.perplexity.ai/v1/models`
- MCP spec: [https://modelcontextprotocol.io](https://modelcontextprotocol.io)

## Acceptance criteria

1. `PERPLEXITY_API_KEY=… python3 perplexity_agent_mcp.py` starts and blocks on stdin.
2. The self-test recipe drives `initialize` → `notifications/initialized` → `tools/list` → `tools/call` and shows: a valid initialize result, the `perplexity_agent` tool in the list, and a `tools/call` that returns real answer text \+ at least one source URL.
3. Missing/empty `PERPLEXITY_API_KEY` → clean tool error, no traceback, no secret leak.
4. A non-2xx from Perplexity (e.g. bad model id) → readable error content, server stays alive.
5. `grep`\-ing the file shows **no** third-party imports and the key is never written to stdout or any log.
6. Dropped into the client config below, the tool appears and answers a live query.

### Client config snippet (put in the brief's README)

```json
{
  "mcpServers": {
    "perplexity-agent": {
      "command": "python3",
      "args": ["/ABSOLUTE/PATH/TO/perplexity_agent_mcp.py"],
      "env": { "PERPLEXITY_API_KEY": "pplx-…" }
    }
  }
}
```

## Notes / open choices (flagged for the owner)

- **Language:** Python stdlib chosen for the cleanest zero-dependency single file. A Node built-ins-only version (`https` \+ `readline`, no `npm install`) is equally valid if preferred — say so and it's a straight swap.
- **Tool surface:** one `perplexity_agent` tool proposed to stay lean. Easy follow-ons if wanted: a `finance_search`/`people_search`\-focused variant, or exposing `recency` and `search_domain_filter`.
- **Billing:** the Agent API is pay-as-you-go on the same Perplexity API account/key as the existing Sonar MCP; ensure the account has credits. Independent of the (cancelled) Pro sub.
