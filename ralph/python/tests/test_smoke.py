"""Smoke tests for the ``ralph-afk`` console script.

These tests cover only the CLI-surface contracts that survive
across slices:

* ``ralph-afk --help`` exits 0 and surfaces the documented flags.
* Negative ``<max-iterations>`` is rejected before any I/O.
* Unknown ``ISSUE_SOURCE`` is rejected via argparse-style stderr.
* Missing prompt file inside a git repo raises a clear stderr message
  rather than a stack trace (replaces the original scaffold-stub
  echo-ISSUE_SOURCE assertion).

Deeper behaviour (the iteration driver itself) is covered by
:mod:`tests.test_iteration_end_to_end`.
"""

from __future__ import annotations

import shutil
import subprocess
import sys


def _ralph_afk_command() -> list[str]:
    """Prefer the installed console script; fall back to ``python -m``.

    ``uv sync --project ralph/python`` puts ``ralph-afk`` on the venv's
    PATH via ``[project.scripts]``. If the test happens to run in an
    environment where the script isn't on PATH yet (e.g. partial
    install), fall back to invoking the module directly so the smoke
    remains meaningful.
    """
    if shutil.which("ralph-afk"):
        return ["ralph-afk"]
    return [sys.executable, "-m", "ralph_afk.cli"]


def test_ralph_afk_help_exits_zero() -> None:
    """``ralph-afk --help`` prints help and exits 0."""
    cmd = _ralph_afk_command() + ["--help"]
    result = subprocess.run(
        cmd, capture_output=True, text=True, check=False, timeout=30
    )
    assert result.returncode == 0, (
        f"ralph-afk --help exited {result.returncode}; "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    stdout = result.stdout
    # The full deep-module CLI surface must be visible in --help so
    # operators (and wrapper scripts) can discover it.
    for expected in (
        "max-iterations",
        "--no-reasoning",
        "--deny-tool",
        "--deny-skill",
        "MAX_NMT_STRIKES",
        "RALPH_DENY_TOOLS",
        "RALPH_PRICING_FILE",
    ):
        assert expected in stdout, (
            f"--help missing expected token {expected!r}; stdout was:\n"
            f"{stdout}"
        )


def test_ralph_afk_rejects_negative_iterations() -> None:
    """Negative ``max_iterations`` is rejected with a non-zero exit and clear error."""
    cmd = _ralph_afk_command() + ["-1"]
    result = subprocess.run(
        cmd, capture_output=True, text=True, check=False, timeout=30
    )
    assert result.returncode != 0, (
        "ralph-afk should reject a negative max_iterations argument; "
        f"got exit 0 with stdout={result.stdout!r}"
    )
    assert (
        "max_iterations" in result.stderr or "non-negative" in result.stderr
    ), (
        f"expected a max_iterations validation message on stderr; "
        f"stderr was:\n{result.stderr}"
    )


def test_ralph_afk_rejects_unknown_issue_source(tmp_path, monkeypatch) -> None:
    """An unsupported ``ISSUE_SOURCE`` value is rejected with a clear error.

    Matches the bash runner's behaviour at ``ralph/afk.sh:68-73``. The
    validation fires inside the CLI before the loop even runs.
    """
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    monkeypatch.setenv("ISSUE_SOURCE", "gitlab")
    result = subprocess.run(
        _ralph_afk_command(),
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode != 0, (
        "ralph-afk should reject an unknown ISSUE_SOURCE value; "
        f"got exit 0 with stdout={result.stdout!r}"
    )
    assert "ISSUE_SOURCE" in result.stderr, (
        f"expected ISSUE_SOURCE validation message on stderr; "
        f"stderr was:\n{result.stderr}"
    )


def test_ralph_afk_rejects_unknown_max_nmt_strikes(tmp_path, monkeypatch) -> None:
    """A non-integer ``MAX_NMT_STRIKES`` is rejected with a clear error."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    monkeypatch.setenv("MAX_NMT_STRIKES", "fnord")
    result = subprocess.run(
        _ralph_afk_command(),
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode != 0, (
        f"ralph-afk should reject MAX_NMT_STRIKES='fnord'; "
        f"got exit 0 with stdout={result.stdout!r}"
    )
    assert "MAX_NMT_STRIKES" in result.stderr, (
        f"expected MAX_NMT_STRIKES validation message on stderr; "
        f"stderr was:\n{result.stderr}"
    )


def test_ralph_afk_prds_is_not_implemented(tmp_path, monkeypatch) -> None:
    """``ISSUE_SOURCE=prds`` is accepted by the CLI but rejected by the loop.

    Issue #11 lifts this restriction; until then, the loop returns exit
    2 with a clear stderr message rather than silently running
    GitHub-mode logic.
    """
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    monkeypatch.setenv("ISSUE_SOURCE", "prds")
    result = subprocess.run(
        _ralph_afk_command(),
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 2, (
        f"expected exit 2 for unimplemented prds; "
        f"got exit={result.returncode} stderr={result.stderr!r}"
    )
    assert "prds" in result.stderr, (
        f"expected prds message on stderr; stderr was:\n{result.stderr}"
    )


def test_ralph_afk_outside_git_repo_fails_cleanly(tmp_path) -> None:
    """``ralph-afk`` run outside a git repo exits non-zero with a clean message.

    Verifies the early ``resolve_repo_root()`` failure path fires before
    we import the loop module / pricing / Rich.
    """
    # tmp_path is fresh and has no git repo.
    result = subprocess.run(
        _ralph_afk_command(),
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode != 0, (
        f"ralph-afk should fail outside a git repo; "
        f"got exit 0 with stdout={result.stdout!r}"
    )
    # The error message must be friendly, not a traceback.
    assert "Traceback" not in result.stderr, (
        f"expected friendly error, got traceback:\n{result.stderr}"
    )
    assert "git" in result.stderr.lower(), (
        f"expected mention of git in stderr; stderr was:\n{result.stderr}"
    )


def test_ralph_afk_missing_prompt_fails_cleanly(tmp_path) -> None:
    """``ralph-afk`` inside a repo that lacks ``ralph/prompt.md`` fails with a clean message."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    # No ralph/ directory.
    result = subprocess.run(
        _ralph_afk_command(),
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode != 0, (
        "ralph-afk should fail when prompt file is absent; "
        f"got exit 0 with stdout={result.stdout!r}"
    )
    assert "Traceback" not in result.stderr, (
        f"expected friendly error, got traceback:\n{result.stderr}"
    )
    assert "prompt" in result.stderr.lower(), (
        f"expected mention of prompt file in stderr; stderr was:\n{result.stderr}"
    )
