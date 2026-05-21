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

    Matches the bash runner's behaviour at ``ralph/sh-afk.sh:68-73``. The
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


def test_ralph_afk_prds_empty_pool_exits_zero(tmp_path, monkeypatch) -> None:
    """``ISSUE_SOURCE=prds`` with no ``prds/`` directory exits 0 cleanly.

    PRDs mode is now implemented (issue #11). Without a ``prds/``
    directory, :meth:`PrdsIssueSource.collect_afk_ready` returns ``[]``
    which the loop treats as the empty-pool fast path → exit 0.

    Also satisfies the ``docs/agents/issue-tracker.md`` preflight by
    creating a stub config file — that preflight refuses to start the
    loop until ``/setup-agent-skills`` has been run, regardless of
    ``ISSUE_SOURCE`` (the agent-skills config is needed by downstream
    skills in both modes).
    """
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    # Provide a prompt file so we don't fail on prompt resolution.
    (tmp_path / "ralph").mkdir()
    (tmp_path / "ralph" / "PROMPT.md").write_text("be ralph", encoding="utf-8")
    # Satisfy the agent-skills preflight (mirrors what /setup-agent-skills
    # writes for a local-markdown / 'other' issue-tracker choice).
    (tmp_path / "docs" / "agents").mkdir(parents=True)
    (tmp_path / "docs" / "agents" / "issue-tracker.md").write_text(
        "Stub: local-markdown issue tracker.\n", encoding="utf-8"
    )
    monkeypatch.setenv("ISSUE_SOURCE", "prds")
    result = subprocess.run(
        _ralph_afk_command(),
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"expected exit 0 on empty PRDs pool; "
        f"got exit={result.returncode} stderr={result.stderr!r}"
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


def test_ralph_afk_missing_agent_skills_config_fails_cleanly(tmp_path) -> None:
    """``ralph-afk`` refuses to start when ``docs/agents/issue-tracker.md`` is missing.

    The AFK loop cannot safely run ``/setup-agent-skills`` itself (the
    skill is interactive and would force the agent to invent answers
    under ``copilot --yolo -p``). The CLI preflight detects an
    unconfigured repo by the absence of ``docs/agents/issue-tracker.md``
    and refuses to start with a clear, non-traceback error pointing the
    operator at ``/setup-agent-skills``.

    Mirrors the bash runner's equivalent preflight in ``ralph/sh-afk.sh``.
    """
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    # Provide a prompt file so the prompt-not-found preflight does not
    # short-circuit before the agent-skills preflight fires.
    (tmp_path / "ralph").mkdir()
    (tmp_path / "ralph" / "PROMPT.md").write_text("be ralph", encoding="utf-8")
    # Deliberately do NOT create docs/agents/issue-tracker.md.
    result = subprocess.run(
        _ralph_afk_command(),
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode != 0, (
        "ralph-afk should fail when docs/agents/issue-tracker.md is missing; "
        f"got exit 0 with stdout={result.stdout!r}"
    )
    assert "Traceback" not in result.stderr, (
        f"expected friendly error, got traceback:\n{result.stderr}"
    )
    assert "docs/agents/issue-tracker.md" in result.stderr, (
        f"expected mention of docs/agents/issue-tracker.md in stderr; "
        f"stderr was:\n{result.stderr}"
    )
    assert "/setup-agent-skills" in result.stderr, (
        f"expected mention of /setup-agent-skills in stderr; "
        f"stderr was:\n{result.stderr}"
    )


def test_ralph_afk_missing_prompt_fails_cleanly(tmp_path) -> None:
    """``ralph-afk`` inside a repo that lacks ``ralph/PROMPT.md`` fails with a clean message."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    # Satisfy the agent-skills preflight so the prompt-file check is the
    # next preflight to fire (this test pins the prompt-file contract).
    (tmp_path / "docs" / "agents").mkdir(parents=True)
    (tmp_path / "docs" / "agents" / "issue-tracker.md").write_text(
        "Stub: local-markdown issue tracker.\n", encoding="utf-8"
    )
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


def test_disabled_otel_does_not_import_opentelemetry() -> None:
    """OTel posture: with telemetry disabled, ``opentelemetry`` MUST NOT
    appear in ``sys.modules`` after importing the full ralph_afk surface.

    This is the load-bearing contract for issue #12: the ``[otel]`` extra
    is opt-in. Operators who haven't installed it (or who haven't set
    ``RALPH_OTEL_ENABLED`` / ``OTEL_EXPORTER_OTLP_ENDPOINT``) MUST never
    pay the OTel import cost — and crucially, MUST NOT trip an
    ``ImportError`` traceback at module-load time on the base install.

    Asserted by spawning a fresh Python subprocess (with both env vars
    unset) that imports ``ralph_afk.loop`` (the heaviest module that
    touches the telemetry seam) and the telemetry seam itself, then
    exits zero iff ``opentelemetry`` is absent from ``sys.modules``.
    """
    script = (
        "import os, sys\n"
        # Belt-and-braces: ensure no env-var hint that would enable OTel.
        "os.environ.pop('RALPH_OTEL_ENABLED', None)\n"
        "os.environ.pop('OTEL_EXPORTER_OTLP_ENDPOINT', None)\n"
        "import ralph_afk.loop  # noqa: F401\n"
        "import ralph_afk.telemetry.otel  # noqa: F401\n"
        "from ralph_afk.telemetry import otel\n"
        # Exercise the public seam — these MUST NOT import opentelemetry
        # when the seam is disabled.
        "assert otel.is_enabled() is False\n"
        "assert otel.build_sdk_telemetry_config() is None\n"
        "with otel.span('smoke') as s:\n"
        "    s.set_attribute('k', 'v')\n"
        "otel.force_flush()\n"
        "leaked = [m for m in sys.modules if m == 'opentelemetry' "
        "or m.startswith('opentelemetry.')]\n"
        "if leaked:\n"
        "    print('LEAK: ' + ','.join(leaked))\n"
        "    sys.exit(2)\n"
        "print('OK')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
        env={
            # Inherit just enough to find the venv; explicitly drop the
            # OTel env vars in case the host shell has them set.
            **{
                k: v
                for k, v in __import__("os").environ.items()
                if k not in ("RALPH_OTEL_ENABLED", "OTEL_EXPORTER_OTLP_ENDPOINT")
            },
        },
    )
    assert result.returncode == 0, (
        f"expected exit 0 with 'OK'; got exit {result.returncode}\n"
        f"stdout={result.stdout!r}\n"
        f"stderr={result.stderr!r}"
    )
    assert "OK" in result.stdout, (
        f"expected 'OK' in stdout; got stdout={result.stdout!r}"
    )
    assert "LEAK" not in result.stdout, (
        f"opentelemetry leaked into sys.modules even when disabled:\n"
        f"{result.stdout}"
    )
