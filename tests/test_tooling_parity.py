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


def _pins_from_ci_text(text: str, tool: str) -> list[str]:
    """EVERY version pinned for `tool` on one of CI's `pip install` lines.

    Plural, and both halves of that are corrections of a real failure.

    This returned only the FIRST match until 2026-08-20, and its pattern
    additionally required `run:` and `pip install` on the same physical line.
    `ci.yml` installs mypy twice — the `lint` job on a single-line `run:`, and
    the `llm adapter` job inside a `run: |` block, where `pip install` sits on
    a line of its own. So the second pin was not merely deprioritised, it was
    never matched at all, and this gate had never once compared it. A mypy
    bump on 2026-08-17 duly updated one and left the other behind, green.

    Anchoring survives the widening: a line's first non-whitespace must be
    `pip install` or `run: pip install`, so a comment mentioning a version —
    "# TODO: bump to ruff==0.16.0" — still cannot match, wherever it sits.

    The captured token is the full non-whitespace run after `tool==`,
    preserving a pre-release/build suffix intact instead of truncating it
    to a bare `X.Y.Z`.
    """
    pattern = r"^[ \t]*(?:run:[ \t]*)?pip install\b.*?\b" + re.escape(tool) + r"==(\S+)"
    return re.findall(pattern, text, re.MULTILINE)


def _pinned_in_precommit(tool: str) -> str | None:
    return _pin_from_precommit_text(PRE_COMMIT.read_text(encoding="utf-8"), tool)


def _disagreeing(expected: str, found: list[str]) -> list[tuple[int, str]]:
    """Every CI occurrence that differs from the pre-commit pin, with its 1-based
    position so a failure can say WHICH one drifted.

    A separate function purely so it can be tested against a disagreement. In a
    healthy repository every occurrence agrees, so the comparison inside the
    assertion below is never meaningfully exercised — narrowing it to the first
    occurrence would pass every run and be caught by nothing. That is exactly
    the bug this whole change exists to remove, so it must not be reintroduced
    one level down.
    """
    return [(index, value) for index, value in enumerate(found, start=1) if value != expected]


def _pinned_in_ci(tool: str) -> list[str]:
    return _pins_from_ci_text(CI.read_text(encoding="utf-8"), tool)


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
        ci_versions = _pinned_in_ci(ci_tool)
        self.assertIsNotNone(
            precommit_version, f"{ci_tool} pin not found in .pre-commit-config.yaml"
        )
        self.assertTrue(ci_versions, f"{ci_tool} pin not found in .github/workflows/ci.yml")
        assert precommit_version is not None  # narrowed by the assertion above
        # EVERY occurrence, not just the first. A tool installed in two jobs
        # must be the same tool in both, and the message names which drifted.
        drifted = _disagreeing(precommit_version, ci_versions)
        self.assertEqual(
            drifted,
            [],
            f"{ci_tool} is pinned {precommit_version} in .pre-commit-config.yaml but "
            f"{len(drifted)} of {len(ci_versions)} occurrence(s) in ci.yml disagree: "
            + ", ".join(f"#{i}={v}" for i, v in drifted),
        )

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
                self.assertTrue(getter(tool), f"{tool} must be pinned in CI")


class TestPinExtractionIgnoresLookalikeComments(unittest.TestCase):
    """`re.search` takes the leftmost match, so an unanchored pattern would
    prefer a comment mentioning the tool over the real pin sitting below
    it. These prove the structural anchors in `_pins_from_ci_text` /
    `_pin_from_precommit_text` aren't fooled by that.
    """

    def test_ci_ignores_a_lookalike_comment_before_the_real_pin(self) -> None:
        text = (
            "      - name: Install pinned tools\n"
            "        # TODO: bump to ruff==0.16.0\n"
            "        run: pip install ruff==0.15.22 mypy==2.3.0\n"
        )
        self.assertEqual(_pins_from_ci_text(text, "ruff"), ["0.15.22"])

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
        self.assertEqual(_pins_from_ci_text(text, "ruff"), ["0.16.0rc1"])

    def test_rc_pin_is_reported_as_a_mismatch_against_the_final_release(self) -> None:
        ci_text = "        run: pip install ruff==0.16.0rc1 mypy==2.3.0\n"
        precommit_text = (
            "  - repo: https://github.com/astral-sh/ruff-pre-commit\n    rev: v0.16.0\n"
        )
        (ci_version,) = _pins_from_ci_text(ci_text, "ruff")
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


class TestEveryCiOccurrenceIsCompared(unittest.TestCase):
    """The gate compared one pin per tool until 2026-08-20, and `ci.yml` has
    installed mypy twice for far longer than that.

    The second install lives inside a `run: |` block, where `pip install` sits
    on its own line — which the old pattern, requiring `run:` on the same
    physical line, could not match at all. So this was not a ranking problem
    to be fixed by preferring a later match; the occurrence was invisible.
    """

    def test_both_mypy_installs_in_the_real_ci_file_are_found(self) -> None:
        """Guard the guard, against the real file rather than a fixture.

        `check_pins.py`'s `_EXTRA_SITES` independently declares that `ci.yml`
        contains exactly two `mypy==`. If a reformat ever hides one from this
        pattern, the two mechanisms disagree and this fails — rather than the
        gate quietly going back to checking half of what it claims to.
        """
        self.assertEqual(len(_pinned_in_ci("mypy")), 2)

    def test_a_pin_inside_a_block_scalar_is_found(self) -> None:
        text = (
            "      - name: Install both distributions\n"
            "        run: |\n"
            "          pip install -e .\n"
            "          pip install 'llm>=0.27' httpx mypy==2.3.1\n"
        )
        self.assertEqual(_pins_from_ci_text(text, "mypy"), ["2.3.1"])

    def test_two_occurrences_that_disagree_are_both_reported(self) -> None:
        """The exact 2026-08-17 shape: one job bumped, the other left behind."""
        text = (
            "        run: pip install ruff==0.16.3 mypy==2.3.1\n"
            "        run: |\n"
            "          pip install 'llm>=0.27' httpx mypy==2.3.0\n"
        )
        self.assertEqual(_pins_from_ci_text(text, "mypy"), ["2.3.1", "2.3.0"])

    def test_widening_the_pattern_did_not_start_matching_comments(self) -> None:
        """Dropping the mandatory `run:` is the whole fix, so the anchor that
        rejects lookalike comments now rests entirely on a line's first
        non-whitespace being `pip install`. Worth asserting directly."""
        text = (
            "        # TODO: bump to mypy==9.9.9\n"
            "        #   pip install mypy==8.8.8\n"
            "        run: pip install mypy==2.3.1\n"
        )
        self.assertEqual(_pins_from_ci_text(text, "mypy"), ["2.3.1"])

    def test_a_disagreement_after_the_first_occurrence_is_still_caught(self) -> None:
        """The survivor of the first mutation run.

        Narrowing the comparison to `ci_versions[:1]` passed every test,
        because in a healthy repo every occurrence agrees and there is nothing
        for a first-only check to miss. Only a fixture where a LATER one
        drifts can tell the two implementations apart.
        """
        self.assertEqual(_disagreeing("2.3.1", ["2.3.1", "2.3.1"]), [])
        self.assertEqual(_disagreeing("2.3.1", ["2.3.1", "2.3.0"]), [(2, "2.3.0")])
        self.assertEqual(_disagreeing("2.3.1", ["2.3.0", "2.3.1"]), [(1, "2.3.0")])
