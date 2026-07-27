# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

Nothing yet.

## [0.3.0] - 2026-07-27

### Added

- **Both packages are now on PyPI**, so installing no longer means naming a git
  URL:
  - `uvx perplexity-agent-mcp` — the MCP server
  - `llm install llm-perplexity-agent` — the `llm` CLI plugin
- **Listed in the [MCP Registry](https://registry.modelcontextprotocol.io)** as
  `io.github.zalez/perplexity-agent-mcp`. The community server list at
  `modelcontextprotocol/servers` has been retired in favour of it, and the
  registry only accepts packages from a trusted index — which is what made
  publishing a prerequisite rather than a preference.
- A tag-triggered publish workflow. It builds once, verifies, then publishes
  the server, the adapter, and the registry entry in that order, because each
  depends on the one before. No API token is stored anywhere: PyPI uses
  Trusted Publishing and the registry uses OIDC, so both mint short-lived
  credentials from the workflow's own identity.

### Changed

- **The `llm` adapter is its own distribution**, `llm-perplexity-agent`, built
  from `llm-plugin/`. It was previously an `[llm]` extra of this package —
  which meant nobody searching for an `llm` plugin could find it, since
  `llm install llm-<name>` is the convention 52 of the 55 entries in llm's
  own plugin directory follow. Both are released together from one tag at one
  version, and the adapter pins the server exactly, so there is no version
  skew to reason about.
- The server declares **no optional dependencies at all** again, so installing
  it pulls in nothing under any combination of extras.

### Note

`perplexity-agent-mcp` and `llm-perplexity-agent` publish from separate GitHub
environments (`pypi` and `pypi-llm`). PyPI identifies a Trusted Publisher by
repository owner, repository name, workflow filename and environment — two
packages sharing all four collide, and the second registration is refused.

## [0.2.0] - 2026-07-24

### Added

- **An optional `llm` plugin.** `llm -m perplexity-agent 'your question'`
  exposes the Perplexity Agent API as a model for Simon Willison's
  [`llm`](https://llm.datasette.io) CLI, with `-o preset`, `-o recency`,
  `-o domains`, `-o timeout` and `-o spotlight`. Install with the `llm` extra;
  the MCP server never imports it and still has zero runtime dependencies.
  It exists because `llm` has no MCP support (simonw/llm#696, open since
  January 2025) and `llm-perplexity` wraps only the older Sonar chat models.
- Poll progress on stderr during a run, so a long query does not look hung and
  the answer still pipes cleanly.

### Changed

- **The server no longer claims stdout when merely imported.** Rebinding
  `sys.stdout` moved from import time into `main()`. Importing the module used
  to redirect the whole importing program's output to stderr — harmless for
  the server, fatal for anything reusing the client, including the new
  adapter. The guarantee for the server itself is unchanged: `main()` claims
  stdout before any protocol traffic moves.
- **Build backend is now `setuptools`, was `flit_core`.** flit builds exactly
  one module per distribution and this project now ships two. setuptools is
  the only backend that keeps the property flit was chosen for — one package,
  zero transitive dependencies. `__version__` remains the single source of
  truth.
- Source URLs now keep both ends when truncated, so two long citations from
  the same site no longer render identically.

### Fixed

- `perplexity_agent_cancel`'s message no longer implies a cancellation it
  cannot confirm: Perplexity returns an identical HTTP 400 whether a run had
  already finished or the id never existed.

## [0.1.0] - 2026-07-23

Initial release.

### Added

- `perplexity_agent`, `perplexity_agent_result`, and `perplexity_agent_cancel`
  — a three-tool lifecycle (start / collect / abandon) wrapping Perplexity's
  Agent API. A single synchronous tool call is dead on arrival on Claude
  Desktop's fixed, unconfigurable 60-second tool-call timeout, so every
  research run submits in Perplexity's background mode and is polled, with
  the option to hand back a `response_id` instead of blocking.
- `preset`, `recency`, and `domains` parameters on `perplexity_agent`.
  `preset` is passed through unvalidated — Perplexity's own schema declares
  it as an open string, not an enum — so a preset added upstream after this
  release works without a server update.
- A configurable wait budget (`PERPLEXITY_AGENT_WAIT_SECONDS`, default `55`,
  clamped to `1800`) tuned to Claude Desktop's timeout, with `300` documented
  as the recommended value for clients that tolerate longer waits (Claude
  Code, VS Code, Cursor IDE).
- Opportunistic `notifications/progress` support when the calling client
  supplies a `progressToken` — a no-op everywhere else.
- Prompt-injection mitigation: every answer and its source list are wrapped
  in a randomized, per-response delimiter before being returned ("spotlighting
  by delimiting" — see [SECURITY.md](SECURITY.md#prompt-injection) for what
  this does and does not protect against).
- Zero third-party runtime dependencies — Python standard library only,
  mechanically enforced by a test rather than left as an unverified claim.
- Two install paths, documented with an explicit trust-chain comparison
  rather than presented as equivalent: a single auditable `.py` file, or
  `uvx` from a version-pinned git tag.
- CI matrix across Python 3.10–3.14, plus dedicated lint, pre-commit
  (secret scanning and GitHub Actions security auditing), and
  packaging/smoke-test jobs — 8 jobs total, all green on GitHub-hosted
  runners.
- 158 tests, stdlib `unittest` only, requiring no install step for
  contributors either.

[Unreleased]: https://github.com/zalez/perplexity-agent-mcp/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/zalez/perplexity-agent-mcp/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/zalez/perplexity-agent-mcp/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/zalez/perplexity-agent-mcp/releases/tag/v0.1.0
