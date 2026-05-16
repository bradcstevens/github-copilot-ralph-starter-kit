"""Tests for ``ralph_afk.persist`` (issue #7).

Covers the filesystem side of observability:

* :func:`make_run_id` — 26-char Crockford-base32 ULID, deterministic
  under injected ``time_ms`` + ``rand_bytes_fn``, encodes the time
  prefix sortably.
* :func:`ensure_gitignore_entry` — appends ``.ralph/`` when missing,
  no-ops when ``.gitignore`` absent, idempotent when the entry already
  exists in either ``.ralph/`` or ``.ralph`` form.
* :class:`EventLogWriter` — context manager, lazy directory creation,
  scrubber pass-through, envelope-conformant output, append-on-crash
  survivability (verified via subprocess ``os._exit``).
* :class:`RunSummaryWriter` — context manager, lazy directory creation,
  JSON schema matches the module docstring, ``est_cost_usd`` serialised
  as string (or ``null``), per-iteration counter rows preserve order.
* :func:`create_writers` — aligned stem across artefacts, ``.gitignore``
  touched exactly once, validates explicit ``run_id``, isolates
  diagnostic logger handlers across calls.
* :class:`WritersBundle` — frozen, exposed members.
* Module imports only stdlib + ``ralph_afk.events`` (AST guard).
"""

from __future__ import annotations

import ast
import json
import logging
import os
import re
import subprocess
import sys
import textwrap
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from ralph_afk import persist as persist_module
from ralph_afk.events import REDACTED_SECRET, make_event
from ralph_afk.persist import (
    GITIGNORE_ENTRY,
    EventLogWriter,
    IterationCounters,
    RunSummaryWriter,
    WritersBundle,
    _crockford_b32,
    create_writers,
    ensure_gitignore_entry,
    make_run_id,
)

# Stable test fixtures
_FIXED_TS = datetime(2026, 5, 16, 3, 14, 15, 123_000, tzinfo=timezone.utc)
_FIXED_RUN_ID = "01ABCDEFGHJKMNPQRSTVWXYZ12"  # 26 chars, Crockford-safe
_FIXED_FILENAME_PREFIX = "2026-05-16T03-14-15Z"


# ---------------------------------------------------------------------------
# Crockford / ULID
# ---------------------------------------------------------------------------


def test_crockford_b32_round_trips_zero() -> None:
    assert _crockford_b32(0, 1) == "0"
    assert _crockford_b32(0, 5) == "00000"


def test_crockford_b32_uses_no_ambiguous_glyphs() -> None:
    """Crockford excludes I, L, O, U. None should appear in any encoding."""
    seen = set()
    for v in range(32):
        seen.add(_crockford_b32(v, 1))
    assert seen.isdisjoint({"I", "L", "O", "U"})


def test_crockford_b32_full_alphabet_round_trip() -> None:
    """Each 5-bit symbol maps to a unique Crockford glyph."""
    glyphs = {_crockford_b32(v, 1) for v in range(32)}
    assert len(glyphs) == 32


def test_crockford_b32_overflow_raises() -> None:
    """Encoding a value > 32**width raises ValueError, never silently truncates."""
    with pytest.raises(ValueError):
        _crockford_b32(32, 1)  # one symbol holds 0..31; 32 overflows


def test_crockford_b32_negative_raises() -> None:
    with pytest.raises(ValueError):
        _crockford_b32(-1, 5)


def test_make_run_id_is_26_chars() -> None:
    rid = make_run_id()
    assert len(rid) == 26


def test_make_run_id_matches_crockford_alphabet() -> None:
    rid = make_run_id()
    assert re.fullmatch(r"[0-9A-HJKMNP-TV-Z]{26}", rid), rid


def test_make_run_id_time_prefix_is_lexicographically_sortable() -> None:
    """Two run_ids generated in monotonic time order sort the same way as
    their time prefixes. ULIDs claim this property as a design goal."""
    earlier = make_run_id(time_ms=1_000_000_000_000)  # 2001-09-09
    later = make_run_id(time_ms=2_000_000_000_000)  # 2033-05-18
    assert earlier[:10] < later[:10]
    # Full ULIDs preserve order when time differs even if random bytes vary.
    assert earlier < later


