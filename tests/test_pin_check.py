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
import os
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
        result = self.mod.render([("tool", "v1.2.3", "v1.2.3", "somewhere")])
        self.assertFalse(result.stale)
        self.assertFalse(result.unknown)
        self.assertIn("match their upstream latest release", result.text)

    def test_the_v_prefix_is_not_a_difference(self) -> None:
        """`1.2.3` pinned against a `v1.2.3` release is the same version."""
        self.assertFalse(self.mod.render([("tool", "1.2.3", "v1.2.3", "somewhere")]).stale)

    def test_a_real_difference_is_reported(self) -> None:
        result = self.mod.render([("tool", "v1.2.3", "v1.3.0", "ci.yml")])
        self.assertTrue(result.stale)
        self.assertIn("v1.3.0", result.text)
        self.assertIn("ci.yml", result.text)

    def test_an_unreachable_upstream_is_not_treated_as_current(self) -> None:
        """A network failure must read as 'unknown', never as 'fine'."""
        result = self.mod.render([("tool", "v1.2.3", None, "ci.yml")])
        self.assertFalse(result.stale, "unknown is not stale")
        self.assertIn("Could not reach upstream", result.text)
        self.assertIn("not as current", result.text)

    def test_the_report_warns_against_autoupdate(self) -> None:
        """The downgrade trap is the whole reason this script exists."""
        result = self.mod.render([("tool", "v1.2.3", "v1.3.0", "ci.yml")])
        self.assertIn("autoupdate", result.text)
        self.assertIn("downgrade", result.text)


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


class TestStaleIsReportedEvenWhenTheBumpFails(unittest.TestCase):
    """The nag must not depend on the bump succeeding.

    On 2026-08-24 it did, and the whole gate went quiet. `--write` had found a
    real drift and edited it correctly; the workflow step that pushed the
    result was rejected by GitHub, the job went red, and the step that files
    the tracking issue never ran — because it was gated on an output the
    script only wrote after the part that failed. A drifted pin went entirely
    unreported by the thing whose only job is to report drifted pins.

    The push is gone now, but the coupling is the durable lesson: `stale` is
    emitted before any rewrite is attempted, so a refused rewrite still exits
    non-zero AND still tells the caller there is something to say.
    """

    def setUp(self) -> None:
        self.mod = _load()
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.output = self.tmp / "github_output"
        self.output.touch()

    def _run_with_a_refused_rewrite(self) -> tuple[int, str]:
        rows = [("tool", "1.0.0", "2.0.0", "somewhere")]
        refuse = unittest.mock.Mock(side_effect=self.mod.PinRewriteError("shape changed"))
        with unittest.mock.patch.object(self.mod, "collect", return_value=rows):
            with unittest.mock.patch.object(self.mod, "rewrite", refuse):
                with unittest.mock.patch.dict(os.environ, {"GITHUB_OUTPUT": str(self.output)}):
                    code = self.mod.main(["--write"])
        return code, self.output.read_text(encoding="utf-8")

    def test_a_refused_rewrite_still_exits_non_zero(self) -> None:
        code, _ = self._run_with_a_refused_rewrite()
        self.assertEqual(code, 1, "a file that changed shape must fail loudly")

    def test_a_refused_rewrite_still_emits_stale(self) -> None:
        _, written = self._run_with_a_refused_rewrite()
        self.assertIn(
            "stale=true",
            written,
            "the caller gates its tracking issue on this; writing it only after "
            "the rewrite means a failed rewrite silences the report entirely",
        )

    def test_bumped_is_not_claimed_when_nothing_was_written(self) -> None:
        """`stale` survives the failure. `bumped` must not — nothing was."""
        _, written = self._run_with_a_refused_rewrite()
        self.assertNotIn("bumped=true", written)

    def test_a_clean_run_emits_both(self) -> None:
        """The ordering must not have broken the ordinary path."""
        rows = [("tool", "1.0.0", "1.0.0", "somewhere")]
        with unittest.mock.patch.object(self.mod, "collect", return_value=rows):
            with unittest.mock.patch.dict(os.environ, {"GITHUB_OUTPUT": str(self.output)}):
                code = self.mod.main(["--write"])
        written = self.output.read_text(encoding="utf-8")
        self.assertEqual(code, 0)
        self.assertIn("stale=false", written)
        self.assertIn("bumped=false", written)


class TestTheWorkflowDoesNotTryToPush(unittest.TestCase):
    """`GITHUB_TOKEN` can never push a bump on this repo, so it must not try.

    It is a GitHub App installation token, and GitHub refuses ANY App push
    that creates or updates a file under `.github/workflows/`. No permission
    grants it — the workflow `permissions:` block has no `workflows` key at
    all; `actions: write` covers the Actions API, not workflow files. And
    `ruff` and `mypy` are pinned in `ci.yml`, so every bump of either IS a
    workflow-file edit by construction.

    Adding `contents: write` and a push back looks like an obvious
    improvement — "it already computed the fix, why not commit it?" — and is
    the exact change that was reverted here. It cannot work. It can only fail
    on the first run that finds something, which is what happened.
    """

    WORKFLOW = REPO_ROOT / ".github" / "workflows" / "pin-check.yml"

    def setUp(self) -> None:
        self.text = self.WORKFLOW.read_text(encoding="utf-8")

    def test_the_job_does_not_request_write_access_to_contents(self) -> None:
        for line in self.text.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue  # the comment explaining this is allowed to say it
            self.assertNotIn(
                "contents: write",
                stripped,
                "a GITHUB_TOKEN push touching .github/workflows/ is always refused",
            )

    def test_the_workflow_does_not_push(self) -> None:
        self.assertNotIn("git push", self.text)

    def test_it_still_holds_the_one_permission_it_needs(self) -> None:
        """Read-only everywhere would be a silent no-op, not a safe default."""
        self.assertIn("issues: write", self.text)

    def test_the_tracking_issue_is_filed_even_when_the_step_before_it_fails(self) -> None:
        """The counterpart to `stale` being emitted early — both halves are
        needed, and either alone still leaves the nag silenceable."""
        self.assertIn("always() && steps.check.outputs.stale == 'true'", self.text)


