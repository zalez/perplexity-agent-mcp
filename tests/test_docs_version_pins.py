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

import json
import pathlib
import re
import sys
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
README = REPO_ROOT / "README.md"
SERVER = REPO_ROOT / "perplexity_agent_mcp.py"
SERVER_JSON = REPO_ROOT / "server.json"
PLUGIN_PYPROJECT = REPO_ROOT / "llm-plugin" / "pyproject.toml"

# Both pin forms the README can carry:
#   @v1.2.3   a git tag        (`git+https://…@v1.2.3`)
#   @1.2.3    a PyPI version   (`uvx perplexity-agent-mcp@1.2.3`)
# The placeholder `@vX.Y.Z` deliberately does not match — it names no release,
# so it cannot go stale.
_PIN_RE = re.compile(r"@v?(?P<version>\d+\.\d+\.\d+)")


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
            1,
            "expected at least one version pin in README.md; if the install "
            "snippets changed shape, update _PIN_RE to match rather than "
            "letting this pass on zero findings",
        )


class TestServerJson(unittest.TestCase):
    """`server.json` is what the MCP Registry publishes, and it repeats the
    version twice — once for the server, once for the PyPI package it points
    at. Both must match `__version__`, or a release publishes a registry entry
    advertising a package version that does not exist.

    The publish workflow checks this too, but failing here costs a test run
    rather than half a release: PyPI does not allow re-uploading a version, so
    a wrong number cannot simply be corrected in place.
    """

    def setUp(self) -> None:
        self.manifest = json.loads(SERVER_JSON.read_text(encoding="utf-8"))
        self.shipped = _shipped_version()

    def test_server_version_matches_the_shipped_version(self) -> None:
        self.assertEqual(self.manifest["version"], self.shipped)

    def test_every_package_version_matches_too(self) -> None:
        packages = self.manifest.get("packages") or []
        self.assertTrue(packages, "server.json must declare at least one package")
        for package in packages:
            with self.subTest(package=package.get("identifier")):
                self.assertEqual(package["version"], self.shipped)

    @unittest.skipIf(sys.version_info < (3, 11), "tomllib requires 3.11")
    def test_the_package_identifier_matches_the_distribution_name(self) -> None:
        """A typo here publishes a registry entry pointing at someone else's
        package — or at nothing."""
        import tomllib

        name = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        distribution = name["project"]["name"]
        for package in self.manifest["packages"]:
            with self.subTest(package=package.get("identifier")):
                self.assertEqual(package["identifier"], distribution)

    def test_the_readme_carries_the_ownership_marker(self) -> None:
        """The registry proves we own the PyPI package by finding
        `mcp-name: <server name>` in the package README, which is what PyPI
        renders as the project description. No marker, no publish — and the
        failure happens at release time, not here, unless this test catches it.
        """
        expected = f"mcp-name: {self.manifest['name']}"
        self.assertIn(
            expected,
            README.read_text(encoding="utf-8"),
            f"README.md must contain {expected!r} for registry ownership verification",
        )


class TestPluginVersionLockstep(unittest.TestCase):
    """`llm-perplexity-agent` is a second distribution, deliberately NOT
    independently versioned.

    Both are built and published from the same tag, and the adapter pins the
    server exactly, so the adapter can never run against a core it was not
    tested against. That only holds while three numbers agree: the server's
    `__version__`, the adapter's own version, and the version it pins. Nothing
    but this test makes them agree.
    """

    @unittest.skipIf(sys.version_info < (3, 11), "tomllib requires 3.11")
    def setUp(self) -> None:
        import tomllib

        self.plugin = tomllib.loads(PLUGIN_PYPROJECT.read_text(encoding="utf-8"))["project"]
        self.shipped = _shipped_version()

    @unittest.skipIf(sys.version_info < (3, 11), "tomllib requires 3.11")
    def test_the_adapter_ships_the_same_version_as_the_server(self) -> None:
        self.assertEqual(self.plugin["version"], self.shipped)

    @unittest.skipIf(sys.version_info < (3, 11), "tomllib requires 3.11")
    def test_the_adapter_pins_this_exact_server_version(self) -> None:
        pins = [d for d in self.plugin["dependencies"] if d.startswith("perplexity-agent-mcp")]
        self.assertEqual(len(pins), 1, "expected exactly one pin on the server")
        self.assertEqual(
            pins[0],
            f"perplexity-agent-mcp=={self.shipped}",
            "the adapter must pin the server exactly, at the version being shipped",
        )


if __name__ == "__main__":
    unittest.main()
