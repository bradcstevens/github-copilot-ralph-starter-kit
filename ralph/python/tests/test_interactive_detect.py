"""Tests for ``ralph_afk.interactive.detect`` (issue #23 — interactive gating).

Pins the precedence (flag > env > TTY) and the ``[tui]``-extra requirement,
including the warn-only-when-explicit fallback. Pure — no TTY required.
"""

from __future__ import annotations

import ast
from pathlib import Path

from ralph_afk.interactive import detect as detect_module
from ralph_afk.interactive.detect import resolve_interactive, textual_available


def _resolve(
    *,
    flag: bool | None = None,
    env_value: str | None = None,
    isatty: bool = False,
    textual_importable: bool = True,
    warnings: list[str] | None = None,
) -> bool:
    """resolve_interactive with sensible, overridable defaults + a warn sink."""
    sink = warnings if warnings is not None else []
    return resolve_interactive(
        flag=flag,
        env_value=env_value,
        isatty=isatty,
        textual_importable=textual_importable,
        warn=sink.append,
    )


# ---------------------------------------------------------------------------
# Auto-detect from TTY
# ---------------------------------------------------------------------------


def test_tty_without_flags_is_interactive() -> None:
    assert _resolve(isatty=True) is True


def test_non_tty_without_flags_is_not_interactive() -> None:
    assert _resolve(isatty=False) is False


# ---------------------------------------------------------------------------
# Explicit flag wins over everything
# ---------------------------------------------------------------------------


def test_no_interactive_flag_overrides_tty_and_env() -> None:
    assert _resolve(flag=False, isatty=True, env_value="1") is False


def test_interactive_flag_overrides_non_tty_and_env() -> None:
    assert _resolve(flag=True, isatty=False, env_value="0") is True


# ---------------------------------------------------------------------------
# Env override sits between flag and TTY
# ---------------------------------------------------------------------------


def test_env_one_forces_interactive_on_non_tty() -> None:
    assert _resolve(env_value="1", isatty=False) is True


def test_env_zero_forces_non_interactive_on_tty() -> None:
    assert _resolve(env_value="0", isatty=True) is False


def test_blank_env_is_ignored_and_falls_back_to_tty() -> None:
    assert _resolve(env_value="   ", isatty=True) is True
    assert _resolve(env_value="", isatty=False) is False


# ---------------------------------------------------------------------------
# [tui] extra requirement
# ---------------------------------------------------------------------------


def test_missing_textual_falls_back_even_on_tty() -> None:
    warnings: list[str] = []
    # Auto-detected intent (TTY) → silent fallback, no warning.
    assert _resolve(isatty=True, textual_importable=False, warnings=warnings) is False
    assert warnings == []


def test_explicit_request_without_textual_warns_and_falls_back() -> None:
    warnings: list[str] = []
    assert (
        _resolve(flag=True, textual_importable=False, warnings=warnings) is False
    )
    assert len(warnings) == 1
    assert "tui" in warnings[0].lower()


def test_env_request_without_textual_warns_and_falls_back() -> None:
    warnings: list[str] = []
    assert (
        _resolve(env_value="1", textual_importable=False, warnings=warnings)
        is False
    )
    assert len(warnings) == 1


def test_non_interactive_intent_does_not_probe_or_warn_about_textual() -> None:
    warnings: list[str] = []
    # --no-interactive with Textual missing: no warning, just False.
    assert (
        _resolve(flag=False, textual_importable=False, warnings=warnings) is False
    )
    assert warnings == []


# ---------------------------------------------------------------------------
# textual_available probe + import guard
# ---------------------------------------------------------------------------


def test_textual_available_returns_bool() -> None:
    # In this dev venv the [tui] extra is installed, so it should be True;
    # the contract under test is simply that it returns a bool without raising.
    assert isinstance(textual_available(), bool)


def test_detect_module_imports_are_constrained() -> None:
    """``detect.py`` is pure: stdlib + ``typing`` only — never imports Textual."""
    source = Path(detect_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    allow = {"__future__", "importlib.util", "typing"}
    seen: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                seen.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0
            assert node.module is not None
            seen.add(node.module)
    leaked = seen - allow
    assert not leaked, f"detect.py imports non-allowlisted modules: {leaked}"
    assert "textual" not in seen
