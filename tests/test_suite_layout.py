"""Guards for the shape of the test files themselves.

`python3 -m unittest discover` imports each file whole and never runs its
`if __name__ == "__main__":` block, so CI is blind to where that block sits.
Running a file directly is not: Python executes it top to bottom, and
`unittest.main()` exits the process at the guard. Every class defined below it
simply never exists, and the run still says OK.

That shipped twice before anything noticed. `tests/test_pin_check.py` ran 10
of its 32 tests when invoked directly, `tests/test_tooling_parity.py` 10 of 15
— each file grew by appending classes after a guard that had been at the end.
"""

from __future__ import annotations

import ast
import pathlib
import unittest

TESTS_DIR = pathlib.Path(__file__).resolve().parent


def _is_main_guard(node: ast.stmt) -> bool:
    """True for `if __name__ == "__main__":`, however it is spelled around."""
    if not isinstance(node, ast.If) or not isinstance(node.test, ast.Compare):
        return False
    test = node.test
    return (
        isinstance(test.left, ast.Name)
        and test.left.id == "__name__"
        and len(test.comparators) == 1
        and isinstance(test.comparators[0], ast.Constant)
        and test.comparators[0].value == "__main__"
    )


class TestMainGuardComesLast(unittest.TestCase):
    def test_the_suite_is_not_vacuous(self) -> None:
        """An empty glob would pass the check below for every file in it."""
        self.assertGreater(len(list(TESTS_DIR.glob("test_*.py"))), 5)

    def test_nothing_follows_the_main_guard(self) -> None:
        for path in sorted(TESTS_DIR.glob("test_*.py")):
            with self.subTest(file=path.name):
                body = ast.parse(path.read_text(encoding="utf-8")).body
                guards = [i for i, node in enumerate(body) if _is_main_guard(node)]
                for index in guards:
                    self.assertEqual(
                        index,
                        len(body) - 1,
                        f"{path.name}: code after the __main__ guard at line "
                        f"{body[index].lineno} never runs when the file is executed directly",
                    )


if __name__ == "__main__":
    unittest.main()
