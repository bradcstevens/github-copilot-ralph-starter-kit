"""Static guard: no Python-native API libraries in ``ralph_afk/``.

The wrapper contract (PRD #1, issue #6) requires every external GitHub or
git API call to flow through ``subprocess.run`` against the ``gh`` and
``git`` CLIs. The user's existing ``gh auth`` and ``git config`` (including
SSO tokens, credential helpers, ``safe.directory`` settings, GitHub
Enterprise endpoints) is the single source of truth.

We explicitly forbid these importable top-level packages:

* ``httpx`` — generic HTTP client.
* ``requests`` — generic HTTP client.
* ``github`` — ``PyGithub`` (``pip install PyGithub`` → ``import github``).
* ``git`` — ``GitPython`` (``pip install GitPython`` → ``import git``).
* ``pygit2`` — libgit2 bindings.

Our own ``ralph_afk.git`` module is **not** affected by this guard because
its top-level package name is ``ralph_afk``, not ``git`` — qualified
imports (``from ralph_afk import git``, ``from ralph_afk.git import head_sha``,
``import ralph_afk.git``) and relative imports (``from . import git``) all
target ``ralph_afk``.

The check is performed via AST inspection across every ``*.py`` under
``ralph_afk/`` so any future module addition is automatically covered.
"""

from __future__ import annotations

import ast
from pathlib import Path

import ralph_afk

FORBIDDEN: frozenset[str] = frozenset(
    {
        "httpx",
        "requests",
        "github",
        "git",
        "pygit2",
    }
)


def _iter_package_python_files() -> list[Path]:
    """Return every ``.py`` file under the installed ``ralph_afk`` package."""
    pkg_dir = Path(ralph_afk.__file__).parent
    return sorted(pkg_dir.rglob("*.py"))


def test_at_least_one_module_is_inspected() -> None:
    """Sanity guard: this test is worthless if the rglob finds nothing."""
    files = _iter_package_python_files()
    assert files, "expected to find Python modules under ralph_afk/ to inspect"


def test_no_forbidden_api_libraries_in_any_ralph_afk_module() -> None:
    """Walk every module's AST and reject top-level imports of forbidden packages.

    Catches:

    * ``import httpx`` / ``import requests`` / ``import github`` / ``import git``
      / ``import pygit2`` — any top-level forbidden package as the import target.
    * ``from httpx import ...`` etc. — same set as ``from``-import sources.

    Allowed (not flagged):

    * ``from ralph_afk import git`` — qualified, top-level package is ``ralph_afk``.
    * ``import ralph_afk.git`` — same.
    * ``from . import git`` / ``from .git import head_sha`` — relative imports
      (``node.level > 0``); their target is the package itself.
    """
    failures: list[str] = []
    for py_file in _iter_package_python_files():
        try:
            source = py_file.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            failures.append(f"{py_file}: could not decode as UTF-8: {exc}")
            continue
        try:
            tree = ast.parse(source, filename=str(py_file))
        except SyntaxError as exc:
            failures.append(f"{py_file}: SyntaxError: {exc}")
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top = alias.name.split(".")[0]
                    if top in FORBIDDEN:
                        failures.append(
                            f"{py_file}:{node.lineno}: forbidden `import {alias.name}` "
                            f"(top-level package {top!r} is in the forbidden set)"
                        )
            elif isinstance(node, ast.ImportFrom):
                # Relative imports (`from . import git`) target the package
                # itself, not a top-level forbidden module.
                if node.level > 0:
                    continue
                if not node.module:
                    continue
                top = node.module.split(".")[0]
                if top in FORBIDDEN:
                    failures.append(
                        f"{py_file}:{node.lineno}: forbidden "
                        f"`from {node.module} import ...` "
                        f"(top-level package {top!r} is in the forbidden set)"
                    )
    assert not failures, (
        "ralph_afk/ must funnel every external API call through subprocess "
        "wrappers around `gh` and `git` — Python-native API libraries are "
        "forbidden:\n  " + "\n  ".join(failures)
    )


def test_our_own_ralph_afk_git_module_is_not_misidentified() -> None:
    """Regression guard for the allowlist logic.

    Imports of our own ``ralph_afk.git`` module — qualified or relative —
    must not be flagged. This test loads the gh.py / loop.py-style import
    patterns we expect to see and asserts the classification is correct.
    """
    samples = [
        "from ralph_afk import git",
        "from ralph_afk.git import head_sha",
        "import ralph_afk.git",
        "from . import git",
        "from .git import head_sha",
    ]
    for src in samples:
        tree = ast.parse(src)
        node = tree.body[0]
        if isinstance(node, ast.Import):
            top = node.names[0].name.split(".")[0]
            assert top == "ralph_afk", f"{src!r} classified wrongly: top={top!r}"
        elif isinstance(node, ast.ImportFrom):
            if node.level > 0:
                # Relative import is OK by definition.
                continue
            assert node.module is not None
            top = node.module.split(".")[0]
            assert top == "ralph_afk", f"{src!r} classified wrongly: top={top!r}"
        else:
            raise AssertionError(f"unexpected AST node type for {src!r}")