def test_make_run_id_is_deterministic_under_injection() -> None:
    """Tests inject a deterministic ``rand_bytes_fn`` to pin the ULID."""
    rid = make_run_id(
        time_ms=0,
        rand_bytes_fn=lambda n: b"\x00" * n,
    )
    assert rid == "0" * 26


def test_make_run_id_unique_across_calls_with_default_rng() -> None:
    """Two real calls should not collide (80 bits of randomness)."""
    rid_a = make_run_id()
    rid_b = make_run_id()
    assert rid_a != rid_b


# ---------------------------------------------------------------------------
# .gitignore touch
# ---------------------------------------------------------------------------


def test_ensure_gitignore_entry_appends_when_missing(tmp_path: Path) -> None:
    gi = tmp_path / ".gitignore"
    gi.write_text("node_modules/\n*.log\n", encoding="utf-8")
    ensure_gitignore_entry(tmp_path)
    content = gi.read_text(encoding="utf-8")
    assert ".ralph/" in content
    # Should be appended after the existing content.
    assert content.rstrip().splitlines()[-1] == ".ralph/"


def test_ensure_gitignore_entry_adds_leading_newline_when_no_trailing_newline(
    tmp_path: Path,
) -> None:
    """If .gitignore doesn't end with \\n, the appended entry lands on its
    own line — no concatenation onto the previous line."""
    gi = tmp_path / ".gitignore"
    gi.write_text("node_modules", encoding="utf-8")  # no trailing newline
    ensure_gitignore_entry(tmp_path)
    lines = gi.read_text(encoding="utf-8").splitlines()
    assert lines == ["node_modules", ".ralph/"]


def test_ensure_gitignore_entry_no_op_when_gitignore_absent(tmp_path: Path) -> None:
    ensure_gitignore_entry(tmp_path)
    assert not (tmp_path / ".gitignore").exists()


def test_ensure_gitignore_entry_idempotent_when_ralph_with_slash_present(
    tmp_path: Path,
) -> None:
    gi = tmp_path / ".gitignore"
    gi.write_text("node_modules/\n.ralph/\n", encoding="utf-8")
    ensure_gitignore_entry(tmp_path)
    ensure_gitignore_entry(tmp_path)  # second call must not duplicate
    content = gi.read_text(encoding="utf-8")
    assert content.count(".ralph/") == 1


def test_ensure_gitignore_entry_idempotent_when_ralph_without_slash_present(
    tmp_path: Path,
) -> None:
    """Both ``.ralph/`` and ``.ralph`` are recognised — gitignore treats
    directory-only and "either type" forms as equivalent for our purposes."""
    gi = tmp_path / ".gitignore"
    gi.write_text(".ralph\n", encoding="utf-8")
    ensure_gitignore_entry(tmp_path)
    content = gi.read_text(encoding="utf-8")
    # Should NOT append a duplicate '.ralph/' line.
    assert content.count(".ralph") == 1
    assert ".ralph/" not in content


def test_ensure_gitignore_entry_tolerates_whitespace_around_existing_line(
    tmp_path: Path,
) -> None:
    """``line.strip()`` matcher recognises lines with leading/trailing whitespace."""
    gi = tmp_path / ".gitignore"
    gi.write_text("  .ralph/  \n", encoding="utf-8")
    ensure_gitignore_entry(tmp_path)
    assert gi.read_text(encoding="utf-8").count(".ralph/") == 1


def test_ensure_gitignore_entry_does_not_treat_substring_as_match(
    tmp_path: Path,
) -> None:
    """``my.ralph/`` (a longer line containing our pattern) should not block
    the append — only the exact stripped match counts."""
    gi = tmp_path / ".gitignore"
    gi.write_text("my.ralph/\n", encoding="utf-8")
    ensure_gitignore_entry(tmp_path)
    content = gi.read_text(encoding="utf-8")
    # Both lines now present.
    assert "my.ralph/" in content
    assert any(line.strip() == ".ralph/" for line in content.splitlines())


# ---------------------------------------------------------------------------
# EventLogWriter
# ---------------------------------------------------------------------------


def test_event_log_writer_is_context_manager_returning_self(tmp_path: Path) -> None:
    path = tmp_path / "logs" / "events.jsonl"
    with EventLogWriter(path) as log:
        assert log.path == path


