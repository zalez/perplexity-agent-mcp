"""The README's install commands must pin the version this repo actually ships.

`README.md` tells people to pin a tag rather than track `main`, and that advice
is only useful if the tag it shows is the current one. A stale pin silently
installs an older release — for v0.1.0 that would have meant no redirect
refusal, an unsafe cancel message, and ambiguous citations — while the README
asserts, in prose, that it is current.

This drifted at the very first release: v0.2.0 shipped with two `@v0.1.0` pins
still in the file, one of them under the words "the one this repository ships
as of this README". Nothing in the release process would have caught it. So,
as with `tests/test_tooling_parity.py`, the lesson becomes a gate instead of a
memory.

Deliberately scoped to README.md. `docs/specs/` is a dated design document, not
somewhere to copy an install command from, and uses a `@vX.Y.Z` placeholder so
it stays off the drift surface entirely. CHANGELOG.md is a historical record
whose old entries are *supposed* to name old versions.
"""

from __future__ import annotations

import pathlib
import re
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
README = REPO_ROOT / "README.md"
SERVER = REPO_ROOT / "perplexity_agent_mcp.py"

# `@v1.2.3` in a git URL or prose. The placeholder form `@vX.Y.Z` deliberately
# does not match — it names no release, so it cannot go stale.
_PIN_RE = re.compile(r"@v(?P<version>\d+\.\d+\.\d+)")


def _shipped_version() -> str:
    """`__version__` read from source, without importing the module."""
    match = re.search(
        r"^__version__\s*=\s*[\"'](?P<version>[^\"']+)[\"']",
        SERVER.read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    if match is None:  # pragma: no cover - would mean the server lost its version
        raise AssertionError("could not find __version__ in perplexity_agent_mcp.py")
    return match.group("version")


class TestReadmeVersionPins(unittest.TestCase):
    def test_every_pin_matches_the_shipped_version(self) -> None:
        shipped = _shipped_version()
        text = README.read_text(encoding="utf-8")
        for match in _PIN_RE.finditer(text):
            line = text.count("\n", 0, match.start()) + 1
            with self.subTest(line=line, pinned=match.group("version")):
                self.assertEqual(
                    match.group("version"),
                    shipped,
                    f"README.md:{line} pins @v{match.group('version')} but this "
                    f"repository ships {shipped}. Bump the README in the same "
                    f"commit as __version__, or users install the wrong release.",
                )

    def test_the_readme_actually_contains_pins_to_check(self) -> None:
        """Guard the guard.

        A test that iterates over nothing passes. If the install snippets are
        ever reworded such that this regex stops matching, this fails loudly
        rather than reporting a silent all-clear on zero findings.
        """
        pins = _PIN_RE.findall(README.read_text(encoding="utf-8"))
        self.assertGreaterEqual(
            len(pins),
            2,
            "expected at least the MCP and llm install pins in README.md; "
            "if the snippets changed shape, update _PIN_RE to match",
        )


if __name__ == "__main__":
    unittest.main()
