"""Tests for the PR-surface gating resolution.

Two layers cooperate to decide whether ``ready-for-agent`` PRs join the
AFK pool:

* :func:`ralph_afk.cli._resolve_include_prs` reads the ``INCLUDE_PRS`` env
  override and returns ``True`` / ``False`` / ``None`` (no override).
* :func:`ralph_afk.loop._resolve_include_prs` applies precedence: an explicit
  :attr:`RunConfig.include_prs` wins, otherwise it auto-detects the
  ``PRs as a request surface: yes/no`` flag in
  ``docs/agents/issue-tracker.md`` (default off).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ralph_afk import cli as cli_module
from ralph_afk import loop as loop_module
from ralph_afk.config import RunConfig


def _write_tracker(repo_root: Path, text: str) -> None:
    agents = repo_root / "docs" / "agents"
    agents.mkdir(parents=True, exist_ok=True)
    (agents / "issue-tracker.md").write_text(text, encoding="utf-8")


# --------------------------------------------------------------------------- #
# cli._resolve_include_prs (env override)                                     #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "value, expected",
    [
        ("1", True),
        ("true", True),
        ("TRUE", True),
        ("yes", True),
        ("on", True),
        ("  on  ", True),
        ("0", False),
        ("false", False),
        ("no", False),
        ("off", False),
        ("garbage", False),
    ],
)
def test_cli_resolve_env_values(
    monkeypatch: pytest.MonkeyPatch, value: str, expected: bool
) -> None:
    monkeypatch.setenv("INCLUDE_PRS", value)
    assert cli_module._resolve_include_prs() is expected


def test_cli_resolve_unset_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("INCLUDE_PRS", raising=False)
    assert cli_module._resolve_include_prs() is None


def test_cli_resolve_blank_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INCLUDE_PRS", "   ")
    assert cli_module._resolve_include_prs() is None


# --------------------------------------------------------------------------- #
# loop._resolve_include_prs (config override + file auto-detect)              #
# --------------------------------------------------------------------------- #


def test_loop_resolve_config_true_wins(tmp_path: Path) -> None:
    cfg = RunConfig(include_prs=True)
    assert loop_module._resolve_include_prs(cfg, tmp_path) is True


def test_loop_resolve_config_false_wins_over_file_yes(tmp_path: Path) -> None:
    _write_tracker(tmp_path, "**PRs as a request surface: yes.**\n")
    cfg = RunConfig(include_prs=False)
    assert loop_module._resolve_include_prs(cfg, tmp_path) is False


def test_loop_resolve_missing_file_is_false(tmp_path: Path) -> None:
    cfg = RunConfig(include_prs=None)
    assert loop_module._resolve_include_prs(cfg, tmp_path) is False


def test_loop_resolve_file_yes_enables(tmp_path: Path) -> None:
    _write_tracker(
        tmp_path, "Some preamble.\n\n**PRs as a request surface: yes.**\n"
    )
    cfg = RunConfig(include_prs=None)
    assert loop_module._resolve_include_prs(cfg, tmp_path) is True


def test_loop_resolve_file_no_disables(tmp_path: Path) -> None:
    _write_tracker(tmp_path, "**PRs as a request surface: no.**\n")
    cfg = RunConfig(include_prs=None)
    assert loop_module._resolve_include_prs(cfg, tmp_path) is False


def test_loop_resolve_file_without_flag_is_false(tmp_path: Path) -> None:
    _write_tracker(tmp_path, "# Issue tracker\n\nNo PR flag here.\n")
    cfg = RunConfig(include_prs=None)
    assert loop_module._resolve_include_prs(cfg, tmp_path) is False


def test_loop_resolve_flag_is_case_insensitive(tmp_path: Path) -> None:
    _write_tracker(tmp_path, "prs AS a request surface: YES\n")
    cfg = RunConfig(include_prs=None)
    assert loop_module._resolve_include_prs(cfg, tmp_path) is True
