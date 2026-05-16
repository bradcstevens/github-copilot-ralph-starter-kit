"""``ralph_afk.persist`` — filesystem side of observability.

This module owns the three artefacts the runner writes per
``ralph-afk`` invocation:

==================  ==========================================  =================================
Artefact            Path                                        Purpose
==================  ==========================================  =================================
Event log           ``.ralph/logs/<iso>-<run_id>.jsonl``        Replay-grade JSONL, one event
                                                                per line, append-only.
Run summary         ``.ralph/runs/<iso>-<run_id>.json``         Per-iteration counter rollup,
                                                                emitted on close.
Process diag.       stderr + ``.ralph/logs/<iso>-<run_id>.log``  Human-readable diagnostics;
                                                                stderr stream is primary, the
                                                                ``.log`` file is the mirror.
==================  ==========================================  =================================

All artefacts live under the **repo root** (resolved by callers via
:func:`ralph_afk.git.repo_root`). The directories are created lazily on
first write so a process that exits before producing any output leaves
no on-disk footprint.

Public surface:

* :class:`EventLogWriter` — append-only JSONL writer, context manager.
  Every event is routed through :func:`ralph_afk.events.scrub` and
  :func:`ralph_afk.events.to_jsonl_line` before being flushed.
* :class:`RunSummaryWriter` — accumulates :class:`IterationCounters`
  rows and writes the run-summary JSON on close. Context manager.
* :class:`IterationCounters` — per-iteration counter dataclass.
* :class:`WritersBundle` — frozen tuple-of-writers returned by
  :func:`create_writers`.
* :func:`create_writers` — the canonical factory. Constructs an
  :class:`EventLogWriter`, a :class:`RunSummaryWriter`, and a
  diagnostics :class:`logging.Logger` all bound to the same ``run_id``
  and ``started_at`` so the three artefact filenames share a stem. Also
  appends ``.ralph/`` to ``.gitignore`` if the file exists and the entry
  is not already present (idempotent).
* :func:`make_run_id` — stdlib-only 26-char Crockford-base32 ULID.
* :func:`ensure_gitignore_entry` — exposed for tests; called by
  :func:`create_writers`.
* :data:`GITIGNORE_ENTRY` — the literal ``.ralph/`` line.

Run-summary JSON schema
-----------------------

The :class:`RunSummaryWriter` emits a single JSON document on close::

    {
      "run_id": "<26-char Crockford-base32 ULID>",
      "started_at": "2026-05-16T03:14:15.123Z",          # RFC3339, ms precision, trailing Z
      "iterations": [
        {
          "iter": 1,                                       # 1-based iteration index
          "duration_seconds": 27.84,                       # float, seconds
          "model": "claude-opus-4.7-xhigh",                # str | null
          "tokens_in": 12345,                              # int (per-iteration sum)
          "tokens_out": 678,                               # int (per-iteration sum)
          "context_used": 13023,                           # int, cumulative context tokens
          "est_cost_usd": "0.0234",                        # str (Decimal-as-string) | null
          "tool_count": 6,                                 # int
          "skill_count": 1,                                # int
          "commits": 1,                                    # int (commits made during iter)
          "auto_closures": 1,                              # int (wrapper auto-closes)
          "strikes": 0                                     # int (NMT strike count AFTER iter)
        }
      ]
    }

``est_cost_usd`` is :data:`None` (JSON ``null``) when the iteration's
model has no pricing entry — *never zero*, so downstream consumers can
distinguish "unknown" from "free".

Design notes:

* **Append-only, flush-after-every-write.** The JSONL file is opened
  in ``"a"`` mode and :meth:`io.TextIOBase.flush` is called after every
  :meth:`EventLogWriter.write` so a crashed process leaves a
  partial-but-parseable file. Verified by a subprocess-driven test.
* **Lazy I/O.** Neither :class:`EventLogWriter` nor :class:`RunSummaryWriter`
  touches the filesystem until the first :meth:`~EventLogWriter.write`
  / :meth:`~RunSummaryWriter.record` call. Construction is side-effect
  free aside from :func:`create_writers`'s ``.gitignore`` touch.
* **Idempotent ``.gitignore`` touch.** :func:`ensure_gitignore_entry`
  matches the existing line by ``line.strip() in {".ralph/", ".ralph"}``
  — exact-match only (not case-insensitive, not commented-out lines).
  Running the factory twice does not duplicate the entry.
* **Logger-handler hygiene.** :func:`create_writers` removes any
  pre-existing handlers on the named logger before attaching its own,
  so reusing a ``run_id`` in tests does not leak handlers across calls.
* **Stdlib-only.** Enforced via AST inspection in
  ``tests/test_persist.py::test_persist_module_imports_are_constrained``.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import sys
import time
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import TracebackType
from typing import Any, Callable, TextIO

from ralph_afk.events import scrub, to_jsonl_line

__all__ = [
    "EventLogWriter",
    "RunSummaryWriter",
    "IterationCounters",
    "WritersBundle",
    "create_writers",
    "make_run_id",
    "ensure_gitignore_entry",
    "GITIGNORE_ENTRY",
]

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

GITIGNORE_ENTRY: str = ".ralph/"

# Crockford's Base32 alphabet — excludes I, L, O, U (visually ambiguous /
# slang-collision risk). 32 symbols, 5 bits per symbol.
_CROCKFORD_ALPHABET: str = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

# Validation regex for explicit ``run_id`` arguments. 26 chars, each from
# the Crockford alphabet. Matches the output of :func:`make_run_id`.
_RUN_ID_RE: re.Pattern[str] = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")

# Filename timestamp format. Colons replaced with dashes for case-insensitive
# filesystem safety (e.g. NTFS, HFS+). Trailing Z indicates UTC.
_FILENAME_TS_FORMAT: str = "%Y-%m-%dT%H-%M-%SZ"


# ---------------------------------------------------------------------------
# ULID generation (stdlib-only)
# ---------------------------------------------------------------------------


def _crockford_b32(value: int, width: int) -> str:
    """Encode ``value`` (non-negative int) as a fixed-width Crockford-base32 string.

    Raises:
        ValueError: If ``value`` does not fit in ``width`` symbols
            (i.e. ``value >= 32**width``), or if ``value`` is negative.
    """
    if value < 0:
        raise ValueError(f"value must be non-negative, got {value}")
    out: list[str] = []
    for _ in range(width):
        out.append(_CROCKFORD_ALPHABET[value & 0x1F])
        value >>= 5
    if value != 0:
        raise ValueError(
            f"value too large to encode in {width} Crockford-base32 symbols"
        )
    return "".join(reversed(out))


def make_run_id(
    *,
    time_ms: int | None = None,
    rand_bytes_fn: Callable[[int], bytes] = secrets.token_bytes,
) -> str:
    """Generate a 26-char Crockford-base32 ULID.

    First 10 characters encode a 48-bit milliseconds-since-Unix-epoch
    timestamp; remaining 16 characters encode 80 bits of randomness.
    ULIDs sort lexicographically by their time prefix, which is useful
    for ``ls .ralph/logs/`` and grep-based log inspection.

    Args:
        time_ms: 48-bit millisecond timestamp; defaults to the current
            wall-clock. Tests inject explicit values.
        rand_bytes_fn: callable returning ``n`` random bytes; defaults
            to :func:`secrets.token_bytes`. Tests inject a deterministic
            fake for reproducible ULIDs.

    Returns:
        26-character ULID string.
    """
    if time_ms is None:
        time_ms = int(time.time() * 1000)
    rand_int = int.from_bytes(rand_bytes_fn(10), "big")
    return _crockford_b32(time_ms, 10) + _crockford_b32(rand_int, 16)


# ---------------------------------------------------------------------------
# .gitignore touch
# ---------------------------------------------------------------------------


def ensure_gitignore_entry(repo_root: Path) -> None:
    """Append ``.ralph/`` to ``repo_root/.gitignore`` if the entry is missing.

    No-op when:

    * ``.gitignore`` does not exist at ``repo_root`` — downstream projects
      may have their own conventions; we don't create the file.
    * ``.gitignore`` already contains a line whose ``.strip()`` value is
      ``".ralph/"`` or ``".ralph"`` — both forms are conventional and
      either one already covers our artefacts.

    Otherwise, appends a single ``.ralph/`` line. Adds a leading newline
    if the existing file does not end in one, so the appended line lands
    on its own line.

    Args:
        repo_root: Repository root :class:`Path` (typically from
            :func:`ralph_afk.git.repo_root`).
    """
    gitignore = repo_root / ".gitignore"
    if not gitignore.exists():
        return
    existing = gitignore.read_text(encoding="utf-8")
    for line in existing.splitlines():
        if line.strip() in {".ralph/", ".ralph"}:
            return
    leader = "" if existing.endswith("\n") else "\n"
    gitignore.write_text(existing + leader + GITIGNORE_ENTRY + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# IterationCounters
# ---------------------------------------------------------------------------


@dataclass
class IterationCounters:
    """Per-iteration counter rollup recorded by the loop.

    Fields match the run-summary JSON schema documented in the module
    docstring. ``est_cost_usd`` is :class:`decimal.Decimal` (or :data:`None`
    for unknown models); :meth:`RunSummaryWriter.flush` serialises it to
    string for JSON round-trip-safety.
    """

    iter: int
    duration_seconds: float = 0.0
    model: str | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    context_used: int = 0
    est_cost_usd: Decimal | None = None
    tool_count: int = 0
    skill_count: int = 0
    commits: int = 0
    auto_closures: int = 0
    strikes: int = 0


# ---------------------------------------------------------------------------
# EventLogWriter
# ---------------------------------------------------------------------------


class EventLogWriter(AbstractContextManager["EventLogWriter"]):
    """Append-only JSONL writer for replay-grade event logs.

    Lazy-creates the parent directory on first :meth:`write`. Each event
    is routed through :func:`ralph_afk.events.scrub` and
    :func:`ralph_afk.events.to_jsonl_line` (which scrubs again — the
    scrubber is idempotent) before being appended to the file. The file
    is opened in ``"a"`` (append) mode and flushed after every write so
    a crashed process leaves a partial-but-parseable JSONL trail.

    Use as a context manager::

        with EventLogWriter(path) as log:
            log.write({"type": "wrapper.run.start", ...})
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._fh: TextIO | None = None

    @property
    def path(self) -> Path:
        """Resolved JSONL file path. Stable across the writer's lifetime."""
        return self._path

    def __enter__(self) -> "EventLogWriter":
        return self

    def write(self, event: dict[str, Any]) -> None:
        """Append a scrubbed JSONL line for ``event``. Flushes after write.

        On first call, lazily creates the parent directory and opens the
        file in append mode.

        Args:
            event: Event dict (typically from :func:`ralph_afk.events.make_event`).
        """
        if self._fh is None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = self._path.open("a", encoding="utf-8")
        # Issue #7 acceptance criterion #3: "passes through events.scrub
        # AND events.to_jsonl_line". to_jsonl_line itself runs scrub again
        # — that's fine, scrub is idempotent (events.py guarantees this).
        scrubbed = scrub(event)
        line = to_jsonl_line(scrubbed)
        self._fh.write(line)
        self._fh.flush()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None


