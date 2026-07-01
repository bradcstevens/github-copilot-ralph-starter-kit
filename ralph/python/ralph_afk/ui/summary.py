"""``ralph_afk.ui.summary`` — per-iteration counter accumulator + frozen UI artefacts.

This module owns the data side of the UI: it accumulates per-iteration
counter snapshots from the event stream and constructs the two frozen
artefacts the renderer prints:

* The **iteration ``Panel``** at iteration end (rendered once, never
  re-drawn — preserved verbatim in scrollback).
* The **run-end ``Table``** at run end (one row per completed iteration
  plus a totals footer).

The renderer drives this module via :meth:`RunSummary.on_iteration_start`,
:meth:`RunSummary.on_iteration_end`, and the per-event accumulator
methods. The loop slice (#10) reads :attr:`RunSummary.completed` to
translate iteration snapshots into :class:`ralph_afk.persist.IterationCounters`
records via :meth:`IterationSnapshot.to_counters` — that's the explicit
conversion seam between the UI's display dataclass and the persist
module's storage dataclass.

Design notes:

* **Two-dataclass posture.** :class:`IterationSnapshot` (UI) and
  :class:`ralph_afk.persist.IterationCounters` (persist) intentionally
  do NOT share a base. Keeping them separate prevents UI concerns
  (timestamps, issue numbers, transient pricing state) from polluting
  the persisted JSON schema, and prevents persist-only fields (which
  may grow over time) from forcing UI redraws. The
  :meth:`IterationSnapshot.to_counters` method is the deterministic
  conversion seam.
* **First non-None model wins — via the shared UsageTally.** Some SDK
  versions emit ``usage.tokens`` events with ``model=None``; the tally
  retains the first authoritative model name and ignores subsequent
  ``None``s. A later non-``None`` model ALSO does not overwrite — keeps the
  iteration's recorded model stable even if the SDK changes models
  mid-iteration (which would be unusual but not crashy). That rule (and the
  unknown-model cost guard) is now the :class:`~ralph_afk.usage.UsageTally`'s
  single implementation, shared with the Queue's per-issue sink — no second
  copy lives here.
* **Strikes are cumulative-aware.** A ``WRAPPER_STRIKE`` event carrying
  a ``strikes`` integer is used verbatim (the value is the wrapper's
  authoritative count after the iteration). Absent that key, each
  STRIKE event increments the counter — a marker form for diagnostic
  use.
* **context_used = tokens_in + tokens_out.** Read straight off the tally
  (:attr:`~ralph_afk.usage.UsageTally.total_tokens`); matches the schema
  example in :mod:`ralph_afk.persist`. Labelled as "observed tokens" in the
  rendered panel so the operator doesn't read it as live model
  context-window pressure.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from rich.box import ROUNDED, SIMPLE
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ralph_afk.pricing import Pricing, context_utilisation
from ralph_afk.usage import UsageTally

from .console import STYLES


__all__ = ["IterationSnapshot", "RunSummary", "RunTotals"]


# Highlight threshold: matches the PRD's "Smart Zone Ceiling" cue. When
# context utilisation reaches half the model's window we start drawing
# attention to the cost / context line.
_CONTEXT_HIGH_WATERMARK: float = 0.5


# ---------------------------------------------------------------------------
# IterationSnapshot
# ---------------------------------------------------------------------------


@dataclass
class IterationSnapshot:
    """Per-iteration counter accumulator. Mutable while the iteration is
    in progress; frozen (by convention) once :meth:`RunSummary.on_iteration_end`
    appends it to :attr:`RunSummary.completed`.

    Fields parallel the persist schema where they overlap but include
    extra UI-only fields (``issue_num``, timestamps) that don't belong
    in the persisted JSON. The per-iteration **Consumption** (tokens + the
    model they were billed against) lives in a shared
    :class:`~ralph_afk.usage.UsageTally`; ``model`` / ``tokens_in`` /
    ``tokens_out`` remain as thin read-only accessors onto it so existing
    render call sites and the persist seam read unchanged.
    """

    iter_num: int
    issue_num: Optional[int] = None
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    usage: UsageTally = field(default_factory=UsageTally)
    tool_count: int = 0
    skill_count: int = 0
    commits: int = 0
    auto_closures: int = 0
    strikes: int = 0

    @property
    def model(self) -> Optional[str]:
        """The model this iteration's **Consumption** was billed against."""
        return self.usage.model

    @property
    def tokens_in(self) -> int:
        """Input tokens observed this iteration."""
        return self.usage.tokens_in

    @property
    def tokens_out(self) -> int:
        """Output tokens observed this iteration."""
        return self.usage.tokens_out

    @property
    def context_used(self) -> int:
        """Observed-tokens proxy for context occupancy.

        Sum of input + output tokens within this iteration (delegated to
        :attr:`~ralph_afk.usage.UsageTally.total_tokens`). Labelled
        "observed tokens" in the rendered panel; not a true live
        model-context measurement (multiple turns within a session would
        double-count input tokens, which already include prior history).
        """
        return self.usage.total_tokens

    @property
    def duration_seconds(self) -> float:
        """Wall-clock duration in seconds, or ``0.0`` if not yet closed."""
        if self.started_at is None or self.ended_at is None:
            return 0.0
        return (self.ended_at - self.started_at).total_seconds()

    def cost_usd(self, pricing: Pricing) -> Optional[Decimal]:
        """Compute the iteration's estimated cost, or ``None`` for unknown model.

        Delegates to :meth:`~ralph_afk.usage.UsageTally.cost`, which carries
        the ``None``/unknown-model guard so callers render the em dash.
        """
        return self.usage.cost(pricing)

    def to_counters_kwargs(self, *, pricing: Pricing) -> dict:
        """Return a kwargs dict suitable for constructing
        :class:`ralph_afk.persist.IterationCounters`.

        Returning a dict (rather than an :class:`IterationCounters`
        instance directly) keeps this UI module's import graph free of
        ``ralph_afk.persist``. The loop slice (#10) does::

            from ralph_afk.persist import IterationCounters
            counters = IterationCounters(**snap.to_counters_kwargs(pricing=p))

        The conversion logic (e.g. ``context_used = tokens_in + tokens_out``)
        lives here because it's UI-data-shape concern; the loop just
        wires the field names through. If
        :class:`ralph_afk.persist.IterationCounters` ever gains a new
        required field the runner cares about, this method is the one
        place that needs to know.
        """
        return {
            "iter": self.iter_num,
            "duration_seconds": self.duration_seconds,
            "model": self.usage.model,
            "tokens_in": self.usage.tokens_in,
            "tokens_out": self.usage.tokens_out,
            "context_used": self.context_used,
            "est_cost_usd": self.cost_usd(pricing),
            "tool_count": self.tool_count,
            "skill_count": self.skill_count,
            "commits": self.commits,
            "auto_closures": self.auto_closures,
            "strikes": self.strikes,
        }