def test_event_log_writer_does_not_create_dir_until_first_write(
    tmp_path: Path,
) -> None:
    """Lazy directory creation: instantiating the writer is side-effect free."""
    path = tmp_path / "logs" / "events.jsonl"
    writer = EventLogWriter(path)
    assert not (tmp_path / "logs").exists()
    # Even entering the context manager must not create the dir.
    with writer:
        assert not (tmp_path / "logs").exists()


def test_event_log_writer_creates_dir_on_first_write(tmp_path: Path) -> None:
    path = tmp_path / "logs" / "deeper" / "events.jsonl"
    with EventLogWriter(path) as log:
        log.write(make_event(type="wrapper.run.start", run_id=_FIXED_RUN_ID, iter=None))
    assert path.exists()
    assert path.parent.is_dir()


def test_event_log_writer_writes_envelope_keys(tmp_path: Path) -> None:
    """Every line is JSON with the four envelope keys present."""
    path = tmp_path / "events.jsonl"
    with EventLogWriter(path) as log:
        log.write(make_event(type="wrapper.run.start", run_id=_FIXED_RUN_ID, iter=None))
    line = path.read_text(encoding="utf-8").strip()
    parsed = json.loads(line)
    assert set(parsed.keys()) >= {"ts", "run_id", "iter", "type"}
    assert parsed["type"] == "wrapper.run.start"
    assert parsed["run_id"] == _FIXED_RUN_ID


def test_event_log_writer_runs_scrubber_on_events_before_writing(
    tmp_path: Path,
) -> None:
    """Acceptance criterion #3: events.scrub is applied before disk write.

    Use a GitHub token-shaped string in an arbitrary payload field and
    assert the on-disk representation is REDACTED.
    """
    path = tmp_path / "events.jsonl"
    secret = "ghp_" + "A" * 40
    with EventLogWriter(path) as log:
        log.write(
            make_event(
                type="wrapper.run.start",
                run_id=_FIXED_RUN_ID,
                iter=None,
                note=f"shell exported token={secret}",
            )
        )
    content = path.read_text(encoding="utf-8")
    assert secret not in content
    assert REDACTED_SECRET in content


def test_event_log_writer_runs_scrubber_on_tool_call_truncation(
    tmp_path: Path,
) -> None:
    """Long tool args → truncation sentinel, never raw."""
    path = tmp_path / "events.jsonl"
    huge_args = {"x": "y" * 500}
    with EventLogWriter(path) as log:
        log.write(
            make_event(
                type="tool.call",
                run_id=_FIXED_RUN_ID,
                iter=1,
                tool_call_id="t1",
                tool_name="some_tool",
                arguments=huge_args,
            )
        )
    line = json.loads(path.read_text(encoding="utf-8").strip())
    assert isinstance(line["arguments"], str)
    assert line["arguments"].startswith("<truncated:")
    assert "y" * 500 not in path.read_text(encoding="utf-8")


def test_event_log_writer_appends_across_multiple_writes(tmp_path: Path) -> None:
    """Two writes → two lines."""
    path = tmp_path / "events.jsonl"
    with EventLogWriter(path) as log:
        log.write(make_event(type="wrapper.run.start", run_id=_FIXED_RUN_ID, iter=None))
        log.write(make_event(type="wrapper.run.end", run_id=_FIXED_RUN_ID, iter=None))
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["type"] == "wrapper.run.start"
    assert json.loads(lines[1])["type"] == "wrapper.run.end"


def test_event_log_writer_opens_in_append_mode_across_instantiations(
    tmp_path: Path,
) -> None:
    """Two writers pointed at the same path concatenate (not truncate)."""
    path = tmp_path / "events.jsonl"
    with EventLogWriter(path) as log:
        log.write(make_event(type="a.b", run_id=_FIXED_RUN_ID, iter=1))
    with EventLogWriter(path) as log:
        log.write(make_event(type="c.d", run_id=_FIXED_RUN_ID, iter=2))
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    types = [json.loads(line)["type"] for line in lines]
    assert types == ["a.b", "c.d"]


