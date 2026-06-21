"""``ralph_afk.interactive.detect`` — interactive-path gating (issue #23).

Decides whether one ``ralph-afk`` invocation takes the **interactive** path (a
Textual app observing the loop) or stays on today's exact line-printer behavior.
Deep + pure (stdlib + ``typing`` only — no Textual), so the decision is
unit-testable without a TTY and importing it never costs a Textual import.

Precedence (highest first):

1. The explicit ``--interactive`` / ``--no-interactive`` flag.
2. The ``RALPH_INTERACTIVE`` env override (``1``/``true``/... vs ``0``/...).
3. Auto-detect from TTY-ness (interactive only when stdout is a terminal).

Whatever the resolved *intent*, the interactive path additionally requires the
optional ``[tui]`` extra (Textual) to be importable. When interactivity was
**explicitly** requested (flag or env) but Textual is missing, a warning is
emitted and the run falls back to the line printer; when interactivity was only
auto-detected, the fallback is silent. Every non-interactive outcome (non-TTY,
``--no-interactive``, ``RALPH_INTERACTIVE=0``, or ``[tui]`` absent) yields
today's byte-for-byte line-printer behavior.
"""

from __future__ import annotations

import importlib.util
from typing import Callable

__all__ = ["resolve_interactive", "textual_available"]

_TRUTHY = {"1", "true", "yes", "on"}


def textual_available() -> bool:
    """Return whether the optional ``[tui]`` extra (Textual) is importable.

    Uses :func:`importlib.util.find_spec` so the probe does **not** actually
    import Textual (no screen/curses side effects) — it only checks that the
    package could be imported.
    """
    try:
        return importlib.util.find_spec("textual") is not None
    except (ImportError, ValueError):  # pragma: no cover - defensive
        return False


def _env_is_set(value: str | None) -> bool:
    return value is not None and bool(value.strip())


def _is_truthy(value: str | None) -> bool:
    return value is not None and value.strip().lower() in _TRUTHY


def resolve_interactive(
    *,
    flag: bool | None,
    env_value: str | None,
    isatty: bool,
    textual_importable: bool,
    warn: Callable[[str], None],
) -> bool:
    """Resolve the interactive path from flag / env / TTY plus Textual presence.

    Args:
        flag: Tri-state ``--interactive`` (``True``) / ``--no-interactive``
            (``False``) / neither (``None``).
        env_value: Raw ``RALPH_INTERACTIVE`` value (``None``/blank = unset).
        isatty: Whether the runner's stdout is a terminal.
        textual_importable: Whether the ``[tui]`` extra is importable
            (typically :func:`textual_available`).
        warn: Non-fatal warning sink, used only when interactivity was
            explicitly requested but the ``[tui]`` extra is missing.

    Returns:
        ``True`` to take the interactive path; ``False`` to keep the
        line printer.
    """
    explicit = flag is not None or _env_is_set(env_value)

    if flag is not None:
        intent = flag
    elif _env_is_set(env_value):
        intent = _is_truthy(env_value)
    else:
        intent = isatty

    if not intent:
        return False

    if not textual_importable:
        if explicit:
            warn(
                "interactive mode was requested but the optional [tui] extra "
                "(Textual) is not importable; falling back to the line "
                "printer. Install it with: pip install 'ralph-afk[tui]'"
            )
        return False

    return True
