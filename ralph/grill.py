"""Autonomous /grill-me + /write-prd + /prd-to-issues runner on the Copilot CLI Python SDK.

Phase 1 (looped, single ``CopilotSession``):
    Run ``/grill-me`` on the supplied filename or quote. Auto-accepts every
    recommendation, reusing the same in-process session so design context
    accumulates. Exits the loop when the assistant message contains
    ``<promise>GRILLING COMPLETE</promise>``.

Phase 1.5 (same session):
    A validation turn that catches premature ``GRILLING COMPLETE``.

Phase 2 (same session):
    Run ``/write-prd``. The agent emits ``<prd-path>/abs/path.md</prd-path>``;
    the script captures it (with a newest ``prds/*.md`` fallback) and verifies
    the file exists.

Phase 3 (NEW ``CopilotSession``, looped):
    Brand-new session running ``/prd-to-issues`` with the captured PRD path.
    Exits when the assistant message contains
    ``<promise>ISSUES COMPLETE</promise>``.

Usage:
    uv run python -m ralph.grill <file-or-quote> [<max-grill-iterations>] [-v|--verbose]
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import errno
import fcntl
import os
import re
import sys
from pathlib import Path

from rich.console import Console
from rich.markup import escape
from rich.text import Text

from ._runner import (
    DEFAULT_EFFORT,
    DEFAULT_MODEL,
    Formatter,
    Runner,
    UsageTotals,
)


GRILL_DONE = "<promise>GRILLING COMPLETE</promise>"
ISSUES_DONE = "<promise>ISSUES COMPLETE</promise>"
PRD_OPEN = "<prd-path>"
PRD_CLOSE = "</prd-path>"
PRD_TAG_RE = re.compile(re.escape(PRD_OPEN) + r"([^<]+)" + re.escape(PRD_CLOSE))
LOCK_PATH = ".ralph-grill.lock"


# ---------------------------------------------------------------------------
# Lock
# ---------------------------------------------------------------------------


class GrillLock:
    """Process-lifetime advisory lock on ``.ralph-grill.lock`` via fcntl.flock.

    The file is created if missing and is *intentionally* left on disk after
    release — unlinking after release would create a split-brain window where
    a second process can lock the dead inode while a third (re-)creates the
    file and locks a different inode. Stale files are harmless: ``flock`` is
    advisory and tied to the open FD, not the path.
    """

    def __init__(self, path: str = LOCK_PATH) -> None:
        self.path = path
        self._fd: int | None = None

    def acquire(self) -> bool:
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            if exc.errno in (errno.EAGAIN, errno.EACCES):
                return False
            raise
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        self._fd = fd
        return True

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(self._fd)
        except OSError:
            pass
        self._fd = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _usage_and_exit() -> None:
    sys.stderr.write(
        "Usage:\n"
        "  uv run python -m ralph.grill <file-or-quote> [<max-grill-iterations>] [-v|--verbose]\n\n"
        "Examples:\n"
        "  uv run python -m ralph.grill client-brief.md\n"
        '  uv run python -m ralph.grill "Build a recipes app for amateur chefs"\n'
        "  uv run python -m ralph.grill client-brief.md 30\n"
        "  uv run python -m ralph.grill client-brief.md --verbose\n"
        "  MODEL=gpt-5.4 EFFORT=high uv run python -m ralph.grill client-brief.md\n"
        "  MAX_ISSUES_ITERS=10 uv run python -m ralph.grill client-brief.md\n"
        "  RALPH_VERBOSE=1 uv run python -m ralph.grill client-brief.md\n\n"
        "Flags:\n"
        "  -v, --verbose  Show verbose tool diagnostics (full redacted args,\n"
        "                 all event-data fields on failures, success result\n"
        "                 sizes, and otherwise-silent SDK events). Also\n"
        "                 enabled by RALPH_VERBOSE=1; CLI flag wins.\n\n"
        "If <file-or-quote> resolves to an existing file, the agent is told to read it for\n"
        "context. Otherwise the value is treated as a verbatim quote and embedded directly.\n"
    )
    sys.exit(2)


def _truthy_env(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def _extract_verbose_flag(argv: list[str]) -> tuple[list[str], bool]:
    """Pop ``-v`` / ``--verbose`` from anywhere in ``argv``.

    Done manually instead of via ``argparse`` so the first positional — a
    free-form file path or quote — can still safely start with ``-``. Defaults
    to whatever ``RALPH_VERBOSE`` says; a CLI flag presence flips it to True.
    """

    verbose = _truthy_env("RALPH_VERBOSE")
    filtered: list[str] = []
    for arg in argv:
        if arg in ("-v", "--verbose"):
            verbose = True
        else:
            filtered.append(arg)
    return filtered, verbose


def _parse_int(value: str, label: str) -> int:
    try:
        n = int(value)
    except ValueError:
        sys.stderr.write(f"Error: {label} must be a non-negative integer (got: {value}).\n")
        sys.exit(2)
    if n < 0:
        sys.stderr.write(f"Error: {label} must be a non-negative integer (got: {value}).\n")
        sys.exit(2)
    return n


def _build_session_id(prefix: str) -> str:
    ts = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{prefix}-{ts}-{os.getpid()}"


def _extract_prd_path(text: str) -> str | None:
    matches = PRD_TAG_RE.findall(text or "")
    if not matches:
        return None
    return matches[-1].strip()


def _newest_prd() -> Path | None:
    prds_dir = Path("prds")
    if not prds_dir.is_dir():
        return None
    candidates = sorted(prds_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0] if candidates else None


# ---------------------------------------------------------------------------
# Prompts (verbatim from grill.sh)
# ---------------------------------------------------------------------------


def _kickoff_prompt(context_directive: str) -> str:
    return f"""/grill-me — {context_directive}

