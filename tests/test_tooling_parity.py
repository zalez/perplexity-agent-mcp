"""pre-commit and CI must pin the SAME tool versions.

Divergent pins are how a green local run still fails CI: pre-commit's ruff
enforces a rule CI's ruff has dropped, or vice versa. Making this a test turns
a hard-won lesson into a gate.
"""

from __future__ import annotations

import pathlib
import re
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
PRE_COMMIT = REPO_ROOT / ".pre-commit-config.yaml"
CI = REPO_ROOT / ".github" / "workflows" / "ci.yml"

# Bounded forward scan for `rev:` after a `repo:` anchor line.
# Real per-repo blocks in this file are a handful of lines at most, even with
# `hooks:` (and everything nested under it) reordered ahead of `rev:`, so this
# is generous without risking a scan past the *next* repo's block -- and hence
# returning ITS rev -- when the anchored repo's own rev is genuinely absent.
_REV_SEARCH_WINDOW = 10


def _pin_from_precommit_text(text: str, tool: str) -> str | None:
    """The version pinned for `tool`'s repo block in pre-commit config text.

    Anchored to a `repo:` line naming `tool`: `re.match` only succeeds if
    the match starts at the line's first non-whitespace character, and a
    real YAML comment always starts with `#` there -- so a comment merely
    mentioning `tool` can never open with `repo:` and can never satisfy
    this. No separate comment filter is needed.

    Once anchored, `rev:` is found by scanning a bounded window of the
    following lines rather than requiring it on the immediately next line:
    pre-commit's per-repo mapping keys (`repo`, `rev`, `hooks`) are
    unordered YAML, so `hooks:` -- and everything nested under it -- may
    legally sit between `repo:` and `rev:`. The scan stops the instant it
    reaches the next `repo:` line, so a block with no `rev:` of its own
    returns `None` rather than silently reporting the *next* block's pin.

    The captured token is the full non-whitespace run after `rev:` and an
    optional `v` prefix, not a fixed `X.Y.Z` shape -- so a pre-release
    suffix like `0.16.0rc1` survives intact instead of being truncated to
    `0.16.0` and made to look like a match for the final release.
    """
    lines = text.splitlines()
    anchor = re.compile(r"-?\s*repo:\s*\S*" + re.escape(tool) + r"$")
    boundary = re.compile(r"-?\s*repo:")
    rev_line = re.compile(r"rev:\s*v?(\S+)$")
    for index, line in enumerate(lines):
        if not anchor.match(line.strip()):
            continue
        for candidate in lines[index + 1 : index + 1 + _REV_SEARCH_WINDOW]:
            stripped = candidate.strip()
            if boundary.match(stripped):
                break  # next repo block started; this one had no rev
            match = rev_line.match(stripped)
            if match:
                return match.group(1)
    return None


def _pin_from_ci_text(text: str, tool: str) -> str | None:
    """The version pinned for `tool` on CI's actual `pip install` line.

    Anchored to a real `run: pip install ...` line via `re.MULTILINE` +
    `^`: a lookalike mention elsewhere in the file, such as a comment
    reading "# TODO: bump to ruff==0.16.0", has no `run:` at its own line's
    start and so cannot satisfy the match, regardless of where it sits
    relative to the real pin.

    The captured token is the full non-whitespace run after `tool==`,
    preserving a pre-release/build suffix intact instead of truncating it
    to a bare `X.Y.Z`.
    """
    pattern = r"^[ \t]*run:[ \t]*pip install\b.*?\b" + re.escape(tool) + r"==(\S+)"
    match = re.search(pattern, text, re.MULTILINE)
    return match.group(1) if match else None


def _pinned_in_precommit(tool: str) -> str | None:
    return _pin_from_precommit_text(PRE_COMMIT.read_text(encoding="utf-8"), tool)


def _pinned_in_ci(tool: str) -> str | None:
    return _pin_from_ci_text(CI.read_text(encoding="utf-8"), tool)


class TestToolingParity(unittest.TestCase):
    def _assert_versions_match(self, precommit_tool: str, ci_tool: str) -> None:
        """Each parity test must be self-sufficient. Both sides are asserted
        non-None *here*, inside the same test that compares them, so a pin
        silently deleted from BOTH files can never produce a vacuous
        `None == None` pass -- this fails on one of the two assertIsNotNone
        calls below every time, independent of whatever else this file does
        or doesn't also check.
        """
        precommit_version = _pinned_in_precommit(precommit_tool)
        ci_version = _pinned_in_ci(ci_tool)
        self.assertIsNotNone(
            precommit_version, f"{ci_tool} pin not found in .pre-commit-config.yaml"
        )
        self.assertIsNotNone(ci_version, f"{ci_tool} pin not found in .github/workflows/ci.yml")
        self.assertEqual(precommit_version, ci_version)

    def test_ruff_versions_match(self) -> None:
        self._assert_versions_match("ruff-pre-commit", "ruff")

    def test_mypy_versions_match(self) -> None:
        self._assert_versions_match("mirrors-mypy", "mypy")

    def test_both_versions_are_actually_pinned(self) -> None:
        """Defence in depth, kept alongside -- but no longer relied on by --
        the two tests above: this checks the CI side is pinned in isolation,
        independent of the parity comparison itself.
        """
        for tool, getter in (
            ("ruff", _pinned_in_ci),
            ("mypy", _pinned_in_ci),
        ):
            with self.subTest(tool=tool):
                self.assertIsNotNone(getter(tool), f"{tool} must be pinned in CI")