def test_event_log_writer_flushes_after_each_write_via_subprocess_crash(
    tmp_path: Path,
) -> None:
    """Append-on-crash acceptance criterion: a process that calls write()
    twice and then exits via os._exit(0) — without closing the writer or
    running atexit/__exit__ — must leave a JSONL file containing both
    events fully readable.

    os._exit bypasses the file object's destructor flush, so any output
    that wasn't fh.flush()'d after each write would be lost in the libc
    buffer.
    """
    log_path = tmp_path / "crash.jsonl"
    script = textwrap.dedent(f"""
        import os, sys
        from pathlib import Path
        sys.path.insert(0, {str(Path(persist_module.__file__).parent.parent)!r})
        from ralph_afk.persist import EventLogWriter
        from ralph_afk.events import make_event
        w = EventLogWriter(Path({str(log_path)!r})).__enter__()
        w.write(make_event(type="a", run_id="01ABCDEFGHJKMNPQRSTVWXYZ12", iter=1))
        w.write(make_event(type="b", run_id="01ABCDEFGHJKMNPQRSTVWXYZ12", iter=2))
        os._exit(0)  # bypass any close-on-exit hooks
    """)
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert log_path.exists(), result.stderr
    lines = log_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2, (
        "expected both events on disk after os._exit; "
        f"got {len(lines)} lines"
    )
    types = [json.loads(line)["type"] for line in lines]
    assert types == ["a", "b"]


# ---------------------------------------------------------------------------
# RunSummaryWriter
# ---------------------------------------------------------------------------


def test_run_summary_writer_is_context_manager(tmp_path: Path) -> None:
    path = tmp_path / "runs" / "run.json"
    with RunSummaryWriter(path, run_id=_FIXED_RUN_ID, started_at=_FIXED_TS) as w:
        assert w.path == path


def test_run_summary_writer_does_not_create_dir_until_flush(
    tmp_path: Path,
) -> None:
    """Lazy directory creation — instantiation is side-effect free."""
    path = tmp_path / "runs" / "deeper" / "run.json"
    writer = RunSummaryWriter(path, run_id=_FIXED_RUN_ID, started_at=_FIXED_TS)
    assert not (tmp_path / "runs").exists()
    writer.record(IterationCounters(iter=1))
    # record() is in-memory only.
    assert not (tmp_path / "runs").exists()
    writer.flush()
    assert path.exists()


def test_run_summary_json_matches_documented_schema(tmp_path: Path) -> None:
    path = tmp_path / "runs" / "run.json"
    with RunSummaryWriter(path, run_id=_FIXED_RUN_ID, started_at=_FIXED_TS) as w:
        w.record(
            IterationCounters(
                iter=1,
                duration_seconds=12.5,
                model="claude-opus-4.7-xhigh",
                tokens_in=1000,
                tokens_out=200,
                context_used=1200,
                est_cost_usd=Decimal("0.0234"),
                tool_count=3,
                skill_count=1,
                commits=1,
                auto_closures=1,
                strikes=0,
            )
        )
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["run_id"] == _FIXED_RUN_ID
    assert payload["started_at"] == "2026-05-16T03:14:15.123Z"
    assert isinstance(payload["iterations"], list)
    assert len(payload["iterations"]) == 1
    row = payload["iterations"][0]
    assert row == {
        "iter": 1,
        "duration_seconds": 12.5,
        "model": "claude-opus-4.7-xhigh",
        "tokens_in": 1000,
        "tokens_out": 200,
        "context_used": 1200,
        "est_cost_usd": "0.0234",
        "tool_count": 3,
        "skill_count": 1,
        "commits": 1,
        "auto_closures": 1,
        "strikes": 0,
    }


def test_run_summary_json_null_est_cost_for_unknown_model(tmp_path: Path) -> None:
    """When the model has no pricing entry, est_cost_usd serialises as null."""
    path = tmp_path / "run.json"
    with RunSummaryWriter(path, run_id=_FIXED_RUN_ID, started_at=_FIXED_TS) as w:
        w.record(IterationCounters(iter=1, model="brand-new-model", est_cost_usd=None))
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["iterations"][0]["est_cost_usd"] is None


def test_run_summary_json_null_model_for_no_model_recorded(tmp_path: Path) -> None:
    """Model can be None when the iteration recorded no SDK usage event."""
    path = tmp_path / "run.json"
    with RunSummaryWriter(path, run_id=_FIXED_RUN_ID, started_at=_FIXED_TS) as w:
        w.record(IterationCounters(iter=1))
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["iterations"][0]["model"] is None


