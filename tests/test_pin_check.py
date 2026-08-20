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
import shutil
import sys
import tempfile
import types
import unittest
import unittest.mock

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


class TestSiteMap(unittest.TestCase):
    """`--write` is only as good as its map of where each pin actually lives.

    The report has always described those locations in prose. Prose is what
    let a mypy bump on 2026-08-17 update four of five sites and look finished:
    `ci.yml` carries `mypy==` twice, and `tests/test_tooling_parity.py` reads
    only the first, so the gate stayed green over a half-applied bump.
    """

    def setUp(self) -> None:
        self.mod = _load()

    def test_every_parsed_pin_has_somewhere_to_write(self) -> None:
        """Guard the guard.

        A hook added to `.pre-commit-config.yaml` is picked up by the parser
        automatically; if `sites_for` did not also cover it automatically, it
        would be reported as stale forever and silently skipped by `--write`.
        """
        for name, _pinned, _where in self.mod.parse_pins():
            with self.subTest(tool=name):
                self.assertTrue(self.mod.sites_for(name), f"{name} has no write site")

    def test_mypy_declares_two_sites_in_ci_not_one(self) -> None:
        """The specific lesson, pinned as a number.

        If someone consolidates the two `mypy==` lines in `ci.yml`, this fails
        and they update the count deliberately — rather than `--write` quietly
        bumping one of them and leaving the type-checker split across versions.
        """
        ci_sites = [
            site
            for site in self.mod.sites_for("pre-commit/mirrors-mypy")
            if site.path.name == "ci.yml"
        ]
        self.assertEqual(len(ci_sites), 1, "expected exactly one ci.yml site declaration")
        self.assertEqual(ci_sites[0].occurrences, 2)

    def test_claude_md_is_a_site_for_both_tools_it_quotes(self) -> None:
        """CLAUDE.md §5 quotes the lint job's pip line verbatim. It has gone
        stale twice, both times unnoticed until a human read it."""
        for tool in ("astral-sh/ruff-pre-commit", "pre-commit/mirrors-mypy"):
            with self.subTest(tool=tool):
                names = [site.path.name for site in self.mod.sites_for(tool)]
                self.assertIn("CLAUDE.md", names)

    def test_the_v_prefix_convention_of_each_site_is_preserved(self) -> None:
        """pre-commit revs are written `v1.2.3`; pip pins are written `1.2.3`.
        Upstream answers in whichever form it likes, so neither can be copied
        in as-is."""
        self.assertEqual(self.mod._retarget("v0.16.2", "0.16.3"), "v0.16.3")
        self.assertEqual(self.mod._retarget("0.16.2", "v0.16.3"), "0.16.3")
        self.assertEqual(self.mod._retarget("v0.16.2", "v0.16.3"), "v0.16.3")
        self.assertEqual(self.mod._retarget("0.16.2", "0.16.3"), "0.16.3")


class TestRewriteRefusal(unittest.TestCase):
    """A partial bump is worse than none: it leaves pins disagreeing while the
    parity gate still passes. So a site that does not look the way it declares
    must stop the whole rewrite, before anything is written."""

    def setUp(self) -> None:
        self.mod = _load()
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    def _site(self, name: str, body: str, pattern: str, count: int):
        path = self.tmp / name
        path.write_text(body, encoding="utf-8")
        return self.mod._Site(path, pattern, count)

    def test_a_wrong_occurrence_count_raises_and_writes_nothing(self) -> None:
        good = self._site("a.yml", "tool==1.0.0\n", self.mod._pip_pattern("tool"), 1)
        # Declares two, contains one -- the shape this exists to catch.
        bad = self._site("b.yml", "tool==1.0.0\n", self.mod._pip_pattern("tool"), 2)
        before = good.path.read_text(encoding="utf-8")

        with unittest.mock.patch.object(self.mod, "sites_for", return_value=(good, bad)):
            with self.assertRaises(self.mod.PinRewriteError) as ctx:
                self.mod.rewrite("tool", "2.0.0")

        self.assertIn("b.yml", str(ctx.exception))
        self.assertIn("found 1", str(ctx.exception))
        self.assertEqual(
            good.path.read_text(encoding="utf-8"),
            before,
            "the valid site must be untouched -- validation runs before any write",
        )

    def test_a_matching_plan_rewrites_every_occurrence(self) -> None:
        twice = self._site(
            "c.yml", "tool==1.0.0\nother\ntool==1.0.0\n", self.mod._pip_pattern("tool"), 2
        )
        with unittest.mock.patch.object(self.mod, "sites_for", return_value=(twice,)):
            edits = self.mod.rewrite("tool", "2.0.0")
        self.assertEqual(edits, [(twice.path, 2)])
        self.assertEqual(twice.path.read_text(encoding="utf-8").count("tool==2.0.0"), 2)
        self.assertNotIn("1.0.0", twice.path.read_text(encoding="utf-8"))
