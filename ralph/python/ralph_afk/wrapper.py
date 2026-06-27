"""``ralph_afk.wrapper`` — wrapper contract logic, deep and pure.

This module is the single source of truth for the wrapper-level behaviour
of the AFK runner. Its load-bearing surface is intentionally small:

* :data:`CLOSE_KEYWORD_RE` — the GitHub closing-keyword regex.
* :func:`extract_close_refs` — pulls deduplicated issue numbers out of a
  blob of commit messages, in first-encounter order.
* :func:`filter_to_pool` — restricts a list of refs to a given AFK-ready
  pool, preserving order.
* :func:`did_iteration_make_progress` — the truth function for whether an
  iteration counts as work.
* :class:`NMTStrikeStateMachine` — the no-more-tasks strike state machine
  that decides when to abort a stuck run.

Design notes:

* **stdlib + ``re`` only.** No third-party imports, no peer modules from
  this package, no SDK. The contract must remain unit-testable
  in isolation.
* **Line-by-line matching.** Python's ``\\s+`` would otherwise
  match across newlines, so :func:`extract_close_refs` splits on ``\\n``
  and matches each line independently — equivalent to the line-oriented
  ``grep`` semantics the close-keyword convention is specified against,
  while the compiled regex stays byte-for-byte the PRD-specified pattern.
* **Behaviour is pinned by ``tests/test_wrapper.py``**, which exercises
  :func:`extract_close_refs` against the close-keyword corpus — every
  keyword form, case-insensitivity, the tab / multi-space separators,
  first-encounter dedup, and the negatives the convention must reject.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

__all__ = [
    "CLOSE_KEYWORD_RE",
    "extract_close_refs",
    "filter_to_pool",
    "did_iteration_make_progress",
    "NMTStrikeStateMachine",
    "CHECKPOINT_TRAILER_KEY",
    "checkpoint_message",
    "is_checkpoint_message",
]

# Byte-for-byte the PRD-specified close-keyword pattern. The convention is
# specified against POSIX ``grep`` semantics (``[[:space:]]`` ≈ ``\s``,
# ``[0-9]`` ≈ ``\d``); drift is detected by ``tests/test_wrapper.py``.
CLOSE_KEYWORD_RE: re.Pattern[str] = re.compile(
    r"(?P<kw>close[sd]?|fix(?:es|ed)?|resolve[sd]?)\s+#(?P<num>\d+)",
    re.IGNORECASE,
)


def extract_close_refs(commit_messages: str) -> list[int]:
    """Extract deduplicated issue numbers referenced via GitHub closing
    keywords (``close[sd]?`` / ``fix(es|ed)?`` / ``resolve[sd]?``).

    Returns numbers in first-encounter order — the POSIX grep/sort oracle
    produces sorted-unique output, but the Python side preserves order so
    callers can reason about which commit referenced which issue first.

    Matching is performed line-by-line to preserve POSIX ``grep`` semantics
    (see module docstring). Lines are split on ``\n`` only —
    not via :py:meth:`str.splitlines`, which would also split on ``\\r``,
    ``\\v``, ``\\f`` and Unicode line separators that ``grep`` treats as
    in-line content.

    Args:
        commit_messages: One or more commit messages concatenated together,
            optionally separated by the wrapper's ``---COMMIT-BOUNDARY---``
            marker. Empty string is allowed and returns ``[]``.

    Returns:
        Deduplicated issue numbers in first-encounter order.
    """
    seen: set[int] = set()
    out: list[int] = []
    for line in commit_messages.split("\n"):
        for match in CLOSE_KEYWORD_RE.finditer(line):
            num = int(match.group("num"))
            if num in seen:
                continue
            seen.add(num)
            out.append(num)
    return out


def filter_to_pool(refs: list[int], afk_pool: set[int]) -> list[int]:
    """Restrict ``refs`` to numbers in the iteration's AFK-ready pool.

    Preserves the input order. Does not dedup — :func:`extract_close_refs`
    is the dedup seam, and any dedup here would risk hiding a caller bug
    that fed in a non-deduped list.

    Args:
        refs: A list of issue numbers, typically the output of
            :func:`extract_close_refs`.
        afk_pool: The set of issue numbers the wrapper is allowed to act
            on this iteration (the AFK-ready pool whitelist).

    Returns:
        ``refs`` filtered down to members of ``afk_pool``, in input order.
    """
    return [n for n in refs if n in afk_pool]


def did_iteration_make_progress(
    commits_in_iter: int, auto_closures_in_iter: int
) -> bool:
    """Decide whether an iteration counts as work.

    An iteration "made progress" if either at least one commit landed OR
    the wrapper auto-closed at least one issue.

    Args:
        commits_in_iter: Number of new commits the iteration produced.
        auto_closures_in_iter: Number of issues the wrapper auto-closed.

    Returns:
        ``True`` if either count is non-zero.
    """
    return commits_in_iter > 0 or auto_closures_in_iter > 0


# --------------------------------------------------------------------------- #
# Runner Checkpoint message contract (issue #32 — ADR-0004)                   #
# --------------------------------------------------------------------------- #

#: Commit-trailer key that tags a runner-authored **Checkpoint**. The runner
#: writes ``Ralph-Checkpoint: <ref>`` so a Checkpoint is distinguishable from an
#: agent commit in ``git log`` and so :func:`is_checkpoint_message` can detect
#: one without re-deriving the convention. The value is the active issue ref
#: (or ``unattributed``) — deliberately NOT ``#N``, so a Checkpoint never opens
#: a GitHub cross-reference on the issue every iteration.
CHECKPOINT_TRAILER_KEY = "Ralph-Checkpoint"

#: Attribution value when the active issue could not be inferred.
_CHECKPOINT_UNATTRIBUTED = "unattributed"

_CHECKPOINT_BODY = (
    "Runner-authored Checkpoint (ADR-0004): staged the worktree the agent left\n"
    "uncommitted so the next iteration starts on a clean tree and the work can\n"
    "reach the remote. Not an agent commit; excluded from Strike progress."
)


def checkpoint_message(active_ref: int | str | None) -> str:
    """Build the commit message for a runner **Checkpoint** (ADR-0004).

    The message is guaranteed **close-keyword-free** — it never matches
    :data:`CLOSE_KEYWORD_RE`, so neither the wrapper's auto-close backstop nor
    GitHub's native close-on-push can fire on a Checkpoint — and it carries the
    :data:`CHECKPOINT_TRAILER_KEY` trailer attributing it to the active issue.

    Args:
        active_ref: The active issue the Checkpoint is attributed to — an int
            issue number, a str ref (PRDs path / PR), or ``None`` when the
            runner could not infer it.

    Returns:
        A ``subject\\n\\nbody\\n\\ntrailer`` commit message.
    """
    if active_ref is None:
        subject = "Checkpoint: capture uncommitted work-in-progress"
        attribution = _CHECKPOINT_UNATTRIBUTED
    elif isinstance(active_ref, int):
        subject = f"Checkpoint: capture work-in-progress for issue {active_ref}"
        attribution = str(active_ref)
    else:
        subject = f"Checkpoint: capture work-in-progress for {active_ref}"
        attribution = str(active_ref)
    trailer = f"{CHECKPOINT_TRAILER_KEY}: {attribution}"
    return f"{subject}\n\n{_CHECKPOINT_BODY}\n\n{trailer}"


def is_checkpoint_message(message: str) -> bool:
    """Return ``True`` if ``message`` carries the Checkpoint trailer.

    Tolerant of surrounding whitespace and case so a Checkpoint authored by
    :func:`checkpoint_message` round-trips, while an ordinary agent commit
    (even one that merely mentions a checkpoint in prose) does not.
    """
    prefix = f"{CHECKPOINT_TRAILER_KEY.lower()}:"
    return any(
        line.strip().lower().startswith(prefix) for line in message.split("\n")
    )


# Outcome alphabet — kept narrow on purpose. The loop only needs to know
# whether to keep iterating ("running") or abort ("aborted"). The
# distinction between "saw NMT" and "silently no-progress" is renderer
# concern, not state-machine concern.
Outcome = Literal["running", "aborted"]


@dataclass
class NMTStrikeStateMachine:
    """Tracks consecutive no-progress iterations against a configurable cap.

    The state machine implements the no-progress strikes logic:

    * Start in ``running`` with zero strikes.
    * Each call to :meth:`tick` represents one completed iteration.
    * If the iteration made progress, strikes reset to zero and the
      ``<promise>NO MORE TASKS</promise>`` sentinel — if observed — is
      ignored (informational only).
    * Otherwise strikes increment. On reaching ``max_strikes`` the outcome
      flips to ``aborted`` and stays there; further ticks are no-ops on
      the outcome.

    Attributes:
        max_strikes: Maximum consecutive no-progress iterations tolerated
            before aborting. Must be ≥ 1. Mirrors ``MAX_NMT_STRIKES``
            (default 3).
        strikes: Current strike count.
        outcome: Either ``"running"`` or ``"aborted"``.
    """

    max_strikes: int = 3
    strikes: int = 0
    outcome: Outcome = field(default="running")

    def __post_init__(self) -> None:
        if self.max_strikes < 1:
            raise ValueError(
                f"max_strikes must be ≥ 1 (got {self.max_strikes!r}); "
                "the loop would abort on the very first no-progress "
                "iteration otherwise."
            )

    def tick(
        self,
        *,
        commits_in_iter: int,
        auto_closures_in_iter: int,
        saw_nmt_sentinel: bool = False,
    ) -> Outcome:
        """Record one completed iteration and return the resulting outcome.

        Args:
            commits_in_iter: Number of commits the iteration produced.
            auto_closures_in_iter: Number of wrapper-issued auto-closes.
            saw_nmt_sentinel: ``True`` if the agent emitted the
                ``<promise>NO MORE TASKS</promise>`` sentinel this
                iteration. Informational only — the state machine never
                consults it. The renderer uses it to pick which warning
                line to print for progress vs no-progress. Accepted as a
                keyword arg so future
                consumers can be wired in via :func:`asdict`-style
                passing without changing call sites.

        Returns:
            The new outcome (``"running"`` or ``"aborted"``).
        """
        # saw_nmt_sentinel is informational only; it only varies the
        # warning message, not the state-machine outcome.
        _ = saw_nmt_sentinel

        # Terminal state. On abort the state machine freezes — further
        # ticks neither reset strikes nor flip the outcome.
        if self.outcome == "aborted":
            return self.outcome

        if did_iteration_make_progress(commits_in_iter, auto_closures_in_iter):
            self.strikes = 0
            return self.outcome

        self.strikes += 1
        if self.strikes >= self.max_strikes:
            self.outcome = "aborted"
        return self.outcome