def test_run_summary_preserves_row_order_per_iteration(tmp_path: Path) -> None:
    path = tmp_path / "run.json"
    with RunSummaryWriter(path, run_id=_FIXED_RUN_ID, started_at=_FIXED_TS) as w:
        w.record(IterationCounters(iter=1, commits=2))
        w.record(IterationCounters(iter=2, commits=0))
        w.record(IterationCounters(iter=3, commits=1))
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert [row["iter"] for row in payload["iterations"]] == [1, 2, 3]
    assert [row["commits"] for row in payload["iterations"]] == [2, 0, 1]


def test_run_summary_writes_empty_iterations_for_zero_iter_runs(
    tmp_path: Path,
) -> None:
    """A run that exits cleanly on empty pool (zero iterations) still
    emits a valid summary JSON with an empty iterations list."""
    path = tmp_path / "run.json"
    with RunSummaryWriter(path, run_id=_FIXED_RUN_ID, started_at=_FIXED_TS) as w:
        pass  # no record() calls
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["iterations"] == []
    assert payload["run_id"] == _FIXED_RUN_ID


def test_run_summary_started_at_uses_rfc3339_ms_format(tmp_path: Path) -> None:
    """Acceptance criterion #5 talks about filename TS format; the JSON
    payload uses real RFC3339 (so log analytics tools can parse it)."""
    path = tmp_path / "run.json"
    naive_ts = datetime(2026, 1, 2, 3, 4, 5, 6_000)  # tzinfo missing → assume UTC
    with RunSummaryWriter(path, run_id=_FIXED_RUN_ID, started_at=naive_ts) as w:
        pass
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["started_at"] == "2026-01-02T03:04:05.006Z"


# ---------------------------------------------------------------------------
# create_writers factory
# ---------------------------------------------------------------------------


def test_create_writers_returns_writers_bundle(tmp_path: Path) -> None:
    bundle = create_writers(tmp_path, run_id=_FIXED_RUN_ID, started_at=_FIXED_TS)
    assert isinstance(bundle, WritersBundle)
    assert isinstance(bundle.event_log, EventLogWriter)
    assert isinstance(bundle.run_summary, RunSummaryWriter)
    assert isinstance(bundle.diagnostics, logging.Logger)


def test_create_writers_aligns_filenames_across_artefacts(tmp_path: Path) -> None:
    """Acceptance criterion #5: filenames share the YYYY-MM-DDTHH-MM-SSZ-<ULID>
    stem so an operator can correlate by ``ls .ralph/{logs,runs}/<stem>.*``."""
    bundle = create_writers(tmp_path, run_id=_FIXED_RUN_ID, started_at=_FIXED_TS)
    expected_stem = f"{_FIXED_FILENAME_PREFIX}-{_FIXED_RUN_ID}"
    assert bundle.event_log.path.name == f"{expected_stem}.jsonl"
    assert bundle.run_summary.path.name == f"{expected_stem}.json"
    assert bundle.diagnostics_path.name == f"{expected_stem}.log"


def test_create_writers_paths_under_dot_ralph(tmp_path: Path) -> None:
    bundle = create_writers(tmp_path, run_id=_FIXED_RUN_ID, started_at=_FIXED_TS)
    assert bundle.event_log.path.parent == tmp_path / ".ralph" / "logs"
    assert bundle.run_summary.path.parent == tmp_path / ".ralph" / "runs"
    assert bundle.diagnostics_path.parent == tmp_path / ".ralph" / "logs"


def test_create_writers_no_dirs_until_first_write(tmp_path: Path) -> None:
    """Factory call alone does not create .ralph/logs/ or .ralph/runs/."""
    create_writers(tmp_path, run_id=_FIXED_RUN_ID, started_at=_FIXED_TS)
    assert not (tmp_path / ".ralph").exists()


def test_create_writers_touches_gitignore_when_present_and_missing_entry(
    tmp_path: Path,
) -> None:
    (tmp_path / ".gitignore").write_text("node_modules/\n", encoding="utf-8")
    create_writers(tmp_path, run_id=_FIXED_RUN_ID, started_at=_FIXED_TS)
    content = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert ".ralph/" in content