# ---------------------------------------------------------------------------
# RunSummaryWriter
# ---------------------------------------------------------------------------


class RunSummaryWriter(AbstractContextManager["RunSummaryWriter"]):
    """Accumulator for per-iteration counters; writes JSON on close.

    Constructed via :func:`create_writers` so the ``run_id`` and
    ``started_at`` align with the matching :class:`EventLogWriter`.
    Records are buffered in memory until :meth:`flush` (or context-manager
    exit) materialises the JSON file.

    The output schema is documented in the module docstring.
    """

    def __init__(
        self, path: Path, *, run_id: str, started_at: datetime
    ) -> None:
        self._path = path
        self._run_id = run_id
        self._started_at = started_at
        self._iterations: list[IterationCounters] = []

    @property
    def path(self) -> Path:
        """Resolved JSON file path. Stable across the writer's lifetime."""
        return self._path

    def record(self, counters: IterationCounters) -> None:
        """Append a per-iteration counter row. In-memory only until :meth:`flush`."""
        self._iterations.append(counters)

    def __enter__(self) -> "RunSummaryWriter":
        return self

    def flush(self) -> None:
        """Materialise the accumulated counters to a single JSON file."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload: dict[str, Any] = {
            "run_id": self._run_id,
            "started_at": _format_rfc3339_ms(self._started_at),
            "iterations": [
                {
                    "iter": c.iter,
                    "duration_seconds": c.duration_seconds,
                    "model": c.model,
                    "tokens_in": c.tokens_in,
                    "tokens_out": c.tokens_out,
                    "context_used": c.context_used,
                    "est_cost_usd": (
                        str(c.est_cost_usd) if c.est_cost_usd is not None else None
                    ),
                    "tool_count": c.tool_count,
                    "skill_count": c.skill_count,
                    "commits": c.commits,
                    "auto_closures": c.auto_closures,
                    "strikes": c.strikes,
                }
                for c in self._iterations
            ],
        }
        text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
        self._path.write_text(text, encoding="utf-8")

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self.flush()


# ---------------------------------------------------------------------------
# Diagnostic FileHandler with lazy parent-dir creation
# ---------------------------------------------------------------------------


class _LazyMkdirFileHandler(logging.FileHandler):
    """A :class:`logging.FileHandler` that ``mkdir(parents=True, exist_ok=True)``\\s
    on its parent directory on first emit.

    Combined with ``delay=True`` (passed by :func:`create_writers`), this
    keeps the diagnostic ``.log`` file fully lazy — no directory is
    created and no file is opened until the first log record is emitted.
    """

    def emit(self, record: logging.LogRecord) -> None:
        Path(self.baseFilename).parent.mkdir(parents=True, exist_ok=True)
        super().emit(record)


# ---------------------------------------------------------------------------
# WritersBundle + create_writers factory
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WritersBundle:
    """Aligned bundle returned by :func:`create_writers`.

    All three writers share the same ``run_id`` + ``started_at`` so
    their on-disk artefact filenames share a stem.
    """

    run_id: str
    started_at: datetime
    event_log: EventLogWriter
    run_summary: RunSummaryWriter
    diagnostics: logging.Logger
    diagnostics_path: Path


def create_writers(
    repo_root: Path,
    *,
    run_id: str | None = None,
    started_at: datetime | None = None,
) -> WritersBundle:
    """Construct an aligned :class:`WritersBundle` for one ``ralph-afk`` invocation.

    Side effects:

    * Calls :func:`ensure_gitignore_entry` against ``repo_root`` — adds
      ``.ralph/`` to ``.gitignore`` if the file exists and the entry is
      not already present. Idempotent.
    * Configures a named :class:`logging.Logger` for diagnostics
      (replaces any pre-existing handlers on that name) with one
      :class:`logging.StreamHandler` to ``sys.stderr`` and one
      :class:`_LazyMkdirFileHandler` mirroring to the ``.log`` file.

    Does **not** create any directories or files under ``.ralph/`` —
    that I/O is deferred to each writer's first write / first emit.

    Args:
        repo_root: Repository root :class:`Path`.
        run_id: Optional explicit run_id (must be 26-char Crockford
            base32). When :data:`None`, a fresh ULID is generated.
        started_at: Optional explicit wall-clock; defaults to
            :func:`datetime.now` in UTC.

    Returns:
        A :class:`WritersBundle` carrying the three writers + metadata.

    Raises:
        ValueError: If ``run_id`` is provided and does not match the
            26-char Crockford-base32 ULID shape.
    """
    if started_at is None:
        started_at = datetime.now(timezone.utc)
    elif started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=timezone.utc)
    else:
        started_at = started_at.astimezone(timezone.utc)

    if run_id is None:
        run_id = make_run_id(time_ms=int(started_at.timestamp() * 1000))
    else:
        if not _RUN_ID_RE.fullmatch(run_id):
            raise ValueError(
                f"run_id must be a 26-char Crockford-base32 ULID, got {run_id!r}"
            )

    ensure_gitignore_entry(repo_root)

    stem = f"{_format_filename_ts(started_at)}-{run_id}"
    logs_dir = repo_root / ".ralph" / "logs"
    runs_dir = repo_root / ".ralph" / "runs"

    event_log = EventLogWriter(logs_dir / f"{stem}.jsonl")
    run_summary = RunSummaryWriter(
        runs_dir / f"{stem}.json", run_id=run_id, started_at=started_at
    )
    diagnostics_path = logs_dir / f"{stem}.log"
    logger = _build_diagnostics_logger(run_id, diagnostics_path)

    return WritersBundle(
        run_id=run_id,
        started_at=started_at,
        event_log=event_log,
        run_summary=run_summary,
        diagnostics=logger,
        diagnostics_path=diagnostics_path,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _format_filename_ts(dt: datetime) -> str:
    """Format ``dt`` for a filesystem-safe filename: ``YYYY-MM-DDTHH-MM-SSZ``."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.strftime(_FILENAME_TS_FORMAT)


