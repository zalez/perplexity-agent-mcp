# Dual-era MCP: serving `2026-07-28` and `2025-11-25` from one process

**Date:** 2026-08-01 · **Shipped in:** v0.4.0 · **Supersedes:** §3.1 and D2 of
[2026-07-22-perplexity-agent-mcp-design.md](2026-07-22-perplexity-agent-mcp-design.md)

Like its predecessor this is a **dated document**. It records the reasoning as
of the date above. Where it disagrees with `perplexity_agent_mcp.py`, the
source is correct and this is stale.

---

## 1. What changed upstream

MCP `2026-07-28` was tagged final on 2026-07-28 at 16:47 UTC (`prerelease:
false`), and the spec site's versioning page now names it the current
revision. It is the largest breaking change in the protocol's history.

What actually touches this server:

| Change | Effect here |
|---|---|
| `initialize` / `notifications/initialized` removed; protocol is stateless | New era to serve. Cost was near zero — see §2. |
| Version, capabilities and identity move to per-request `_meta` | New detection and validation path |
| `server/discover` mandatory for servers | One new handler |
| `UnsupportedProtocolVersionError`, code `-32022` | New error, with a `data` payload |
| All results carry `resultType` | Every modern result reshaped |
| `tools/list` results require `ttlMs` + `cacheScope` | Cache hints, `private` (§5) |
| `ping`, `logging/setLevel` removed | `ping` kept anyway (§6) |
| Tasks moved to an official extension | Out of scope; still deferred |
| Sessions, SSE resumability, `Mcp-Session-Id` removed | HTTP-only; irrelevant to a stdio server |
| Roots, Sampling, Logging deprecated | Never implemented; nothing to do |

## 2. The premise held

The original D2 bet that upgrading would be "additive, not a rewrite", on two
grounds: stdio framing was unchanged across revisions, and every method already
lived in a flat `HANDLERS` dict. Both held. A third, unplanned property
mattered more than either: **the server already had no session state.** No
`_initialized` flag, no session object, no gate in `dispatch()`. `tools/list`
answered without a handshake before this change, and unknown `_meta` keys were
already ignored rather than rejected.

So "make it stateless" required no work at all. What the change actually cost
was era *detection* and result *shaping* — both new, both localised to
`dispatch()`.

## 3. Era detection: three ordered rules

The spec's rule for a dual-era server is that a request carrying modern
per-request `_meta` is served statelessly, and an `initialize` request selects
legacy semantics. That is under-specified for `server/discover`, so the
precedence is written out explicitly in the source above `HANDLERS`:

1. **`initialize` → always legacy**, whatever `_meta` it carries.
2. **`server/discover` → always modern**, even with no `_meta` at all.
3. **Everything else → structural**: modern iff
   `_meta.io.modelcontextprotocol/protocolVersion` is present.

Rule 2 is the one that is easy to get wrong and expensive to get wrong.
`server/discover` is the designated stdio backward-compatibility probe: a
dual-era client sends it first and reads the reply to decide what we are. The
spec's fallback rule for clients is explicitly *not* keyed to a specific error
code — **any** error, or a timeout, means "legacy server". So classifying a
bare probe as legacy would make it fall through to `-32601`, the client would
conclude we are legacy, and per the spec it caches that verdict for the
lifetime of the process and may persist it across restarts. One strict-looking
line would cost the entire modern era, silently, with every individual
behaviour still looking correct in isolation.

Rule 1 prevents the mirror-image mistake: a purely structural rule would
classify an `initialize` carrying modern `_meta` as modern and decorate an
`InitializeResult` with `resultType` and `serverInfo` — a frame belonging to
neither revision.

### The honest limit

A modern client that omits `protocolVersion` entirely is, to a stateless
server, byte-for-byte indistinguishable from a legacy client, and is served as
one. The spec's "reject a request missing required fields with `-32602`"
therefore cannot reach that case — there is nothing left to recognise it by.
It applies to requests that *do* declare a version and then omit something
else. This is stated in the source and pinned by a test, so it stays a
decision rather than becoming a discovery.