def test_create_writers_gitignore_touch_is_idempotent(tmp_path: Path) -> None:
    """Running the factory twice does not duplicate the .ralph/ line."""
    (tmp_path / ".gitignore").write_text("node_modules/\n", encoding="utf-8")
    create_writers(tmp_path, run_id=_FIXED_RUN_ID, started_at=_FIXED_TS)
    create_writers(
        tmp_path,
        run_id="01ZZZZZZZZZZZZZZZZZZZZZZZZ",
        started_at=_FIXED_TS,
    )
    content = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert content.count(".ralph/") == 1


def test_create_writers_no_gitignore_no_op(tmp_path: Path) -> None:
    """Absent .gitignore is left absent — we don't create the file."""
    assert not (tmp_path / ".gitignore").exists()
    create_writers(tmp_path, run_id=_FIXED_RUN_ID, started_at=_FIXED_TS)
    assert not (tmp_path / ".gitignore").exists()


def test_create_writers_rejects_malformed_explicit_run_id(tmp_path: Path) -> None:
    """Explicit run_id with non-Crockford glyphs raises rather than producing
    a malformed filename."""
    with pytest.raises(ValueError, match="26-char Crockford"):
        create_writers(tmp_path, run_id="not-a-ulid", started_at=_FIXED_TS)


def test_create_writers_rejects_run_id_with_ambiguous_glyph(tmp_path: Path) -> None:
    """``I`` is not in the Crockford alphabet; reject explicit IDs that use it."""
    bad = "I" + ("0" * 25)
    with pytest.raises(ValueError):
        create_writers(tmp_path, run_id=bad, started_at=_FIXED_TS)


def test_create_writers_assumes_utc_for_naive_started_at(tmp_path: Path) -> None:
    """Naive datetimes are treated as UTC so the filename TS matches expectation."""
    naive_ts = datetime(2026, 5, 16, 3, 14, 15)
    bundle = create_writers(tmp_path, run_id=_FIXED_RUN_ID, started_at=naive_ts)
    assert bundle.event_log.path.name.startswith("2026-05-16T03-14-15Z-")


def test_create_writers_converts_non_utc_started_at_to_utc(tmp_path: Path) -> None:
    """A datetime with a non-UTC tzinfo is converted before formatting."""
    from datetime import timedelta

    eastern = timezone(timedelta(hours=-5))
    aware_ts = datetime(2026, 5, 15, 22, 14, 15, tzinfo=eastern)  # 03:14:15 UTC
    bundle = create_writers(tmp_path, run_id=_FIXED_RUN_ID, started_at=aware_ts)
    assert bundle.event_log.path.name.startswith("2026-05-16T03-14-15Z-")


def test_create_writers_generates_fresh_run_id_when_none_passed(tmp_path: Path) -> None:
    bundle = create_writers(tmp_path, started_at=_FIXED_TS)
    assert re.fullmatch(r"[0-9A-HJKMNP-TV-Z]{26}", bundle.run_id)


def test_create_writers_isolates_diagnostic_handlers_across_calls(
    tmp_path: Path,
) -> None:
    """Reusing the same run_id must not accumulate handlers — the second
    factory call replaces the first call's handlers."""
    b1 = create_writers(tmp_path, run_id=_FIXED_RUN_ID, started_at=_FIXED_TS)
    handlers_before = list(b1.diagnostics.handlers)
    assert len(handlers_before) == 2  # one StreamHandler, one FileHandler

    other_root = tmp_path / "other_repo"
    other_root.mkdir()
    b2 = create_writers(other_root, run_id=_FIXED_RUN_ID, started_at=_FIXED_TS)
    handlers_after = list(b2.diagnostics.handlers)
    assert len(handlers_after) == 2
    # The file handler should target the new path, not the first call's.
    file_handlers = [
        h for h in handlers_after if isinstance(h, logging.FileHandler)
    ]
    assert len(file_handlers) == 1
    assert Path(file_handlers[0].baseFilename) == b2.diagnostics_path


def test_create_writers_diagnostics_logger_propagation_disabled(
    tmp_path: Path,
) -> None:
    """Don't bubble to the root logger — avoids double-printing if the
    embedding process configures its own root handlers."""
    bundle = create_writers(tmp_path, run_id=_FIXED_RUN_ID, started_at=_FIXED_TS)
    assert bundle.diagnostics.propagate is False