class TestUnreachableIsNotCurrent(unittest.TestCase):
    """ "Nothing was found stale" and "everything is current" are different claims.

    `render` only ever compared the pins it could reach, and reported `stale`
    for those. That is correct as far as it goes — but a run where every
    upstream was unreachable finds nothing stale for the trivial reason that it
    compared nothing, and the caller could not tell the two apart.

    `.github/workflows/pin-check.yml` acted on that: its close step fired on
    `stale == 'false'` alone, so a network outage during the weekly slot would
    CLOSE the tracking issue with "Every dev-tool pin now matches its upstream
    latest release." — a verification the run never performed — and un-report a
    real drift until the following Monday.

    So `unknown` is now emitted beside `stale`, and closing requires both to be
    false. Same family as the 2026-08-24 failure the push revert fixed: the nag
    going quiet for a reason that has nothing to do with the pins.
    """

    def setUp(self) -> None:
        self.mod = _load()

    def test_a_total_outage_is_unknown_not_current(self) -> None:
        result = self.mod.render([("a", "1.0.0", None, "x"), ("b", "2.0.0", None, "y")])
        self.assertFalse(result.stale, "nothing can be stale when nothing was compared")
        self.assertTrue(result.unknown, "the caller must be able to see that")

    def test_a_total_outage_does_not_claim_every_pin_matches(self) -> None:
        """The headline is read alone, by someone skimming a closed issue."""
        result = self.mod.render([("a", "1.0.0", None, "x")])
        self.assertNotIn("All dev-tool pins match", result.text)
        self.assertIn("No pin could be checked", result.text)

    def test_a_partial_outage_is_also_unknown(self) -> None:
        """One unreachable pin is the one that might have drifted.

        Reaching seven of eight is not "current"; it is seven answers and one
        silence, and the silent one is exactly the one nobody has checked.
        """
        result = self.mod.render([("a", "1.0.0", "1.0.0", "x"), ("b", "2.0.0", None, "y")])
        self.assertFalse(result.stale)
        self.assertTrue(result.unknown)
        self.assertIn("1 of 2 pins compared", result.text)

    def test_everything_reachable_and_current_is_not_unknown(self) -> None:
        """The ordinary green path still says so plainly, and still closes."""
        result = self.mod.render([("a", "1.0.0", "1.0.0", "x")])
        self.assertFalse(result.stale)
        self.assertFalse(result.unknown)
        self.assertIn("All dev-tool pins match their upstream latest release.", result.text)

    def test_stale_and_unknown_are_independent(self) -> None:
        """A drift found AND an upstream missed: both flags, not either/or."""
        result = self.mod.render([("a", "1.0.0", "2.0.0", "x"), ("b", "3.0.0", None, "y")])
        self.assertTrue(result.stale)
        self.assertTrue(result.unknown)


class TestUnknownReachesTheWorkflow(unittest.TestCase):
    """The flag is worthless unless it is emitted and acted on."""

    WORKFLOW = REPO_ROOT / ".github" / "workflows" / "pin-check.yml"

    def setUp(self) -> None:
        self.mod = _load()
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.output = self.tmp / "github_output"
        self.output.touch()

    def _emitted_for(self, rows) -> str:
        with unittest.mock.patch.object(self.mod, "collect", return_value=rows):
            with unittest.mock.patch.dict(os.environ, {"GITHUB_OUTPUT": str(self.output)}):
                self.assertEqual(self.mod.main([]), 0)
        return self.output.read_text(encoding="utf-8")

    def test_an_outage_emits_unknown_true(self) -> None:
        written = self._emitted_for([("a", "1.0.0", None, "x")])
        self.assertIn("stale=false", written)
        self.assertIn("unknown=true", written)

    def test_a_clean_run_emits_unknown_false(self) -> None:
        written = self._emitted_for([("a", "1.0.0", "1.0.0", "x")])
        self.assertIn("stale=false", written)
        self.assertIn("unknown=false", written)

    def test_the_close_step_requires_both_flags(self) -> None:
        """`stale == 'false'` alone is the bug. Both, or the issue stays open."""
        text = self.WORKFLOW.read_text(encoding="utf-8")
        self.assertIn(
            "steps.check.outputs.stale == 'false' && steps.check.outputs.unknown == 'false'",
            text,
            "closing on `stale` alone closes the tracking issue during an outage",
        )
