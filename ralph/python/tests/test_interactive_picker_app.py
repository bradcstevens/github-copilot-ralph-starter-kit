"""Pilot tests for ``ralph_afk.interactive.picker_app`` (issue #24).

Gated behind ``pytest.importorskip("textual")`` so the base (no ``[tui]`` extra)
install skips them. They drive the real :class:`ModelPickerApp` via Textual's
Pilot to prove the acceptance behaviours that need a running app:

* selecting an enabled model with efforts advances to stage 2 and returns the
  chosen ``Selection`` (with the model default pre-highlighted);
* a model with no supported reasoning efforts **auto-skips** stage 2 and returns
  ``effort=None``;
* a policy-disabled model is greyed-out and **cannot** be selected; and
* the cursor is pre-highlighted on the index the orchestrator passes in.

The pure projection + the orchestration/fallback are unit-tested (ungated) in
``test_interactive_models.py`` / ``test_interactive_picker.py``.
"""

from __future__ import annotations

import pytest

pytest.importorskip("textual")

from textual.widgets import DataTable  # noqa: E402

from ralph_afk.interactive.models import ModelChoice, Selection  # noqa: E402
from ralph_afk.interactive.picker_app import ModelPickerApp  # noqa: E402


def _choice(
    id: str,
    *,
    efforts: tuple[str, ...] = (),
    default: str | None = None,
    selectable: bool = True,
) -> ModelChoice:
    return ModelChoice(
        id=id,
        name=id.upper(),
        multiplier=1.0,
        context_window=200_000,
        supports_reasoning=bool(efforts),
        default_effort=default or (efforts[0] if efforts else None),
        supported_efforts=efforts,
        selectable=selectable,
        policy_state="enabled" if selectable else "disabled",
    )


async def test_select_model_then_effort_returns_selection() -> None:
    choices = [_choice("a", efforts=("low", "high"), default="high"), _choice("b")]
    app = ModelPickerApp(choices, cursor=0)
    async with app.run_test() as pilot:
        # Stage 1: cursor on "a" (has efforts) -> Enter advances to stage 2.
        await pilot.press("enter")
        await pilot.pause()
        efforts = app.query_one("#picker-efforts", DataTable)
        assert efforts.display is True
        assert app.query_one("#picker-models", DataTable).display is False
        # The model default ("high", index 1) is pre-highlighted.
        assert efforts.cursor_row == 1
        # Stage 2: Enter selects the highlighted effort and exits.
        await pilot.press("enter")
        await pilot.pause()
    assert app.return_value == Selection("a", "high")


async def test_effort_arrow_then_enter_selects_non_default() -> None:
    choices = [_choice("a", efforts=("low", "high"), default="high")]
    app = ModelPickerApp(choices, cursor=0)
    async with app.run_test() as pilot:
        await pilot.press("enter")  # -> stage 2 (cursor on default "high")
        await pilot.pause()
        await pilot.press("up")  # move to "low"
        await pilot.press("enter")
        await pilot.pause()
    assert app.return_value == Selection("a", "low")


async def test_model_with_no_efforts_auto_skips_stage_two() -> None:
    app = ModelPickerApp([_choice("noreason")], cursor=0)
    async with app.run_test() as pilot:
        await pilot.press("enter")  # no efforts -> exits immediately
        await pilot.pause()
    assert app.return_value == Selection("noreason", None)


async def test_disabled_model_is_greyed_out_and_not_selectable() -> None:
    choices = [_choice("dis", selectable=False), _choice("ok", efforts=("high",))]
    app = ModelPickerApp(choices, cursor=0)  # cursor starts on the disabled row
    async with app.run_test() as pilot:
        table = app.query_one("#picker-models", DataTable)
        # The disabled row is marked (and rendered dim).
        assert "disabled" in str(table.get_row_at(0)[1])
        await pilot.press("enter")  # Enter on the disabled row is a no-op
        await pilot.pause()
        # Still on stage 1, app still running, no selection made.
        assert app.is_running is True
        assert table.display is True
        assert app.query_one("#picker-efforts", DataTable).display is False
    assert app.return_value is None


async def test_cursor_is_prehighlighted_on_given_index() -> None:
    choices = [
        _choice("a", efforts=("high",)),
        _choice("b", efforts=("high",)),
        _choice("c", efforts=("high",)),
    ]
    app = ModelPickerApp(choices, cursor=2)
    async with app.run_test():
        assert app.query_one("#picker-models", DataTable).cursor_row == 2


async def test_q_cancels_with_no_selection() -> None:
    app = ModelPickerApp([_choice("a", efforts=("high",))], cursor=0)
    async with app.run_test() as pilot:
        await pilot.press("q")
        await pilot.pause()
    assert app.return_value is None


async def test_esc_steps_back_from_effort_to_model_stage() -> None:
    app = ModelPickerApp([_choice("a", efforts=("low", "high"))], cursor=0)
    async with app.run_test() as pilot:
        await pilot.press("enter")  # -> stage 2
        await pilot.pause()
        assert app.query_one("#picker-efforts", DataTable).display is True
        await pilot.press("escape")  # back to stage 1
        await pilot.pause()
        assert app.query_one("#picker-models", DataTable).display is True
        assert app.query_one("#picker-efforts", DataTable).display is False
        assert app.is_running is True