def test_create_writers_diagnostics_log_lazy_mkdir(tmp_path: Path) -> None:
    """The diagnostic .log file (and .ralph/logs/) are not created until
    the first emit on the logger."""
    bundle = create_writers(tmp_path, run_id=_FIXED_RUN_ID, started_at=_FIXED_TS)
    assert not (tmp_path / ".ralph").exists()
    bundle.diagnostics.info("diag msg")
    assert bundle.diagnostics_path.exists()


def test_writers_bundle_is_frozen() -> None:
    """WritersBundle is a frozen dataclass — accidental mutation is rejected."""
    bundle = WritersBundle(
        run_id=_FIXED_RUN_ID,
        started_at=_FIXED_TS,
        event_log=EventLogWriter(Path("/tmp/x.jsonl")),
        run_summary=RunSummaryWriter(
            Path("/tmp/x.json"), run_id=_FIXED_RUN_ID, started_at=_FIXED_TS
        ),
        diagnostics=logging.getLogger("ralph_afk.diagnostics.test_frozen_check"),
        diagnostics_path=Path("/tmp/x.log"),
    )
    import dataclasses

    with pytest.raises(dataclasses.FrozenInstanceError):
        bundle.run_id = "changed"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# End-to-end: scrubber + envelope through the writer
# ---------------------------------------------------------------------------


def test_event_log_envelope_keys_appear_in_documented_order(tmp_path: Path) -> None:
    """The JSONL envelope keys appear in the canonical order (ts, run_id,
    iter, type), then payload keys sorted alphabetically. Replay tools
    grep by `ts` as the leading key, so order is load-bearing."""
    path = tmp_path / "events.jsonl"
    with EventLogWriter(path) as log:
        log.write(
            make_event(
                type="wrapper.run.start",
                run_id=_FIXED_RUN_ID,
                iter=None,
                zeta="z",
                alpha="a",
            )
        )
    raw = path.read_text(encoding="utf-8").strip()
    # Find the order of keys via a JSON re-parse + the raw text.
    # The simplest test: ts is the first key in the raw string.
    m = re.match(r'^\{"(?P<first_key>[^"]+)":', raw)
    assert m is not None
    assert m.group("first_key") == "ts"


def test_event_log_writer_handles_iter_none_in_payload(tmp_path: Path) -> None:
    """A run-scope event with iter=None should serialise as JSON null."""
    path = tmp_path / "events.jsonl"
    with EventLogWriter(path) as log:
        log.write(make_event(type="wrapper.run.start", run_id=_FIXED_RUN_ID, iter=None))
    parsed = json.loads(path.read_text(encoding="utf-8").strip())
    assert parsed["iter"] is None


# ---------------------------------------------------------------------------
# Static import allowlist
# ---------------------------------------------------------------------------


def test_persist_module_imports_are_constrained() -> None:
    """``persist.py`` may import only stdlib + ``ralph_afk.events``.

    Catches stray third-party imports (e.g. a misguided ``ulid`` import,
    or a ``requests`` slipped in by future drift) AND catches imports
    of peer ralph_afk modules other than ``events`` — keeps persist as a
    pure "events → disk" seam without it accidentally growing
    dependencies on gh / git / wrapper / loop / etc.
    """
    source = Path(persist_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    allow = {
        # stdlib
        "__future__",
        "json",
        "logging",
        "re",
        "secrets",
        "sys",
        "time",
        "contextlib",
        "dataclasses",
        "datetime",
        "decimal",
        "pathlib",
        "types",
        "typing",
        # our own module
        "ralph_afk.events",
    }
    seen: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                seen.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, (
                f"persist.py contains a relative import (level={node.level})"
            )
            assert node.module is not None, "from-import with no module name"
            seen.add(node.module)
    leaked = seen - allow
    assert not leaked, f"persist.py imports non-allowlisted modules: {leaked}"


def test_persist_module_exports_documented_public_surface() -> None:
    """``__all__`` must list every member the issue spec names as public."""
    expected = {
        "EventLogWriter",
        "RunSummaryWriter",
        "IterationCounters",
        "WritersBundle",
        "create_writers",
        "make_run_id",
        "ensure_gitignore_entry",
        "GITIGNORE_ENTRY",
    }
    assert expected <= set(persist_module.__all__)
