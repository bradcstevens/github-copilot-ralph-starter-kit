"""Tests for the interactive wiring in :mod:`ralph_afk.cli` (issue #23).

Covers the ``--interactive`` / ``--no-interactive`` tri-state flag and that
:func:`ralph_afk.cli.main` dispatches to ``loop.run`` with a driver on the
interactive path and without one otherwise. ``loop.run`` and the driver builder
are faked so no SDK client or Textual app is constructed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ralph_afk import cli as cli_module
from ralph_afk.config import RunConfig


# ---------------------------------------------------------------------------
# Flag parsing (tri-state)
# ---------------------------------------------------------------------------


def test_interactive_flag_defaults_to_none() -> None:
    args = cli_module.build_parser().parse_args([])
    assert args.interactive is None


def test_interactive_flag_true() -> None:
    args = cli_module.build_parser().parse_args(["--interactive"])
    assert args.interactive is True


def test_no_interactive_flag_false() -> None:
    args = cli_module.build_parser().parse_args(["--no-interactive"])
    assert args.interactive is False


# ---------------------------------------------------------------------------
# _should_run_interactive wiring (delegates to detect.resolve_interactive)
# ---------------------------------------------------------------------------


def test_should_run_interactive_respects_no_interactive_flag() -> None:
    args = cli_module.build_parser().parse_args(["--no-interactive"])
    assert cli_module._should_run_interactive(args) is False


def test_should_run_interactive_env_zero_forces_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RALPH_INTERACTIVE", "0")
    args = cli_module.build_parser().parse_args([])
    assert cli_module._should_run_interactive(args) is False


# ---------------------------------------------------------------------------
# main() dispatch
# ---------------------------------------------------------------------------


def _install_fake_loop_run(
    monkeypatch: pytest.MonkeyPatch, captured: list[tuple[RunConfig, Any]]
) -> None:
    async def fake_run(cfg: RunConfig, *, driver: Any = None) -> int:
        captured.append((cfg, driver))
        return 0

    from ralph_afk import loop as loop_module

    monkeypatch.setattr(loop_module, "run", fake_run)


def test_main_non_interactive_passes_no_driver(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(cli_module, "resolve_repo_root", lambda: tmp_path)
    monkeypatch.setattr(cli_module, "_should_run_interactive", lambda args: False)
    captured: list[tuple[RunConfig, Any]] = []
    _install_fake_loop_run(monkeypatch, captured)

    rc = cli_module.main([])

    assert rc == 0
    assert len(captured) == 1
    _cfg, driver = captured[0]
    assert driver is None


def test_main_interactive_builds_and_passes_driver(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(cli_module, "resolve_repo_root", lambda: tmp_path)
    monkeypatch.setattr(cli_module, "_should_run_interactive", lambda args: True)

    sentinel = object()
    import ralph_afk.interactive.driver as driver_module

    monkeypatch.setattr(
        driver_module, "build_interactive_driver", lambda config: sentinel
    )

    captured: list[tuple[RunConfig, Any]] = []
    _install_fake_loop_run(monkeypatch, captured)

    rc = cli_module.main([])

    assert rc == 0
    assert len(captured) == 1
    _cfg, driver = captured[0]
    assert driver is sentinel