## 4. Validation, and where it must live

Spec-strict on both required `_meta` fields, decided deliberately: no client
had shipped `2026-07-28` support when this was written, so there were no
lenient-client compatibility problems to trade against, and a leniency added
now would be invisible drift that nothing would ever revisit.

Three placement constraints, each of which produces a real bug if ignored:

- **After the notification early-returns.** Hoisting detection to the top of
  `dispatch()` makes a malformed modern *notification* emit a `-32602` carrying
  `"id": null` — a reply to a notification, which the spec forbids, in a frame
  that looks like a parse error.
- **Required-fields only, never deny-unknown.** `_meta` is an extension point.
  Rejecting unrecognised keys would reject `progressToken`, which is spelled
  bare in both eras and shares the same dict — silently ending progress
  notifications with no error and no log.
- **In `dispatch()`, not in a handler.** Inside `handle_tools_call` the check
  would sit after the unknown-tool test and beside argument validation, so a
  malformed modern call would come back as "Unknown tool", or as
  `isError: true`, instead of as the protocol error it is.

## 5. `cacheScope: "private"`, and a five-minute TTL

Both are narrower than they first look, for the same reason: `tools/list`
output is a function of *this process's* environment.
`_WAIT_SECONDS_SCHEMA_MAX` is read from `PERPLEXITY_AGENT_WAIT_SECONDS` at
import and appears both as a schema `maximum` and inside description prose. A
shared cache serving that answer to a differently-configured consumer would be
handing over a ceiling that is wrong for it — so `private`, not `public`.

And `ttlMs` is this server's **only** invalidation channel: it deliberately
does not declare `listChanged` and will never send that notification. Raise the
environment variable, restart, and a client holding a long cache would keep
advertising the old ceiling to its model. Five minutes bounds how long that
can last.

## 6. `ping` is kept

Removed from the revision, but nothing in the spec forbids answering it, and
an empty result is unambiguous. Refusing it can only break a client using it
as a liveness check, and gains nothing. Served in both eras, which also keeps
the era rules down to two exemptions rather than three.

## 7. `HANDLERS` entries became records

Era and cacheability could have lived in lookaside sets. They did not, because
the band comment above `HANDLERS` promises that reading that dict tells you
the whole surface, and two shadow registries elsewhere in the file would have
made that false. Each value is a `NamedTuple` of `(handler, era, cacheable)`,
so a method cannot be registered without answering both questions, and
`dispatch()` contains no method-name literals at all.

One consequence worth recording because it will bite again: anything
monkeypatching `HANDLERS` must now supply a record, not a bare callable.
`tests/test_no_secrets.py` did exactly that, and the resulting failure was
memorably misleading — dispatch raised `AttributeError`, the broad `except`
turned it into `-32603`, the frame count and both frame ids still matched, and
the only assertion that failed was one whose message read "this test proved
nothing".

## 8. Ecosystem state at time of writing

Recorded because it is the reason this shipped early rather than urgently, and
because it will read very differently in six months.

- **No MCP client had publicly shipped `2026-07-28` support.** Anthropic's own
  announcement was future tense ("rolling out across Claude products soon");
  Claude Code's changelog had no mention of it; Cursor, Zed and Windsurf had
  no evidence either way.
- **The SDKs had shipped.** Python SDK v2.0.0 on GA day, TypeScript v2 the day
  before.
- Simon Willison published `llm-mcp-client` 0.1a0 on 2026-07-31, which
  negotiates both eras — but documents HTTP, not stdio.

So the modern path here will not be exercised by a real client for a while.
That is fine, and was the point: the work is purely additive, the spec is
final, and a server that is ready before its clients arrive costs nothing.

## 9. Explicitly still deferred

The **Tasks extension** (`io.modelcontextprotocol/tasks`), now official rather
than experimental, and the spec-blessed form of this server's hand-rolled
submit/poll/cancel lifecycle. It would redesign the three-tool surface —
`tasks/get`, `tasks/update`, task handles — which is a different question with
a different blast radius, and one better decided against a working dual-era
baseline than tangled up with creating one.
