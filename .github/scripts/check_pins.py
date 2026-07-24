#!/usr/bin/env python3
"""Report dev-tool pins that have fallen behind their upstream latest release.

This repo pins every development tool: the hooks in `.pre-commit-config.yaml`
by `rev:`, and `ruff` / `mypy` / `pre-commit` by version string in
`.github/workflows/ci.yml`. Dependabot covers GitHub Actions and nothing else,
because there are no runtime dependencies to watch. So without this, a pin only
gets updated when somebody happens to look — and a stale security scanner
reports clean, which is indistinguishable from a clean repo.

**Deliberately does not use `pre-commit autoupdate`.** That resolves each hook
to the newest tag reachable from the upstream default branch, which is not the
same thing as the newest release. Run against this config on 2026-07-23 it
proposed `gitleaks v8.30.1 -> v8.30.0` — a downgrade of a secret scanner —
because upstream cut v8.30.1 on a commit that diverged from `master`. This
script asks for the latest *release* instead, which is what a human checks.

Exits 0 whether or not anything is stale; the caller reads `stale` from
`$GITHUB_OUTPUT` (or just reads the report). A network failure for one tool is
reported as "unknown" rather than failing the run, so one flaky endpoint cannot
turn into a false all-clear or a red workflow.

Run locally with:  GITHUB_TOKEN=$(gh auth token) python3 .github/scripts/check_pins.py
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import sys
import urllib.error
import urllib.request

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
PRE_COMMIT = REPO_ROOT / ".pre-commit-config.yaml"
CI = REPO_ROOT / ".github" / "workflows" / "ci.yml"

_TIMEOUT = 30
_USER_AGENT = "perplexity-agent-mcp-pin-check"

# The one pin that is a PyPI package rather than a GitHub repo.
_PRE_COMMIT_PYPI = "pre-commit (PyPI)"

# `repo:` followed within a few lines by `rev:`. Tolerant of key order the way
# tests/test_tooling_parity.py is, so a valid reshuffle does not silently
# return nothing.
_HOOK_RE = re.compile(
    r"repo:\s*https://github\.com/(?P<slug>[\w.-]+/[\w.-]+)\s*\n"
    r"(?:[^\n]*\n){0,6}?\s*rev:\s*(?P<rev>\S+)",
)


def _get_json(url: str) -> dict[str, object] | list[object] | None:
    """Fetch and parse JSON, or return None on any failure."""
    request = urllib.request.Request(url)  # noqa: S310 - literal https URLs only
    request.add_header("User-Agent", _USER_AGENT)
    request.add_header("Accept", "application/vnd.github+json")
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token and "api.github.com" in url:
        request.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:  # noqa: S310
            parsed = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
        return None
    if isinstance(parsed, (dict, list)):
        return parsed
    return None


def github_latest(slug: str) -> str | None:
    """Newest published release for a GitHub repo, falling back to its tags.

    Some upstreams (notably `pre-commit/mirrors-mypy`) publish tags but never
    cut releases, so `/releases/latest` 404s for them and the tag list is the
    only signal available.
    """
    release = _get_json(f"https://api.github.com/repos/{slug}/releases/latest")
    if isinstance(release, dict):
        name = release.get("tag_name")
        if isinstance(name, str) and name:
            return name
    tags = _get_json(f"https://api.github.com/repos/{slug}/tags")
    if isinstance(tags, list) and tags:
        first = tags[0]
        if isinstance(first, dict):
            name = first.get("name")
            if isinstance(name, str) and name:
                return name
    return None


def pypi_latest(package: str) -> str | None:
    """Newest version of a package on PyPI."""
    payload = _get_json(f"https://pypi.org/pypi/{package}/json")
    if isinstance(payload, dict):
        info = payload.get("info")
        if isinstance(info, dict):
            version = info.get("version")
            if isinstance(version, str) and version:
                return version
    return None


def _normalise(version: str) -> str:
    """Compare `v1.2.3` and `1.2.3` as the same thing."""
    return version.lstrip("vV")


def parse_pins() -> list[tuple[str, str, str]]:
    """Every pin as (name, pinned_version, where_to_fix). No network.

    Split out from `collect` so it can be tested offline — see
    `tests/test_pin_check.py`. If a reformat of either file ever breaks these
    regexes, this returns fewer rows than expected and the report would
    cheerfully say everything is current. A silent all-clear is the worst
    failure this script has, so the count is asserted in a test.
    """
    rows: list[tuple[str, str, str]] = []

    for match in _HOOK_RE.finditer(PRE_COMMIT.read_text(encoding="utf-8")):
        slug = match.group("slug")
        # ruff and mypy are pinned in BOTH files and kept identical by
        # tests/test_tooling_parity.py, so bumping either needs both edited.
        both = slug in {"astral-sh/ruff-pre-commit", "pre-commit/mirrors-mypy"}
        where = ".pre-commit-config.yaml + ci.yml" if both else ".pre-commit-config.yaml"
        rows.append((slug, match.group("rev"), where))

    # `pre-commit` itself is pip-installed in CI and is covered by neither
    # Dependabot nor the parity test — the one pin with no guard at all.
    pinned = re.search(r"pip install pre-commit==(\S+)", CI.read_text(encoding="utf-8"))
    if pinned:
        rows.append((_PRE_COMMIT_PYPI, pinned.group(1), "ci.yml"))

    return rows


def collect() -> list[tuple[str, str, str | None, str]]:
    """Every pin, as (name, pinned, latest_or_None, where_to_fix)."""
    rows: list[tuple[str, str, str | None, str]] = []
    for name, pinned, where in parse_pins():
        latest = pypi_latest("pre-commit") if name == _PRE_COMMIT_PYPI else github_latest(name)
        rows.append((name, pinned, latest, where))
    return rows


def render(rows: list[tuple[str, str, str | None, str]]) -> tuple[str, bool]:
    """Build the markdown report and say whether anything is stale."""
    stale = [
        (name, pin, latest, where)
        for name, pin, latest, where in rows
        if latest and _normalise(pin) != _normalise(latest)
    ]
    unknown = [name for name, _, latest, _ in rows if latest is None]

    lines: list[str] = []
    if stale:
        lines.append("| Tool | Pinned | Latest release | Update in |")
        lines.append("|---|---|---|---|")
        for name, pin, latest, where in stale:
            lines.append(f"| `{name}` | `{pin}` | **`{latest}`** | `{where}` |")
        lines.append("")
        lines.append(
            "Bump by hand rather than with `pre-commit autoupdate` — it resolves to the "
            "newest tag on the upstream default branch, not the newest release, and has "
            "proposed a **downgrade** of `gitleaks` on this repo before."
        )
        lines.append("")
        lines.append(
            "`ruff` and `mypy` live in two files and must move together, or "
            "`tests/test_tooling_parity.py` fails — which is the gate working."
        )
    else:
        lines.append("All dev-tool pins match their upstream latest release.")

    if unknown:
        lines.append("")
        lines.append(
            "Could not reach upstream for: " + ", ".join(f"`{n}`" for n in unknown) + ". "
            "Treated as unknown, not as current."
        )

    return "\n".join(lines), bool(stale)


def main() -> int:
    rows = collect()
    report, stale = render(rows)

    print(report)

    # The report itself goes to stdout, so the caller can redirect it wherever
    # it likes; only the yes/no needs the Actions output channel.
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as handle:
            handle.write(f"stale={'true' if stale else 'false'}\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
