"""``ralph_afk.interactive.picker_app`` — the two-stage Textual picker (issue #24).

The presentation half of the startup **model + reasoning-effort picker**
(decisions D2a-D2d). :class:`ModelPickerApp` renders the picker rows projected by
:mod:`ralph_afk.interactive.models` and returns the operator's
:class:`~ralph_afk.interactive.models.Selection` (or ``None`` if quit):

* **Stage 1 (model)** — a cursor-navigated table of the live models with the
  documented columns (id, display name, premium multiplier, context window,
  reasoning support + default effort). Policy-disabled models are greyed-out
  (dim) and **non-selectable** — ``enter`` on them is a no-op. The cursor is
  pre-highlighted on the env ``MODEL`` / kit default passed in as ``cursor``.
* **Stage 2 (reasoning effort)** — the chosen model's supported efforts, with the
  model default pre-highlighted. **Auto-skipped** when the model supports none:
  selecting such a model exits immediately with ``effort=None``.

``enter`` advances/confirms; ``escape`` steps back from effort to model (or
cancels from the model stage); ``q`` / ``Ctrl+C`` cancel (the orchestrator then
keeps the env/default). This module imports Textual, so — like
:mod:`ralph_afk.interactive.app` — it is reached only on the interactive path,
lazily, after :func:`ralph_afk.interactive.picker.fetch_live_models` succeeds.
The pure row model lives in :mod:`ralph_afk.interactive.models`; everything here
is presentation.
"""

from __future__ import annotations

from typing import Sequence

from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import DataTable, Footer, Static

from ralph_afk.interactive.models import (
    ModelChoice,
    Selection,
    format_context_window,
    format_multiplier,
    format_reasoning,
)

__all__ = ["ModelPickerApp"]

#: Widget ids for the two stage tables, so the shared ``RowSelected`` handler can
#: tell which stage fired (mirrors how :mod:`ralph_afk.interactive.app` routes by
#: ``event.data_table.id``).
_MODEL_TABLE = "picker-models"
_EFFORT_TABLE = "picker-efforts"

_MODEL_COLUMNS = (
    ("Model", "model"),
    ("Name", "name"),
    ("Premium", "premium"),
    ("Context", "context"),
    ("Reasoning", "reasoning"),
)


class ModelPickerApp(App["Selection | None"]):
    """The one-time, two-stage startup picker (model -> reasoning effort)."""

    TITLE = "ralph-afk · pick a model"

    CSS = """
    #picker-title {
        dock: top;
        height: 1;
        padding: 0 1;
        background: $panel;
        color: $text;
    }
    DataTable {
        height: 1fr;
    }
    """

    BINDINGS = [
        Binding("q", "cancel", "Cancel"),
        # Ctrl+C also cancels; priority so it is honoured regardless of focus.
        Binding("ctrl+c", "cancel", "Cancel", priority=True, show=False),
        Binding("escape", "picker_back", "Back"),
    ]

    def __init__(self, choices: Sequence[ModelChoice], *, cursor: int = 0) -> None:
        super().__init__()
        self._choices = list(choices)
        self._cursor = cursor
        self._by_id = {choice.id: choice for choice in self._choices}
        #: The model selected in stage 1 while stage 2 is open (``None`` on the
        #: model stage). ``escape`` reads it to step back vs. cancel.
        self._chosen: ModelChoice | None = None

    def compose(self) -> ComposeResult:
        yield Static(id="picker-title")
        yield DataTable(id=_MODEL_TABLE, cursor_type="row", zebra_stripes=True)
        yield DataTable(id=_EFFORT_TABLE, cursor_type="row")
        yield Footer()

    def on_mount(self) -> None:
        models = self.query_one(f"#{_MODEL_TABLE}", DataTable)
        for label, key in _MODEL_COLUMNS:
            models.add_column(label, key=key)
        for choice in self._choices:
            models.add_row(*self._model_cells(choice), key=choice.id)

        efforts = self.query_one(f"#{_EFFORT_TABLE}", DataTable)
        efforts.add_column("Reasoning effort", key="effort")

        self._show_model_stage()
        if 0 <= self._cursor < len(self._choices):
            models.move_cursor(row=self._cursor)
        models.focus()

    # -- row rendering -----------------------------------------------------

    @staticmethod
    def _model_cells(choice: ModelChoice) -> list[object]:
        """Cells for one model row; greyed-out (dim) + marked when disabled."""
        values = [
            choice.id,
            choice.name + ("" if choice.selectable else " (disabled)"),
            format_multiplier(choice.multiplier),
            format_context_window(choice.context_window),
            format_reasoning(choice),
        ]
        if not choice.selectable:
            return [Text(value, style="dim") for value in values]
        return list(values)

    # -- stage switching ---------------------------------------------------

    def _set_title(self, text: str) -> None:
        self.query_one("#picker-title", Static).update(text)

    def _show_model_stage(self) -> None:
        self._set_title(
            "Select a model   ·   \u2191/\u2193 move   ·   Enter select   ·   q cancel"
        )
        self.query_one(f"#{_MODEL_TABLE}", DataTable).display = True
        self.query_one(f"#{_EFFORT_TABLE}", DataTable).display = False

    def _show_effort_stage(self, choice: ModelChoice) -> None:
        efforts = self.query_one(f"#{_EFFORT_TABLE}", DataTable)
        efforts.clear()
        for effort in choice.supported_efforts:
            label = effort + ("   (default)" if effort == choice.default_effort else "")
            efforts.add_row(label, key=effort)
        self._set_title(
            f"Reasoning effort for {choice.id}   ·   "
            "\u2191/\u2193 move   ·   Enter select   ·   Esc back"
        )
        self.query_one(f"#{_MODEL_TABLE}", DataTable).display = False
        efforts.display = True
        if choice.default_effort in choice.supported_efforts:
            efforts.move_cursor(row=choice.supported_efforts.index(choice.default_effort))
        efforts.focus()

    # -- selection ---------------------------------------------------------

    @on(DataTable.RowSelected)
    def _on_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id == _MODEL_TABLE:
            self._on_model_selected(event)
        elif event.data_table.id == _EFFORT_TABLE:
            self._on_effort_selected(event)

    def _on_model_selected(self, event: DataTable.RowSelected) -> None:
        key = event.row_key.value
        if key is None:
            return
        choice = self._by_id.get(str(key))
        if choice is None or not choice.selectable:
            return  # greyed-out / unknown: Enter is a no-op
        if choice.supported_efforts:
            self._chosen = choice
            self._show_effort_stage(choice)
        else:
            # Auto-skip stage 2: the model supports no reasoning effort.
            self.exit(Selection(choice.id, None))

    def _on_effort_selected(self, event: DataTable.RowSelected) -> None:
        if self._chosen is None:
            return
        key = event.row_key.value
        if key is None:
            return
        self.exit(Selection(self._chosen.id, str(key)))

    # -- navigation actions ------------------------------------------------

    def action_picker_back(self) -> None:
        """Esc: from the effort stage step back to the model stage; else cancel."""
        if self._chosen is not None:
            self._chosen = None
            self._show_model_stage()
            self.query_one(f"#{_MODEL_TABLE}", DataTable).focus()
        else:
            self.exit(None)

    def action_cancel(self) -> None:
        """q / Ctrl+C: quit the picker; the orchestrator keeps the env/default."""
        self.exit(None)
