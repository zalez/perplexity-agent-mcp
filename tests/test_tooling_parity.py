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


def _pinned_in_precommit(tool: str) -> str | None:
    text = PRE_COMMIT.read_text(encoding="utf-8")
    match = re.search(rf"{tool}[^\n]*\n\s+rev:\s*v?([0-9]+\.[0-9]+\.[0-9]+)", text)
    return match.group(1) if match else None


def _pinned_in_ci(tool: str) -> str | None:
    text = CI.read_text(encoding="utf-8")
    match = re.search(rf"{tool}==([0-9]+\.[0-9]+\.[0-9]+)", text)
    return match.group(1) if match else None


class TestToolingParity(unittest.TestCase):
    def test_ruff_versions_match(self) -> None:
        self.assertEqual(_pinned_in_precommit("ruff-pre-commit"), _pinned_in_ci("ruff"))

    def test_mypy_versions_match(self) -> None:
        self.assertEqual(_pinned_in_precommit("mirrors-mypy"), _pinned_in_ci("mypy"))

    def test_both_versions_are_actually_pinned(self) -> None:
        for tool, getter in (
            ("ruff", _pinned_in_ci),
            ("mypy", _pinned_in_ci),
        ):
            with self.subTest(tool=tool):
                self.assertIsNotNone(getter(tool), f"{tool} must be pinned in CI")


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