class TestPinExtractionIgnoresLookalikeComments(unittest.TestCase):
    """`re.search` takes the leftmost match, so an unanchored pattern would
    prefer a comment mentioning the tool over the real pin sitting below
    it. These prove the structural anchors in `_pin_from_ci_text` /
    `_pin_from_precommit_text` aren't fooled by that.
    """

    def test_ci_ignores_a_lookalike_comment_before_the_real_pin(self) -> None:
        text = (
            "      - name: Install pinned tools\n"
            "        # TODO: bump to ruff==0.16.0\n"
            "        run: pip install ruff==0.15.22 mypy==2.3.0\n"
        )
        self.assertEqual(_pin_from_ci_text(text, "ruff"), "0.15.22")

    def test_precommit_ignores_a_lookalike_comment_before_the_real_pin(self) -> None:
        text = (
            "  # ruff-pre-commit bump plan\n"
            "  rev: v0.16.0\n"
            "  - repo: https://github.com/astral-sh/ruff-pre-commit\n"
            "    rev: v0.15.22\n"
        )
        self.assertEqual(_pin_from_precommit_text(text, "ruff-pre-commit"), "0.15.22")


class TestPinCapturePreservesPrereleaseSuffixes(unittest.TestCase):
    """Capturing a fixed `[0-9]+\\.[0-9]+\\.[0-9]+` shape would silently
    drop a 4th component or a pre-release suffix, so "0.16.0rc1" would be
    truncated to "0.16.0" and falsely reported as matching a final "0.16.0"
    release pinned in the other file.
    """

    def test_ci_capture_keeps_the_rc_suffix_intact(self) -> None:
        text = "        run: pip install ruff==0.16.0rc1 mypy==2.3.0\n"
        self.assertEqual(_pin_from_ci_text(text, "ruff"), "0.16.0rc1")

    def test_rc_pin_is_reported_as_a_mismatch_against_the_final_release(self) -> None:
        ci_text = "        run: pip install ruff==0.16.0rc1 mypy==2.3.0\n"
        precommit_text = (
            "  - repo: https://github.com/astral-sh/ruff-pre-commit\n    rev: v0.16.0\n"
        )
        ci_version = _pin_from_ci_text(ci_text, "ruff")
        precommit_version = _pin_from_precommit_text(precommit_text, "ruff-pre-commit")
        self.assertNotEqual(
            ci_version, precommit_version, "0.16.0rc1 must not be reported as matching 0.16.0"
        )


class TestPrecommitPinSearchToleratesKeyReordering(unittest.TestCase):
    """pre-commit's per-repo YAML mapping keys are unordered, so requiring
    `rev:` on the line immediately after `repo:` is a positional assumption
    that a valid reordering can defeat.
    """

    def test_rev_after_a_reordered_hooks_block_is_still_found(self) -> None:
        text = (
            "  - repo: https://github.com/astral-sh/ruff-pre-commit\n"
            "    hooks:\n"
            "      - id: ruff-check\n"
            "        args: [--fix]\n"
            "      - id: ruff-format\n"
            "    rev: v0.15.22\n"
        )
        self.assertEqual(_pin_from_precommit_text(text, "ruff-pre-commit"), "0.15.22")

    def test_a_genuinely_missing_rev_returns_none_not_the_next_blocks_rev(self) -> None:
        """The bounded window must stop at the next repo: boundary: proves
        tolerating reordering (above) doesn't also make the scan overshoot
        into a neighbouring block's pin when this one truly has none.
        """
        text = (
            "  - repo: https://github.com/astral-sh/ruff-pre-commit\n"
            "    hooks:\n"
            "      - id: ruff-check\n"
            "\n"
            "  - repo: https://github.com/pre-commit/mirrors-mypy\n"
            "    rev: v2.3.0\n"
        )
        self.assertIsNone(_pin_from_precommit_text(text, "ruff-pre-commit"))


class TestActionsArePinnedToShas(unittest.TestCase):
    def test_no_action_is_pinned_to_a_mutable_tag(self) -> None:
        """A tag can be repointed; a SHA cannot. Supply-chain hygiene."""
        text = CI.read_text(encoding="utf-8")
        for match in re.finditer(r"uses:\s*([^\s]+)", text):
            ref = match.group(1)
            with self.subTest(action=ref):
                self.assertRegex(
                    ref,
                    r"@[0-9a-f]{40}$",
                    f"{ref} must be pinned to a full commit SHA, not a tag",
                )


if __name__ == "__main__":
    unittest.main()
