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


def _install_fake_resolve_run_model(
    monkeypatch: pytest.MonkeyPatch,
    result: tuple[str | None, str | None] | None = None,
) -> None:
    """Stub the startup picker so ``main`` never makes a live ``list_models()``.

    Returns ``result`` (a chosen ``(model, effort)``) when given, else echoes the
    config's env/default — mirroring the picker's own fallback — so the driver
    dispatch is exercised without a TTY or SDK client.
    """

    async def fake_resolve(
        config: RunConfig, *, warn: Any
    ) -> tuple[str | None, str | None]:
        if result is not None:
            return result
        return config.model, config.reasoning_effort

    from ralph_afk.interactive import picker as picker_module

    monkeypatch.setattr(picker_module, "resolve_run_model", fake_resolve)


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
    _install_fake_resolve_run_model(monkeypatch)

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


def test_main_interactive_bakes_picker_selection_into_run_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Issue #24: the run uses *exactly* the model + effort the picker returned.

    The picker's selection is baked into the frozen :class:`RunConfig` the loop
    consumes, overriding the env/default the CLI first composed.
    """
    monkeypatch.setattr(cli_module, "resolve_repo_root", lambda: tmp_path)
    monkeypatch.setattr(cli_module, "_should_run_interactive", lambda args: True)
    # The operator picked a different model + effort than the kit default.
    _install_fake_resolve_run_model(monkeypatch, result=("gpt-5.4", "high"))

    captured: list[tuple[RunConfig, Any]] = []
    _install_fake_loop_run(monkeypatch, captured)

    rc = cli_module.main([])

    assert rc == 0
    cfg, driver = captured[0]
    assert cfg.model == "gpt-5.4"
    assert cfg.reasoning_effort == "high"
    # The driver was built from the *baked* config (its observed state seeds
    # from the chosen model/effort).
    assert driver is not None


def test_main_interactive_no_effort_selection_is_baked(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A reasoning-incapable pick bakes ``reasoning_effort=None`` into the config."""
    monkeypatch.setattr(cli_module, "resolve_repo_root", lambda: tmp_path)
    monkeypatch.setattr(cli_module, "_should_run_interactive", lambda args: True)
    _install_fake_resolve_run_model(monkeypatch, result=("claude-opus-4.5", None))

    captured: list[tuple[RunConfig, Any]] = []
    _install_fake_loop_run(monkeypatch, captured)

    cli_module.main([])

    cfg, _driver = captured[0]
    assert cfg.model == "claude-opus-4.5"
    assert cfg.reasoning_effort is None