# ---------------------------------------------------------------------------
# RunTotals
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RunTotals:
    """Totals view computed from :attr:`RunSummary.completed`.

    Used by the run-end Table footer and exposed publicly so the loop
    slice (#10) can read the same numbers without duplicating the
    accumulation logic.
    """

    iterations: int
    tokens_in: int
    tokens_out: int
    cost_usd: Optional[Decimal]
    commits: int
    auto_closures: int
    final_strikes: int


# ---------------------------------------------------------------------------
# RunSummary
# ---------------------------------------------------------------------------


@dataclass
class RunSummary:
    """Aggregate per-iteration snapshots; builds the frozen UI artefacts.

    Owned by the caller (typically the loop slice) and passed to the
    :class:`~ralph_afk.ui.renderer.Renderer`. The renderer subscribes to
    events and drives the accumulator methods below; the caller reads
    :attr:`completed` (and optionally :meth:`totals`) to translate to
    persist-side records.

    Attributes:
        pricing: :class:`ralph_afk.pricing.Pricing` table used for cost
            estimation and context-utilisation thresholding.
        pricing_date: Optional ISO date label (e.g. ``"2026-05-16"``)
            surfaced alongside the cost line. ``None`` omits the suffix.
        current: The in-progress :class:`IterationSnapshot`, or ``None``
            between iterations.
        completed: Frozen snapshots in iteration order.
    """

    pricing: Pricing
    pricing_date: Optional[str] = None
    current: Optional[IterationSnapshot] = None
    completed: list[IterationSnapshot] = field(default_factory=list)

    # -- iteration lifecycle ------------------------------------------------

    def on_iteration_start(
        self, *, iter_num: int, issue_num: Optional[int] = None
    ) -> IterationSnapshot:
        """Open a new snapshot at the start of an iteration."""
        snap = IterationSnapshot(
            iter_num=iter_num,
            issue_num=issue_num,
            started_at=datetime.now(timezone.utc),
        )
        self.current = snap
        return snap

    def on_iteration_end(self) -> Optional[IterationSnapshot]:
        """Close the current snapshot, append to :attr:`completed`, return it.

        Returns ``None`` if no iteration is currently open (a stray
        ``WRAPPER_ITERATION_END`` — e.g. the abort path — must not crash).
        """
        snap = self.current
        if snap is None:
            return None
        snap.ended_at = datetime.now(timezone.utc)
        self.completed.append(snap)
        self.current = None
        return snap

    # -- per-event accumulators --------------------------------------------

    def record_usage(self, *, model: Optional[str], tokens_in: int, tokens_out: int) -> None:
        snap = self.current
        if snap is None:
            return
        # Fold this usage sample into the iteration's shared UsageTally. The
        # accrual rule (first non-None model wins; tokens sum) lives entirely in
        # UsageTally.add; the sink keeps its own int(x or 0) input sanitization.
        snap.usage.add(model, int(tokens_in or 0), int(tokens_out or 0))

    def record_tool_call(self, *, tool_name: str) -> None:
        snap = self.current
        if snap is None:
            return
        snap.tool_count += 1
        if tool_name == "skill":
            snap.skill_count += 1

    def record_commit(self) -> None:
        snap = self.current
        if snap is None:
            return
        snap.commits += 1

    def record_auto_close(self) -> None:
        snap = self.current
        if snap is None:
            return
        snap.auto_closures += 1

    def record_strike(self, *, strikes: Optional[int] = None) -> None:
        snap = self.current
        if snap is None:
            return
        if strikes is not None:
            snap.strikes = int(strikes)
        else:
            snap.strikes += 1

    # -- rollup -------------------------------------------------------------

    def totals(self) -> RunTotals:
        """Aggregate counters across :attr:`completed` iterations.

        Cost only sums iterations whose model was in the pricing table —
        unknown-model iterations contribute ``None`` and are skipped, so
        the totals row never silently understates cost by treating
        unknown as zero.

        ``final_strikes`` is the last completed iteration's strike count
        (not the sum) — strikes reset on progress in the wrapper
        contract, so summing would mislead.
        """
        tokens_in = sum(s.tokens_in for s in self.completed)
        tokens_out = sum(s.tokens_out for s in self.completed)
        commits = sum(s.commits for s in self.completed)
        auto_closures = sum(s.auto_closures for s in self.completed)
        priced_costs = [
            s.cost_usd(self.pricing)
            for s in self.completed
        ]
        defined_costs = [c for c in priced_costs if c is not None]
        cost_usd: Optional[Decimal]
        if defined_costs:
            cost_usd = sum(defined_costs, Decimal(0))
        else:
            cost_usd = None
        final_strikes = self.completed[-1].strikes if self.completed else 0
        return RunTotals(
            iterations=len(self.completed),
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=cost_usd,
            commits=commits,
            auto_closures=auto_closures,
            final_strikes=final_strikes,
        )

    # -- frozen UI artefacts -----------------------------------------------

    def build_iteration_panel(self, snap: IterationSnapshot) -> Panel:
        """Construct the per-iteration frozen Panel.

        Every counter listed in the PRD is rendered. Cost is the only
        field that can render as ``—`` (em dash) — when the model is not
        in the pricing table. Context utilisation is highlighted when it
        crosses the documented half-window threshold.
        """
        body = Text()

        # Header: iter# + issue#
        body.append("Iteration ", style=STYLES["meta"])
        body.append(str(snap.iter_num), style=STYLES["panel_title"])
        if snap.issue_num is not None:
            body.append(f"  •  Issue #{snap.issue_num}", style=STYLES["meta"])
        body.append("\n")

        # Duration + model line
        body.append("Duration: ", style=STYLES["meta"])
        body.append(f"{snap.duration_seconds:.2f}s")
        body.append("    Model: ", style=STYLES["meta"])
        body.append(snap.model if snap.model is not None else "—")
        body.append("\n")

        # Tokens line + context utilisation
        body.append("Tokens: ", style=STYLES["meta"])
        body.append(f"in={snap.tokens_in:,}  out={snap.tokens_out:,}")
        body.append("    Context: ", style=STYLES["meta"])
        ctx_text = self._format_context_line(snap)
        body.append_text(ctx_text)
        body.append("  (observed tokens)", style=STYLES["meta"])
        body.append("\n")

        # Cost line
        body.append("Est cost: ", style=STYLES["meta"])
        cost_text = self._format_cost_line(snap)
        body.append_text(cost_text)
        body.append("\n")

        # Tool / skill counts
        body.append("Tools: ", style=STYLES["meta"])
        body.append(str(snap.tool_count))
        body.append("    Skills: ", style=STYLES["meta"])
        body.append(str(snap.skill_count))
        body.append("\n")

        # Commits / auto-closures / strikes
        body.append("Commits: ", style=STYLES["meta"])
        body.append(str(snap.commits))
        body.append("    Auto-closures: ", style=STYLES["meta"])
        body.append(str(snap.auto_closures))
        body.append("    Strikes: ", style=STYLES["meta"])
        body.append(str(snap.strikes))

        return Panel(
            body,
            title=f"[bold]Iteration {snap.iter_num} done[/bold]",
            border_style=STYLES["panel_rule"],
            box=ROUNDED,
            padding=(0, 1),
        )

    def build_run_table(self) -> Table:
        """Construct the frozen run-end Table.

        One row per completed iteration, plus a totals footer that
        surfaces summed tokens / cost / commits / auto-closures and the
        ``final_strikes`` value from the last iteration.
        """
        table = Table(
            title="[bold]Run summary[/bold]",
            box=SIMPLE,
            header_style=STYLES["table_header"],
            show_footer=len(self.completed) > 0,
        )
        table.add_column("Iter", justify="right", footer="totals")
        table.add_column("Issue", justify="right", footer="")
        table.add_column("Model", justify="left", footer="")
        table.add_column("Duration", justify="right", footer="")
        totals = self.totals()
        table.add_column(
            "Tokens in",
            justify="right",
            footer=f"{totals.tokens_in:,}",
        )
        table.add_column(
            "Tokens out",
            justify="right",
            footer=f"{totals.tokens_out:,}",
        )
        table.add_column(
            "Cost USD",
            justify="right",
            footer=_format_decimal_footer(totals.cost_usd),
        )
        table.add_column(
            "Commits",
            justify="right",
            footer=str(totals.commits),
        )
        table.add_column(
            "Closures",
            justify="right",
            footer=str(totals.auto_closures),
        )
        table.add_column(
            "Final strikes",
            justify="right",
            footer=str(totals.final_strikes),
        )

        for snap in self.completed:
            cost = snap.cost_usd(self.pricing)
            cost_str = f"${cost:.4f}" if cost is not None else "—"
            issue_str = f"#{snap.issue_num}" if snap.issue_num is not None else "—"
            model_str = snap.model if snap.model is not None else "—"
            table.add_row(
                str(snap.iter_num),
                issue_str,
                model_str,
                f"{snap.duration_seconds:.1f}s",
                f"{snap.tokens_in:,}",
                f"{snap.tokens_out:,}",
                cost_str,
                str(snap.commits),
                str(snap.auto_closures),
                str(snap.strikes),
            )
        return table

    def build_rollup_band(self) -> Text:
        """Compose the compact **Summary** rollup band for the Dashboard (ADR-0003).

        A single-line, *live* (not frozen) run-level totals strip — the band of
        the Dashboard stacked under the Queue. It mirrors the run-end
        :meth:`build_run_table` footer: summed tokens, estimated cost, commits,
        closures, and the final strike count, plus the iteration count for
        context. Returned as a Rich :class:`~rich.text.Text` the interactive app
        drops into a ``Static``; the full per-iteration table stays the run-end
        artefact. Cost renders as the em dash when no completed iteration had a
        priced model (the same unknown-model treatment as the table footer).
        """
        totals = self.totals()
        text = Text()
        text.append("Summary", style=STYLES["meta"])
        text.append(f"  •  iters {totals.iterations}")
        text.append(f"  •  tokens in={totals.tokens_in:,} out={totals.tokens_out:,}")
        text.append(f"  •  cost {_format_decimal_footer(totals.cost_usd)}")
        text.append(f"  •  commits {totals.commits}")
        text.append(f"  •  closures {totals.auto_closures}")
        text.append(f"  •  strikes {totals.final_strikes}")
        return text

    # -- internal -----------------------------------------------------------

    def _format_context_line(self, snap: IterationSnapshot) -> Text:
        """Render ``used / window  (XX%)`` with high-watermark highlighting."""
        text = Text()
        used = snap.context_used
        if snap.model is None:
            text.append(f"{used:,}")
            return text
        util = context_utilisation(snap.model, used, self.pricing)
        if util is None:
            text.append(f"{used:,}")
            return text
        u, window, fraction = util
        pct = int(round(fraction * 100))
        line = f"{u:,} / {window:,}  ({pct}%)"
        if fraction >= _CONTEXT_HIGH_WATERMARK:
            text.append(line, style=STYLES["warning"])
        else:
            text.append(line)
        return text

    def _format_cost_line(self, snap: IterationSnapshot) -> Text:
        """Render the cost line with date label or em dash."""
        text = Text()
        cost = snap.cost_usd(self.pricing)
        if cost is None:
            text.append("—  ", style=STYLES["meta"])
            text.append("(model not in pricing table)", style=STYLES["meta"])
            return text
        text.append(f"${cost:.4f} USD")
        if self.pricing_date is not None:
            text.append(
                f"  (provider list, as of {self.pricing_date})",
                style=STYLES["meta"],
            )
        return text


# ---------------------------------------------------------------------------
# Module-private helpers
# ---------------------------------------------------------------------------


def _format_decimal_footer(cost: Optional[Decimal]) -> str:
    """Footer-cell formatter for cost totals; em dash for None."""
    if cost is None:
        return "—"
    return f"${cost:.4f}"
