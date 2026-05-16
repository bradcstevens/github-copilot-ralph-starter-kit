"""``ralph_afk.wrapper`` — cross-runner contract logic, deep and pure.

This module is the single source of truth for the wrapper-level behaviour
shared between the bash runner (``ralph/afk.sh``) and the Python runner
(this package). Its load-bearing surface is intentionally small:

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
  this package, no SDK. The cross-runner contract must remain unit-testable
  in isolation.
* **Line-by-line matching for parity.** Python's ``\\s+`` would otherwise
  match across newlines, but the bash runner pipes through ``grep`` which
  reads line-by-line. :func:`extract_close_refs` splits on ``\\n`` so the
  matching behaviour matches bash exactly, while the compiled regex stays
  byte-for-byte the PRD-specified pattern.
* **Drift is caught by ``tests/test_close_keyword_parity.py``.** That test
  runs both the bash ``grep`` pipeline and :func:`extract_close_refs` against
  a shared corpus. If it ever fails, the failure IS the spec.
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
]

# Byte-for-byte the PRD-specified pattern. Mirrors the BRE used at
# ``ralph/afk.sh:193`` (``[[:space:]]`` ≈ ``\s``, ``[0-9]`` ≈ ``\d``).
# Drift here is detected by the parity test.
CLOSE_KEYWORD_RE: re.Pattern[str] = re.compile(
    r"(?P<kw>close[sd]?|fix(?:es|ed)?|resolve[sd]?)\s+#(?P<num>\d+)",
    re.IGNORECASE,
)


def extract_close_refs(commit_messages: str) -> list[int]:
    """Extract deduplicated issue numbers referenced via GitHub closing
    keywords (``close[sd]?`` / ``fix(es|ed)?`` / ``resolve[sd]?``).

    Returns numbers in first-encounter order — the bash pipeline ends in
    ``sort -un`` (sorted-unique) but the Python side preserves order so
    callers can reason about which commit referenced which issue first.

    Matching is performed line-by-line to preserve cross-runner parity
    with ``grep`` (see module docstring). Lines are split on ``\\n`` only —
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

    Mirrors the bash truth function at ``ralph/afk.sh:403-406``: an
    iteration "made progress" if either at least one commit landed OR the
    wrapper auto-closed at least one issue.

    Args:
        commits_in_iter: Number of new commits the iteration produced.
        auto_closures_in_iter: Number of issues the wrapper auto-closed.

    Returns:
        ``True`` if either count is non-zero.
    """
    return commits_in_iter > 0 or auto_closures_in_iter > 0


# Outcome alphabet — kept narrow on purpose. The loop only needs to know
# whether to keep iterating ("running") or abort ("aborted"). The
# distinction between "saw NMT" and "silently no-progress" is renderer
# concern, not state-machine concern.
Outcome = Literal["running", "aborted"]


@dataclass
class NMTStrikeStateMachine:
    """Tracks consecutive no-progress iterations against a configurable cap.

    The state machine mirrors the bash strikes logic at
    ``ralph/afk.sh:297-298`` and ``409-429``:

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
                line to print (matching ``ralph/afk.sh:411-412`` vs
                ``416-417``). Accepted as a keyword arg so future
                consumers can be wired in via :func:`asdict`-style
                passing without changing call sites.

        Returns:
            The new outcome (``"running"`` or ``"aborted"``).
        """
        # saw_nmt_sentinel is informational only; bash logic at lines
        # 411-412 / 416-417 only varies the warning message, not the
        # state-machine outcome.
        _ = saw_nmt_sentinel

        # Terminal state. The bash runner exits immediately on abort
        # (``ralph/afk.sh:427``); the Python state machine mirrors that by
        # freezing — further ticks neither reset strikes nor flip outcome.
        if self.outcome == "aborted":
            return self.outcome

        if did_iteration_make_progress(commits_in_iter, auto_closures_in_iter):
            self.strikes = 0
            return self.outcome

        self.strikes += 1
        if self.strikes >= self.max_strikes:
            self.outcome = "aborted"
        return self.outcome
