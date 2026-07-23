# Contributing

Thanks for looking at this. The project's whole premise is that
`perplexity_agent_mcp.py` is short enough to read in one sitting and holds an
API key on the reader's behalf — contributions are held to that same bar.

## Setup

```bash
git clone https://github.com/zalez/perplexity-agent-mcp.git
cd perplexity-agent-mcp
python3 -m unittest discover
```

No install step. That's not an oversight — it's the point. The server has
zero runtime dependencies, and the test suite (`tests/fake_perplexity.py`
fakes the upstream with the standard library's own `http.server`, in-process)
has none either, so `git clone` and a stdlib `unittest` invocation are
sufficient. You need a real Python **3.10 or newer** — stock macOS ships 3.9.6
at `/usr/bin/python3`, which is too old; see the README's install section.

Then, once:

```bash
uv tool install pre-commit   # or: pip install pre-commit / pipx install pre-commit
pre-commit install
```

That wires pre-commit into `.git/hooks`, so it runs automatically on every
commit from here on.

**pre-commit and CI are required to pin identical tool versions.** `ruff` and
`mypy` are pinned in both `.pre-commit-config.yaml` and
`.github/workflows/ci.yml`, and `tests/test_tooling_parity.py` fails the
build if the two ever disagree. This exists because divergent pins are
exactly how a change passes locally and then fails CI, or the reverse — it
happened once already on this project, which is why it's a test now instead
of a memory. If you bump one file's pin, bump the other in the **same PR**.

## The invariants a PR must not break

These come from the implementation plan's Global Constraints and apply to
every change, not just the task that first established them. Each was
re-checked against the current source while writing this file — see the
parenthetical after each one.

- **Zero third-party runtime dependencies.** stdlib only. No
  `requirements.txt`, no MCP SDK, no `pip install` for users. Enforced by
  `tests/test_no_dependencies.py`. *(Verified: the server's only imports are
  `__future__`, `sys`, `io`, `json`, `os`, `random`, `re`, `secrets`, `ssl`,
  `time`, `urllib.error`, `urllib.request`, `collections.abc`, `typing` — all
  stdlib, all in the test's allowlist.)*
- **Python floor `>=3.10`.** No `match` statements, no `tomllib` import in the
  server itself (that's 3.11+; only the *test suite* uses it, guarded by
  `skipIf`), no PEP 695 generics. Develop on 3.14; CI matrix covers
  3.10–3.14. *(Verified: no `match`/`case` or `type X = ...` statement
  anywhere in `perplexity_agent_mcp.py`; the version guard at the top of the
  file checks `sys.version_info < (3, 10)` before anything else runs.)*
- **Single outbound host: `https://api.perplexity.ai`, hardcoded.** No
  environment-variable, tool-argument, or config-file override — see
  [SECURITY.md](SECURITY.md#network) for why a configurable base URL would be
  an exfiltration vector. *(Verified: `os.environ` is read in exactly two
  places in the whole file, for `PERPLEXITY_API_KEY` and
  `PERPLEXITY_AGENT_WAIT_SECONDS` — nothing resembling a base-URL override.)*
- **The API key is never printed, logged, echoed, or attached to an
  exception.** Read from `PERPLEXITY_API_KEY` at call time, not import time.
  See [SECURITY.md](SECURITY.md#key-handling) for the full argument and the
  tests that enforce it.
- **stdout is exclusively JSON-RPC.** All logging goes to stderr.
  `sys.stdout` is rebound to `sys.stderr` at startup, specifically so a stray
  `print()` anywhere in the process — yours, a future contributor's, a
  library's — lands harmlessly on stderr instead of corrupting the protocol
  stream. Don't remove that rebind, and don't add a legitimate reason to
  write to the real stdout outside of `_write()`.
- **MCP protocol revision `2025-11-25`.** Accept `2025-11-25`, `2025-06-18`,
  and `2025-03-26`; echo the client's version back on a match, otherwise
  return `2025-11-25`. **Never error on version negotiation** — the spec is
  explicit that a server must answer with a version it supports, not an
  error.
- **Validation errors are `isError: true`, never JSON-RPC `-32602`.** Since
  MCP `2025-11-25` (SEP-1303), a bad tool argument is something the calling
  model should be able to read and self-correct from, not a protocol-level
  failure. See [CLAUDE.md](CLAUDE.md#3-error-handling) for the full table of
  what goes where.
- **Line length 100. Ruff-formatted. `mypy --strict` clean. Every public
  function annotated.** `ruff check . && ruff format --check . && mypy
  --strict perplexity_agent_mcp.py` should be clean before you open a PR.
- **Comment density is high by design.** This file's product is
  auditability — a reader has to be able to follow it top to bottom without
  jumping elsewhere to understand *why* a line exists. If your change adds
  logic that isn't self-evidently correct on sight, it needs a comment
  explaining the reasoning, not just what the code does.
- **Copyright `(c) 2026, Constantin Gonzalez`. License: BSD-3-Clause.**
  Don't alter [LICENSE](LICENSE) or the copyright line without talking to the
  maintainer first.
- **Never claim cancellation reduces the bill** — not in code comments, not
  in a tool description, not in any doc. Perplexity reports no `usage` and no
  `cost` at all for a cancelled run, so "this saved you money" would be an
  invented claim. `tests/test_perplexity_client.py`'s
  `test_cancel_never_mentions_billing` enforces the tool-facing half of this
  by asserting the cancel tool's return message never contains "bill",
  "cost", "charge", "refund", "money", or "save".

## How to add a stdlib import

`tests/test_no_dependencies.py` walks the server's AST and checks every
import against an explicit `ALLOWED` frozenset. If your change needs a new
standard-library module:

1. Add it to `ALLOWED` in `tests/test_no_dependencies.py`, in the same PR as
   the code that uses it — not as a drive-by in an unrelated change.
2. Say why in the PR description. "Needed `X` for `Y`" is enough; the point
   is that it shows up in review, not that it's a large justification.
3. Don't add a module you're not using yet "for later" —
   `test_allowlist_is_actually_stdlib` guards against non-stdlib names
   sneaking in, but nothing stops the allowlist from accumulating unused
   entries except review attention.

If what you need genuinely isn't in the standard library, it almost
certainly doesn't belong in `perplexity_agent_mcp.py` at all — that's the
zero-dependency constraint above, not a suggestion.

## Running things locally

```bash
python3 -m unittest discover -v   # the whole suite; no network access needed
ruff check .
ruff format --check .
mypy --strict perplexity_agent_mcp.py
pre-commit run --all-files        # all 18 hooks: file hygiene, ruff, mypy,
                                   # gitleaks, actionlint, zizmor, codespell,
                                   # and the unittest suite again
```

**A green `pre-commit run --all-files` is necessary but not sufficient.**
Two things CI checks that pre-commit never will, on this repo specifically:

- **The Python version matrix.** Pre-commit's local `unittest` hook runs on
  whichever `python3` is on your `PATH` — one interpreter. CI's `test` job
  runs the identical command across Python 3.10, 3.11, 3.12, 3.13, and 3.14.
  If you have more than one of those installed, run the suite on a couple of
  them, especially the floor (3.10) if your change touches anything
  version-sensitive.
- **That the package actually builds and runs.** Pre-commit never invokes
  `uv build`. CI's `package` job does: builds a wheel, installs it into a
  clean virtualenv, and drives the real `perplexity-agent-mcp` console script
  over pipes with a live `initialize` / `tools/list` exchange. A successful
  `pre-commit run` says nothing about whether `pyproject.toml`,
  `__version__`, or the entry point still work — if your change touches any
  of those, run `uv build` yourself and smoke-test the result.

See [CLAUDE.md](CLAUDE.md#5-exact-local-commands) for the exact command each
CI job runs, kept verbatim so you can reproduce any of them individually.

## Release process

1. Bump `__version__` in `perplexity_agent_mcp.py`. This is the **single
   source of truth** — `pyproject.toml` declares `version` as `dynamic`, and
   `flit_core` reads it straight out of the module at build time. There is
   nowhere else to change it.
2. Add an entry to [CHANGELOG.md](CHANGELOG.md) (Keep a Changelog format)
   describing what's in the release.
3. Commit those two changes.
4. Tag the commit `vX.Y.Z` and push the tag:
   ```bash
   git tag -a vX.Y.Z -m "vX.Y.Z"
   git push origin vX.Y.Z
   ```
5. Cut a GitHub release from that tag, with release notes summarizing the
   CHANGELOG entry.

**Tags are load-bearing, not bookkeeping.** The README's `uvx` install path
has users point their MCP client config at
`git+https://github.com/zalez/perplexity-agent-mcp@vX.Y.Z` — a tag, pinned,
in a config file they may not touch again for months. `uv` re-resolves an
*unpinned* `git+https://...` reference to whatever is on `main` on every
single client restart, including anything pushed after a repo compromise —
which is exactly the risk the README's trust-chain table warns about. Once a
tag is pushed and someone may have pinned to it, treat it as immutable:
don't force-move it, don't delete it, don't repoint it at a different commit.
If a release was wrong, ship a new version instead of rewriting an old tag.
