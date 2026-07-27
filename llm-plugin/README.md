# llm-perplexity-agent

Perplexity's [Agent API](https://docs.perplexity.ai/docs/agent-api/quickstart) — multi-step web research with citations — as a model for Simon Willison's [`llm`](https://llm.datasette.io) CLI.

```bash
llm install llm-perplexity-agent
llm keys set perplexity        # skip if you already set this for llm-perplexity
llm -m perplexity-agent 'What changed in MCP 2026-07-28?'
```

## Why

`llm-perplexity` wraps Perplexity's older Sonar chat models. This wraps the **Agent API** (`POST /v1/agent`) — the multi-step endpoint that runs its own searches, fetches pages, and synthesizes one cited answer. Different product, different endpoint.

## Options

```bash
llm -m perplexity-agent -o preset xhigh -o recency week 'Latest on X'
llm -m perplexity-agent -o domains 'nasa.gov,-reddit.com' 'Artemis status'
llm -m perplexity-agent -o timeout 600 'Something genuinely deep'
```

| Option | Default | Notes |
|---|---|---|
| `preset` | `medium` | `fast`, `low`, `medium`, `high`, `xhigh`, `wide-research`. Passed through unvalidated — Perplexity declares no enum, so a new preset works the day it ships. |
| `recency` | — | `hour`, `day`, `week`, `month`, `year` |
| `domains` | — | Comma-separated; prefix with `-` to exclude. Max 20. |
| `timeout` | `300` | Seconds before giving up. |
| `spotlight` | `false` | Wrap the answer as untrusted web content — see below. |

Poll progress goes to **stderr**, so the answer pipes cleanly.

## A note on prompt injection

Retrieved web content is untrusted, and this tool feeds it to a language model. The companion MCP server always wraps its answers in a delimiter marking them as untrusted data, because there the answer goes straight into a model that is holding tools — injected instructions could cause *actions*.

Here the answer goes to your terminal, and `llm` runs no tool loop by default, so that wrapping is **off** by default. Turn it on when the output is headed somewhere that matters:

```bash
llm -m perplexity-agent -o spotlight true 'Research X' | llm -m gpt-5 'Summarise'
```

It is a mitigation, not a fix. See [SECURITY.md](https://github.com/zalez/perplexity-agent-mcp/blob/main/SECURITY.md).

## Sibling

This is one of two adapters over the same client. The other is [`perplexity-agent-mcp`](https://github.com/zalez/perplexity-agent-mcp), an MCP server for Claude Desktop, Claude Code, VS Code and other MCP clients. Both are released together at the same version from [one repository](https://github.com/zalez/perplexity-agent-mcp).

BSD-3-Clause. Copyright (c) 2026, Constantin Gonzalez.
