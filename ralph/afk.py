"""Autonomous ralph loop on the GitHub Copilot CLI Python SDK.

Each iteration creates a fresh ``CopilotSession`` (one ``copilot`` invocation
per iteration) and runs the same combined prompt: 5 most recent commits, the
contents of every open issue under ``issues/`` (excluding ``done/``), and
``ralph/prompt.md``. The loop exits when the iteration's terminal assistant
message contains the sentinel ``<promise>NO MORE TASKS</promise>``.

Usage:
    uv run python -m ralph.afk                # unlimited iterations
    uv run python -m ralph.afk 50             # cap at 50
    uv run python -m ralph.afk --verbose      # full tool diagnostics
    MODEL=gpt-5.4 EFFORT=high uv run python -m ralph.afk
    RALPH_VERBOSE=1 uv run python -m ralph.afk
"""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import subprocess
import sys
from pathlib import Path

from rich.console import Console
from rich.markup import escape
from rich.text import Text

from ._runner import (
    DEFAULT_EFFORT,
    DEFAULT_MODEL,
    Runner,
    UsageTotals,
)


SENTINEL = "<promise>NO MORE TASKS</promise>"
PROMPT_FILE = Path("ralph/prompt.md")


# ---------------------------------------------------------------------------


def _gather_commits() -> str:
    if not shutil.which("git"):
        return "No commits found"
    try:
        out = subprocess.run(
            ["git", "log", "-n", "5", "--format=%H%n%ad%n%B---", "--date=short"],
            capture_output=True,
            text=True,
            check=False,
        )
    except Exception:
        return "No commits found"
    text = (out.stdout or "").strip()
    return text or "No commits found"


def _gather_issues(root: Path = Path("issues")) -> str:
    if not root.is_dir():
        return "No issues found"
    chunks: list[str] = []
    for path in sorted(root.rglob("*.md")):
        # Skip anything under a 'done/' directory at any depth.
        if any(part == "done" for part in path.relative_to(root).parts):
            continue
        try:
            chunks.append(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
    return "\n".join(chunks).strip() or "No issues found"


def _count_open_issues(root: Path = Path("issues")) -> tuple[int, int]:
    """Return (open_file_count, project_dir_count) for the issues tree."""

    if not root.is_dir():
        return (0, 0)
    files = 0
    projects: set[str] = set()
    for path in root.rglob("*.md"):
        rel_parts = path.relative_to(root).parts
        if any(part == "done" for part in rel_parts):
            continue
        files += 1
        if rel_parts:
            projects.add(rel_parts[0])
    return (files, len(projects))


def _build_prompt() -> str:
    commits = _gather_commits()
    issues = _gather_issues()
    base_prompt = PROMPT_FILE.read_text(encoding="utf-8")
    # Mirror afk.sh exactly:  "Previous commits: $commits Issues: $issues $prompt"
    return f"Previous commits: {commits} Issues: {issues} {base_prompt}"


# ---------------------------------------------------------------------------


def _truthy_env(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def _parse_args(argv: list[str]) -> tuple[int, bool]:
    parser = argparse.ArgumentParser(
        prog="ralph.afk",
        description=(
            "Run the autonomous ralph loop. By default iterations is "
            "unlimited; the loop exits when an iteration emits the "
            "NO MORE TASKS sentinel."
        ),
    )
    parser.add_argument(
        "iterations",
        nargs="?",
        type=int,
        default=0,
        help="Maximum iterations (default: unlimited; 0 also means unlimited)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        default=_truthy_env("RALPH_VERBOSE"),
        help=(
            "Show verbose tool diagnostics: full (redacted) tool arguments, "
            "all event-data fields on tool failures, success result sizes, "
            "and otherwise-silent SDK events. CLI flag wins over env. "
            "Also enabled by RALPH_VERBOSE=1."
        ),
    )
    args = parser.parse_args(argv[1:])
    if args.iterations < 0:
        parser.error("iterations must be >= 0 (0 = unlimited)")
    return args.iterations, args.verbose


async def _async_main(max_iterations: int, verbose: bool) -> int:
    if not PROMPT_FILE.is_file():
        sys.stderr.write(
            "Error: ralph/prompt.md not found. Run this script from the repo root.\n"
        )
        return 1

    console = Console(highlight=False, soft_wrap=True)
    totals = UsageTotals()

    # Startup banner: prints synchronously before any I/O so the user gets
    # immediate feedback that the script is alive while CopilotClient.start()
    # spawns the copilot subprocess and runs list_models().
    cap_str = "unlimited" if not max_iterations else str(max_iterations)
    file_count, project_count = _count_open_issues()
    if file_count == 0:
        backlog = "0 open issue files (kanban is empty — agent will exit immediately)"
    else:
        backlog = f"{file_count} open issue file(s) across {project_count} project(s)"
    console.print(
        Text(
            f"ralph.afk · model={DEFAULT_MODEL} · effort={DEFAULT_EFFORT}",
            style="bold cyan",
        )
    )
    console.print(
        Text(
            f"  iterations: {cap_str} · backlog: {backlog}",
            style="dim",
        )
    )
    if verbose:
        console.print(
            Text("  verbose: tool diagnostics enabled", style="dim")
        )

    async with Runner(
        console=console,
        model=DEFAULT_MODEL,
        effort=DEFAULT_EFFORT,
        client_name="ralph-afk",
        totals=totals,
        verbose=verbose,
    ) as runner:
        i = 0
        while True:
            i += 1
            if max_iterations and i > max_iterations:
                console.print(
                    f"[yellow]=== Reached iteration limit ({max_iterations}) "
                    f"without {escape(SENTINEL)}; exiting. ===[/yellow]"
                )
                return 0

            console.rule(f"[bold]Iteration {i}[/bold]", style="cyan")
            prompt = _build_prompt()
            console.print(
                Text(f"  prompt: {len(prompt):,} chars", style="dim")
            )

            async with runner.new_session() as (session, formatter):
                result = await runner.run_turn(session, formatter, prompt)

            if SENTINEL in (result.text or ""):
                console.print(
                    f"[green]=== Iteration {i} emitted {escape(SENTINEL)} — exiting. ===[/green]"
                )
                runner_formatter_totals = totals
                # Render the global totals once at exit.
                from ._runner import Formatter

                Formatter(console, totals=runner_formatter_totals).render_final_stats(
                    title="afk totals"
                )
                return 0


def main() -> None:
    iterations, verbose = _parse_args(sys.argv)
    try:
        rc = asyncio.run(_async_main(iterations, verbose))
    except KeyboardInterrupt:
        sys.stderr.write("interrupted\n")
        sys.exit(130)
    sys.exit(rc)


if __name__ == "__main__":
    main()
