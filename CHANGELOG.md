# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

Nothing yet.

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

[Unreleased]: https://github.com/zalez/perplexity-agent-mcp/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/zalez/perplexity-agent-mcp/releases/tag/v0.1.0
