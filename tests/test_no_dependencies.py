"""The core product claim — zero third-party dependencies — as a CI gate.

Marketing claims that aren't tested are marketing claims that drift.
"""

from __future__ import annotations

import ast
import pathlib
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
    def test_pyproject_declares_no_dependencies(self) -> None:
        import tomllib

        data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(data["project"]["dependencies"], [])
        self.assertNotIn("optional-dependencies", data["project"])


if __name__ == "__main__":
    unittest.main()
