"""Guards for the dev-tool pin checker in `.github/scripts/check_pins.py`.

The checker's worst failure is a silent all-clear: if a reformat of
`.pre-commit-config.yaml` or `ci.yml` breaks its regexes, it finds nothing,
compares nothing, and reports that every pin is current. Nobody looks at a
green report. So the parsing is asserted here, offline and without touching
the network — the fetching is not, since that would make the suite depend on
GitHub being reachable.

This mirrors the lesson from `tests/test_tooling_parity.py`, whose equality
checks were once vacuously satisfiable when both sides were missing.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
import types
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / ".github" / "scripts" / "check_pins.py"


def _load() -> types.ModuleType:
    """Import the checker by path — it lives outside any package."""
    spec = importlib.util.spec_from_file_location("check_pins", SCRIPT)
    if spec is None or spec.loader is None:  # pragma: no cover - import plumbing
        raise AssertionError(f"could not load {SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_pins"] = module
    spec.loader.exec_module(module)
    return module


class TestParsing(unittest.TestCase):
    """The parser must actually find the pins that exist."""

    def setUp(self) -> None:
        self.mod = _load()
        self.rows = self.mod.parse_pins()

    def test_every_pre_commit_repo_is_found(self) -> None:
        """One row per `repo:` in the config, so none can be skipped silently."""
        config = (REPO_ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")
        # `- repo: local` has no rev and is intentionally not checked upstream.
        remote = config.count("repo: https://github.com/")
        found = [name for name, _, _ in self.rows if "/" in name and "(" not in name]
        self.assertEqual(
            len(found),
            remote,
            f"parsed {len(found)} hook repos but the config declares {remote}. "
            "A regex that silently matches fewer pins reports a false all-clear.",
        )

    def test_pre_commit_itself_is_checked(self) -> None:
        """It is pip-installed in CI, so nothing else watches this pin."""
        names = [name for name, _, _ in self.rows]
        self.assertIn(self.mod._PRE_COMMIT_PYPI, names)

    def test_every_pin_has_a_version_and_a_location(self) -> None:
        for name, pinned, where in self.rows:
            with self.subTest(pin=name):
                self.assertTrue(pinned.strip(), f"{name} parsed with an empty version")
                self.assertTrue(where.strip(), f"{name} parsed with no file to fix")

    def test_ruff_and_mypy_say_both_files_must_change(self) -> None:
        """They are pinned twice; a bump that edits one file fails parity."""
        for name, _, where in self.rows:
            if name in {"astral-sh/ruff-pre-commit", "pre-commit/mirrors-mypy"}:
                with self.subTest(pin=name):
                    self.assertIn("ci.yml", where)
                    self.assertIn(".pre-commit-config.yaml", where)

    def test_parsing_is_not_vacuous(self) -> None:
        """Belt and braces: an empty parse must never look like success."""
        self.assertGreaterEqual(len(self.rows), 5)


class TestReporting(unittest.TestCase):
    """`render` decides whether anyone gets told. Check both directions."""

    def setUp(self) -> None:
        self.mod = _load()

    def test_matching_versions_are_not_reported_as_stale(self) -> None:
        report, stale = self.mod.render([("tool", "v1.2.3", "v1.2.3", "somewhere")])
        self.assertFalse(stale)
        self.assertIn("match their upstream latest release", report)

    def test_the_v_prefix_is_not_a_difference(self) -> None:
        """`1.2.3` pinned against a `v1.2.3` release is the same version."""
        _, stale = self.mod.render([("tool", "1.2.3", "v1.2.3", "somewhere")])
        self.assertFalse(stale)

    def test_a_real_difference_is_reported(self) -> None:
        report, stale = self.mod.render([("tool", "v1.2.3", "v1.3.0", "ci.yml")])
        self.assertTrue(stale)
        self.assertIn("v1.3.0", report)
        self.assertIn("ci.yml", report)

    def test_an_unreachable_upstream_is_not_treated_as_current(self) -> None:
        """A network failure must read as 'unknown', never as 'fine'."""
        report, stale = self.mod.render([("tool", "v1.2.3", None, "ci.yml")])
        self.assertFalse(stale, "unknown is not stale")
        self.assertIn("Could not reach upstream", report)
        self.assertIn("not as current", report)

    def test_the_report_warns_against_autoupdate(self) -> None:
        """The downgrade trap is the whole reason this script exists."""
        report, _ = self.mod.render([("tool", "v1.2.3", "v1.3.0", "ci.yml")])
        self.assertIn("autoupdate", report)
        self.assertIn("downgrade", report)


if __name__ == "__main__":
    unittest.main()