Run grill-me autonomously without asking the user any questions. For every
question you would normally ask, present your recommended answer and accept
it as the chosen direction (treat yourself as the answerer, accepting your own
recommendation). Walk every branch of the design tree and resolve dependencies
between decisions one-by-one.

When the shared design is fully resolved end-to-end, summarize the agreed
design and emit the literal sentinel as the FINAL line of your message:

{GRILL_DONE}

Do NOT emit the sentinel until the design is fully resolved."""


def _continue_prompt() -> str:
    return f"""Continue /grill-me from where we left off. Accept your previously
recommended answer for the last branch, then move to the next unresolved branch
of the design tree. Do NOT ask the user any questions.

When the design is fully resolved, emit the literal sentinel as the FINAL line
of your message:

{GRILL_DONE}"""


def _validation_prompt() -> str:
    return f"""Before producing the PRD, do a final consistency check on the
design we just agreed on.

List any decisions that are still open, ambiguous, or unresolved. If any
exist, resolve each by accepting your own recommended answer, then re-emit
the sentinel. If everything is fully resolved, simply re-emit the sentinel.

The sentinel must be the FINAL line of your message:

{GRILL_DONE}"""


def _prd_prompt(input_mode: str) -> str:
    return f"""/write-prd — Use the shared design consensus we just reached to
write the PRD.

Choose the output path per the skill's <output-path-rules> WITHOUT asking the
user for confirmation. This session was seeded from a {input_mode}; if you
need a slug for the filename and no source file basename is available, derive
a kebab-case slug from the agreed design.

After the file is successfully written, emit its absolute path on a line by
itself wrapped in this exact tag (the runner script captures it):

{PRD_OPEN}/absolute/path/to/the.md{PRD_CLOSE}

Emit the tag exactly once, only after the PRD file write succeeds, as the
FINAL content of your message."""


def _issues_kickoff_prompt(prd_path: str) -> str:
    return f"""/prd-to-issues — PRD path: {prd_path}

Run prd-to-issues autonomously without asking the user any questions. For
every question you would normally ask, accept your recommended answer and
proceed.

Locate and read the PRD at the path above, propose vertical-slice issues,
then write each issue file under issues/<core-name>/ per the skill's
<output-path-rules>.

