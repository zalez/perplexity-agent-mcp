"""The core product claim — zero third-party dependencies — as a CI gate.

Marketing claims that aren't tested are marketing claims that drift.
"""

from __future__ import annotations

import ast
import pathlib
import re
import sys
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SERVER = REPO_ROOT / "perplexity_agent_mcp.py"

# Every module the server is permitted to import. Adding to this list is a
# deliberate act that shows up in code review.
ALLOWED = frozenset(
    {
        "__future__",
        "argparse",
        "collections.abc",
        "io",
        "json",
        "os",
        "random",
        "re",
        "secrets",
        "ssl",
        "sys",
        "time",
        "typing",
        "urllib",
        "urllib.error",
        "urllib.parse",
        "urllib.request",
    }
)


def _requirement_name(requirement: str) -> str:
    """Bare distribution name from a PEP 508 requirement string."""
    return re.split(r"[<>=!~\[; ]", requirement.strip(), maxsplit=1)[0].strip()


def _imported_roots(path: pathlib.Path) -> set[str]:
    """Every module name imported by `path`, as written in the source."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative import — impossible in a single file
                raise AssertionError("relative import in a single-file server")
            if node.module:
                names.add(node.module)
    return names


class TestNoDependencies(unittest.TestCase):
    def test_server_imports_only_allowed_stdlib(self) -> None:
        for name in sorted(_imported_roots(SERVER)):
            with self.subTest(module=name):
                self.assertIn(
                    name,
                    ALLOWED,
                    f"{name!r} is not in the stdlib allowlist. If it is genuinely "
                    f"stdlib, add it to ALLOWED deliberately.",
                )

    def test_allowlist_is_actually_stdlib(self) -> None:
        """Guard the guard: every allowlisted name must be a stdlib module."""
        stdlib = sys.stdlib_module_names
        for name in sorted(ALLOWED):
            with self.subTest(module=name):
                self.assertIn(name.split(".")[0], stdlib)

    @unittest.skipIf(sys.version_info < (3, 11), "tomllib requires 3.11")
    def test_pyproject_declares_no_runtime_dependencies(self) -> None:
        import tomllib

        data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(data["project"]["dependencies"], [])

    @unittest.skipIf(sys.version_info < (3, 11), "tomllib requires 3.11")
    def test_the_server_declares_no_optional_dependencies_either(self) -> None:
        """Not even an extra.

        This briefly allowed one — an `llm` extra, when the CLI adapter shipped
        inside this distribution. Splitting the adapter into its own package
        (`llm-perplexity-agent`, built from llm-plugin/) removed the need, so
        the stricter original invariant is back: installing the server pulls in
        nothing at all, under any combination of extras.
        """
        import tomllib

        data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertNotIn("optional-dependencies", data["project"])

    @unittest.skipIf(sys.version_info < (3, 11), "tomllib requires 3.11")
    def test_the_llm_adapter_depends_on_llm_and_this_server_and_nothing_else(self) -> None:
        """The adapter is allowed dependencies; it is not allowed surprises.

        Two, exactly: `llm` (the thing it plugs into) and this server (the
        client it adapts). Anything else appearing here would mean the
        zero-dependency story quietly stopped being true for the half of the
        project people reach by `llm install`.
        """
        import tomllib

        plugin = REPO_ROOT / "llm-plugin" / "pyproject.toml"
        data = tomllib.loads(plugin.read_text(encoding="utf-8"))
        names = sorted(_requirement_name(r) for r in data["project"]["dependencies"])
        self.assertEqual(names, ["llm", "perplexity-agent-mcp"])
        self.assertNotIn("optional-dependencies", data["project"])

    @unittest.skipIf(sys.version_info < (3, 11), "tomllib requires 3.11")
    def test_build_backend_stays_a_single_zero_dependency_package(self) -> None:
        """The build backend is a supply chain too.

        flit_core was chosen because it resolves to exactly one package with
        no transitive dependencies. It builds only one module per
        distribution, though, and this project ships two — so it was replaced
        with setuptools, which is the only backend that keeps that property.
        Pinned here so a casual switch to something with a dependency tree
        (hatchling pulls four) is a decision rather than an accident.
        """
        import tomllib

        data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        requires = data["build-system"]["requires"]
        self.assertEqual(
            [_requirement_name(r) for r in requires],
            ["setuptools"],
            "exactly one build dependency, and it must be a zero-transitive one",
        )


if __name__ == "__main__":
    unittest.main()