def _format_rfc3339_ms(dt: datetime) -> str:
    """Format ``dt`` as RFC3339 with millisecond precision and trailing ``Z``.

    Matches the JSONL envelope's ``ts`` format from :mod:`ralph_afk.events`
    so timestamps in the run summary are directly comparable.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    millis = dt.microsecond // 1000
    return f"{dt.strftime('%Y-%m-%dT%H:%M:%S')}.{millis:03d}Z"


def _build_diagnostics_logger(run_id: str, log_path: Path) -> logging.Logger:
    """Construct a per-run diagnostics logger with stderr + lazy-file handlers.

    Removes any pre-existing handlers on the named logger before adding
    fresh ones so reusing a ``run_id`` (e.g. in tests) does not leak
    handlers across factory calls — without this guard, the second call
    would emit each record twice and the file handler would point at the
    first-call's path.
    """
    logger = logging.getLogger(f"ralph_afk.diagnostics.{run_id}")
    logger.setLevel(logging.INFO)
    logger.propagate = False  # don't bubble to root (avoids double-printing).

    for old in list(logger.handlers):
        old.close()
        logger.removeHandler(old)

    fmt = logging.Formatter(
        fmt="%(asctime)s.%(msecs)03dZ %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setFormatter(fmt)
    logger.addHandler(stream_handler)

    file_handler = _LazyMkdirFileHandler(
        str(log_path), mode="a", encoding="utf-8", delay=True
    )
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    return logger