When all issue files have been written, emit the literal sentinel as the
FINAL line of your message:

{ISSUES_DONE}"""


def _issues_continue_prompt(prd_path: str) -> str:
    return f"""Continue /prd-to-issues. Accept your own recommendations and
finish writing any remaining issue files for the PRD at:

{prd_path}

When all issue files for this PRD are written, emit the literal sentinel as
the FINAL line of your message:

{ISSUES_DONE}"""


# ---------------------------------------------------------------------------
# Async main
# ---------------------------------------------------------------------------


async def _async_main(
    input_arg: str,
    max_grill: int,
    max_issues: int,
    verbose: bool,
) -> int:
    console = Console(highlight=False, soft_wrap=True)

    input_path = Path(input_arg)
    if input_path.is_file():
        context_directive = (
            f"see file `{input_arg}` (in this repository) for context. Read it before proceeding."
        )
        input_mode = "file"
    else:
        context_directive = f"context (verbatim quote): {input_arg}"
        input_mode = "quote"

    grill_session_id = _build_session_id("grill")
    issues_session_id = _build_session_id("prd-to-issues")
    totals = UsageTotals()

    # Startup banner — prints synchronously before any I/O so the user sees
    # the script is alive while CopilotClient.start() warms up.
    if input_mode == "file":
        try:
            brief_size = input_path.stat().st_size
            input_summary = f"file '{input_arg}' ({brief_size:,} bytes)"
        except OSError:
            input_summary = f"file '{input_arg}'"
    else:
        snippet = input_arg if len(input_arg) <= 80 else input_arg[:77] + "…"
        input_summary = f"quote: {snippet!r}"
    console.print(
        Text(
            f"ralph.grill · model={DEFAULT_MODEL} · effort={DEFAULT_EFFORT}",
            style="bold cyan",
        )
    )
    console.print(
        Text(
            f"  input: {input_summary}",
            style="dim",
        )
    )
    console.print(
        Text(
            f"  caps: grill={max_grill or 'unlimited'}  issues={max_issues or 'unlimited'}",
            style="dim",
        )
    )
    if verbose:
        console.print(Text("  verbose: tool diagnostics enabled", style="dim"))

    async with Runner(
        console=console,
        model=DEFAULT_MODEL,
        effort=DEFAULT_EFFORT,
        client_name="ralph-grill",
        totals=totals,
        verbose=verbose,
    ) as runner:

        # ===================================================================
        # Phase 1 — /grill-me loop in a single session
        # ===================================================================
        async with runner.new_session(
            session_id=grill_session_id,
            title=f"Grill iteration 1 (kickoff)  ·  session {grill_session_id}",
        ) as (grill_session, grill_formatter):
            result = await runner.run_turn(
                grill_session, grill_formatter, _kickoff_prompt(context_directive)
            )

            i = 1
            while GRILL_DONE not in result.text:
                i += 1
                if max_grill and i > max_grill:
                    console.print(
                        f"[red]=== Grill iteration cap ({max_grill}) reached without "
                        f"{escape(GRILL_DONE)}.[/red]"
                    )
                    sys.stderr.write(
                        f"    Resume manually with: copilot --resume=\"{grill_session_id}\"\n"
                    )
                    return 1

                console.rule(
                    f"[bold]Grill iteration {i}[/bold]  ·  session {grill_session_id}",
                    style="cyan",
                )
                result = await runner.run_turn(
                    grill_session, grill_formatter, _continue_prompt()
                )

            console.print(
                f"[green]=== Grilling sentinel detected after {i} iteration(s). ===[/green]"
            )

            # =================================================================
            # Phase 1.5 — Validation turn
            # =================================================================
            console.rule(
                f"[bold]Grill validation turn[/bold]  ·  session {grill_session_id}",
                style="cyan",
            )
            result = await runner.run_turn(
                grill_session, grill_formatter, _validation_prompt()
            )
            if GRILL_DONE not in result.text:
                console.print(
                    f"[red]=== Validation turn did not re-emit {escape(GRILL_DONE)}; "
                    "aborting before PRD generation.[/red]"
                )
                sys.stderr.write(
                    f"    Resume manually with: copilot --resume=\"{grill_session_id}\"\n"
                )
                return 1

            # =================================================================
            # Phase 2 — /write-prd in same session
            # =================================================================
            console.rule(
                f"[bold]/write-prd[/bold] (same session)  ·  session {grill_session_id}",
                style="cyan",
            )
            result = await runner.run_turn(
                grill_session, grill_formatter, _prd_prompt(input_mode)
            )

        prd_path_str = _extract_prd_path(result.text)
        prd_path: Path | None = None
        if prd_path_str:
            candidate = Path(prd_path_str)
            if candidate.is_file():
                prd_path = candidate
        if prd_path is None:
            console.print(
                "[yellow]=== <prd-path> tag missing or path not found; "
                "falling back to newest prds/*.md ===[/yellow]"
            )
            prd_path = _newest_prd()
        if prd_path is None or not prd_path.is_file():
            console.print("[red]Error: could not locate a generated PRD under prds/.[/red]")
            sys.stderr.write(
                f"    Resume manually with: copilot --resume=\"{grill_session_id}\"\n"
            )
            return 1
        prd_path = prd_path.resolve()
        console.print(f"[green]=== PRD written: {prd_path} ===[/green]")
        console.print(f"[dim]=== Closing grill session: {grill_session_id} ===[/dim]")

        # ===================================================================
        # Phase 3 — /prd-to-issues in a NEW session, looped
        # ===================================================================
        async with runner.new_session(
            session_id=issues_session_id,
            title=(
                f"/prd-to-issues iteration 1 (kickoff)  ·  session {issues_session_id}"
            ),
        ) as (issues_session, issues_formatter):
            result = await runner.run_turn(
                issues_session,
                issues_formatter,
                _issues_kickoff_prompt(str(prd_path)),
            )
            j = 1
            while ISSUES_DONE not in result.text:
                j += 1
                if max_issues and j > max_issues:
                    console.print(
                        f"[red]=== /prd-to-issues iteration cap ({max_issues}) reached without "
                        f"{escape(ISSUES_DONE)}.[/red]"
                    )
                    sys.stderr.write(
                        f"    Resume manually with: copilot --resume=\"{issues_session_id}\"\n"
                    )
                    return 1
                console.rule(
                    f"[bold]/prd-to-issues iteration {j}[/bold]  ·  session {issues_session_id}",
                    style="cyan",
                )
                result = await runner.run_turn(
                    issues_session,
                    issues_formatter,
                    _issues_continue_prompt(str(prd_path)),
                )

        console.print("[green]=== Done. ===[/green]")
        console.print(f"    PRD:            {prd_path}")
        console.print(f"    Grill session:  {grill_session_id}")
        console.print(f"    Issues session: {issues_session_id}")

        # Final totals across all phases.
        Formatter(console, totals=totals).render_final_stats(title="grill totals")

    return 0


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def main() -> None:
    argv, verbose = _extract_verbose_flag(sys.argv)
    if len(argv) < 2 or not argv[1]:
        _usage_and_exit()
    input_arg = argv[1]
    max_grill = _parse_int(argv[2], "<max-grill-iterations>") if len(argv) >= 3 else 0
    max_issues = _parse_int(os.environ.get("MAX_ISSUES_ITERS", "0"), "MAX_ISSUES_ITERS")

    lock = GrillLock()
    if not lock.acquire():
        sys.stderr.write(
            f"Error: another ralph/grill run appears to be in progress (lock: {LOCK_PATH}).\n"
            f"       Remove the file if you are sure it is stale: rm {LOCK_PATH}\n"
        )
        sys.exit(1)

    try:
        rc = asyncio.run(_async_main(input_arg, max_grill, max_issues, verbose))
    except KeyboardInterrupt:
        sys.stderr.write("interrupted\n")
        rc = 130
    finally:
        lock.release()
    sys.exit(rc)


if __name__ == "__main__":
    main()
