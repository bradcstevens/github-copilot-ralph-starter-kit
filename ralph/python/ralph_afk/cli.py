"""``ralph-afk`` console-script entry point.

Composes a :class:`ralph_afk.config.RunConfig` from CLI flags + env vars
+ defaults, then hands off to :func:`ralph_afk.loop.run` via
:func:`asyncio.run`.

Precedence rules:

* CLI flags win over environment variables for scalar knobs (``MODEL``,
  ``ISSUE_SOURCE``, ``MAX_NMT_STRIKES``, verbosity, ``--no-reasoning``).
* For the collection-valued denylists (``--deny-tool`` / ``--deny-skill``
  vs ``RALPH_DENY_TOOLS`` / ``RALPH_DENY_SKILLS``), **CLI flags are
  ADDITIVE to the env-var baseline** — the final denylist is the set
  union of both sources. This is a deliberate security-positive
  divergence from "CLI wins": a wrapper script that sets an env-var
  baseline (e.g. ``RALPH_DENY_TOOLS=bash``) must not be silently
  overridden by an absent CLI flag. To remove an env baseline, unset
  the env var or use ``-E`` semantics in the wrapper script.

CLI surface — mirrors ``ralph/sh-afk.sh`` and extends it with the new
deep-module knobs:

* Positional ``<max-iterations>`` — ``0`` (or omitted) means unlimited.
* ``-v`` / ``-vv`` / ``-vvv`` — verbosity ladder owned by the renderer.
* ``--no-reasoning`` — suppresses assistant reasoning output.
* ``--deny-tool TOOL`` — repeatable; permission-handler denylist.
* ``--deny-skill SKILL`` — repeatable; permission-handler denylist
  applied to the ``skill`` meta-tool's ``arguments.skill`` field.

Env vars:

* ``MODEL`` — Copilot model id override.
* ``REASONING_EFFORT`` — Optional reasoning-effort override
  (``low`` / ``medium`` / ``high`` / ``xhigh``). When unset, the runner
  auto-derives a safe default from the resolved model id's suffix —
  e.g. ``claude-opus-4.7-xhigh`` → ``xhigh`` — so the kit's default
  model works out-of-the-box without a 400 from the backend.
* ``ISSUE_SOURCE`` — ``github`` (default, GitHub issues backend) or
  ``prds`` (legacy local-markdown ``prds/<feature>/NNN-*.md`` backend).
* ``MAX_NMT_STRIKES`` — strike threshold (integer ≥ 1).
* ``RALPH_DENY_TOOLS`` — comma-separated tool denylist (set-unioned
  with ``--deny-tool`` flags).
* ``RALPH_DENY_SKILLS`` — comma-separated skill denylist.
* ``RALPH_PRICING_FILE`` — optional explicit ``pricing.toml`` path
  (overrides the default live pricing catalog/cache path).
* ``RALPH_OTEL_ENABLED`` — truthy ``"1"`` enables OTel plumbing
  (operative wiring lands in issue #12).
* ``OTEL_EXPORTER_OTLP_ENDPOINT`` — presence enables OTel.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
from pathlib import Path

from ralph_afk.config import REASONING_EFFORTS, RunConfig

__all__ = ["main", "build_parser", "resolve_repo_root"]

_DEFAULT_MAX_NMT_STRIKES = 3
# Mirrors bash ``ralph/sh-afk.sh:65`` so a wrapper script calling either
# variant with no ``MODEL`` set produces parity behaviour.
_DEFAULT_MODEL = "claude-opus-4.7-xhigh"


def resolve_repo_root(start: Path | None = None) -> Path:
    """Resolve the enclosing git repository's top-level directory.

    Kept as a thin shell around ``git rev-parse --show-toplevel`` so the
    *very early* stderr message ("not a git repo / git not on PATH")
    can fire before we import the loop module (which would pull in the
    SDK and Rich and add seconds to cold-start latency on a clearly
    failing invocation).

    Args:
        start: Optional directory to run the ``git`` lookup from;
            defaults to the current working directory.

    Returns:
        Absolute :class:`Path` to the repository root.

    Raises:
        RuntimeError: If ``git`` is not on PATH or ``start`` is not
            inside a git repository.
    """
    cwd = str(start) if start is not None else None
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            "ralph-afk requires `git` on PATH (not found). "
            "Install git and re-run."
        ) from exc

    if completed.returncode != 0:
        stderr_tail = (completed.stderr or "").strip().splitlines()[-1:]
        detail = stderr_tail[0] if stderr_tail else "(no stderr output)"
        raise RuntimeError(
            "ralph-afk must be invoked from inside a git repository "
            f"(`git rev-parse --show-toplevel` failed: {detail})."
        )

    return Path(completed.stdout.strip()).resolve()


def _parse_max_iterations(raw: str) -> int:
    """Validate the positional ``<max-iterations>`` arg as a non-negative int."""
    try:
        value = int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"max_iterations must be a non-negative integer, got {raw!r}"
        ) from exc
    if value < 0:
        raise argparse.ArgumentTypeError(
            f"max_iterations must be non-negative, got {value}"
        )
    return value


def build_parser() -> argparse.ArgumentParser:
    """Construct the argparse parser for the ``ralph-afk`` console script."""
    parser = argparse.ArgumentParser(
        prog="ralph-afk",
        description=(
            "Autonomous AFK loop on the GitHub Copilot Python SDK. "
            "Peer variant of ralph/sh-afk.sh — same wrapper contract, "
            "richer terminal UX."
        ),
        epilog=(
            "Environment variables:\n"
            "  MODEL                       Copilot model id override.\n"
            "  REASONING_EFFORT            Reasoning-effort override "
            "(low|medium|high|xhigh).\n"
            "                              When unset, auto-derived "
            "from the model id suffix\n"
            "                              (e.g. "
            "claude-opus-4.7-xhigh → xhigh).\n"
            "  ISSUE_SOURCE                'github' (default) or 'prds' "
            "(legacy local-markdown).\n"
            "  MAX_NMT_STRIKES             Strike threshold (default: 3).\n"
            "  RALPH_DENY_TOOLS            Comma-separated tool denylist.\n"
            "  RALPH_DENY_SKILLS           Comma-separated skill denylist.\n"
            "  RALPH_PRICING_FILE          Optional pricing.toml override.\n"
            "  RALPH_OTEL_ENABLED          Truthy '1' enables OTel.\n"
            "  OTEL_EXPORTER_OTLP_ENDPOINT  Presence enables OTel.\n"
            "  RALPH_SEND_TIMEOUT_SECONDS  send_and_wait timeout "
            "(default: 7200).\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "max_iterations",
        nargs="?",
        type=_parse_max_iterations,
        default=0,
        metavar="<max-iterations>",
        help=(
            "Cap the number of iterations (0 or omitted = unlimited; "
            "default: 0). Mirrors the positional arg accepted by "
            "ralph/sh-afk.sh."
        ),
    )
    parser.add_argument(
        "-v",
        dest="verbosity",
        action="count",
        default=0,
        help=(
            "Increase verbosity. -v shows tool results; -vv adds reasoning; "
            "-vvv raw-dumps every event (including session/permission)."
        ),
    )
    parser.add_argument(
        "--no-reasoning",
        dest="render_reasoning",
        action="store_false",
        default=True,
        help=(
            "Suppress assistant reasoning output. Wins over -v/-vv/-vvv."
        ),
    )
    parser.add_argument(
        "--deny-tool",
        dest="deny_tools",
        action="append",
        default=[],
        metavar="TOOL",
        help=(
            "Reject the named tool at the SDK permission gate. Repeatable. "
            "Unioned with RALPH_DENY_TOOLS env var."
        ),
    )
    parser.add_argument(
        "--deny-skill",
        dest="deny_skills",
        action="append",
        default=[],
        metavar="SKILL",
        help=(
            "Reject the named skill (the `skill` meta-tool's "
            "arguments.skill value) at the permission gate. Repeatable. "
            "Unioned with RALPH_DENY_SKILLS env var."
        ),
    )
    return parser


def _parse_csv_env(value: str | None) -> list[str]:
    """Parse a comma-separated env-var value into a stripped list.

    Empty or whitespace-only entries are dropped so a stray trailing
    comma doesn't produce an empty-string denylist member.
    """
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _is_truthy(value: str | None) -> bool:
    """Match the conventional truthy-env-var spelling used elsewhere in the kit."""
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _otel_enabled() -> bool:
    """Derive ``otel_enabled`` from the two recognised env vars."""
    if _is_truthy(os.environ.get("RALPH_OTEL_ENABLED")):
        return True
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "")
    return bool(endpoint.strip())


def _resolve_max_nmt_strikes() -> int:
    """Read and validate ``MAX_NMT_STRIKES`` env var; fall back to default."""
    raw = os.environ.get("MAX_NMT_STRIKES")
    if raw is None or not raw.strip():
        return _DEFAULT_MAX_NMT_STRIKES
    try:
        value = int(raw)
    except ValueError as exc:
        raise SystemExit(
            f"ralph-afk: error: MAX_NMT_STRIKES must be a positive integer, "
            f"got {raw!r}"
        ) from exc
    if value < 1:
        raise SystemExit(
            f"ralph-afk: error: MAX_NMT_STRIKES must be ≥ 1, got {value}"
        )
    return value


def _resolve_issue_source() -> str:
    """Read and validate ``ISSUE_SOURCE`` env var; default ``"github"``."""
    source = os.environ.get("ISSUE_SOURCE", "github")
    if source not in {"github", "prds"}:
        raise SystemExit(
            f"ralph-afk: error: ISSUE_SOURCE must be 'github' or 'prds' "
            f"(got {source!r})."
        )
    return source


def _resolve_pricing_file() -> Path | None:
    """Read ``RALPH_PRICING_FILE`` and return a Path or None."""
    raw = os.environ.get("RALPH_PRICING_FILE")
    if raw is None or not raw.strip():
        return None
    return Path(raw)


def _derive_reasoning_effort_from_model(model: str | None) -> str | None:
    """Derive a default ``reasoning_effort`` from a model id suffix.

    Some Copilot model ids pin the supported reasoning effort to a
    single value and surface that pin via the trailing ``-<effort>``
    segment of the model id — for example ``claude-opus-4.7-xhigh``
    only accepts ``"xhigh"`` and the backend rejects the service-default
    ``"medium"`` with a CAPI 400. Returning the suffix matches the only
    supported value for such variants so the SDK call goes through
    on the very first iteration.

    Models without a recognised suffix return ``None`` so the SDK
    sends no ``reasoningEffort`` field and the backend applies its own
    default — preserving today's behaviour for non-pinned models.

    Args:
        model: The resolved model id, or ``None``.

    Returns:
        One of ``"low"`` / ``"medium"`` / ``"high"`` / ``"xhigh"`` if
        the model id ends with that suffix, otherwise ``None``.
    """
    if not model:
        return None
    for effort in REASONING_EFFORTS:
        if model.endswith(f"-{effort}"):
            return effort
    return None


def _resolve_reasoning_effort(model: str | None) -> str | None:
    """Resolve ``reasoning_effort`` from env-var override or model suffix.

    Precedence:

    1. ``REASONING_EFFORT`` env var (must be one of the documented
       literals; an invalid value is a hard ``SystemExit`` because the
       SDK would otherwise raise mid-iteration).
    2. Auto-derived from the model id (see
       :func:`_derive_reasoning_effort_from_model`).
    3. ``None`` (let the backend pick).
    """
    raw = os.environ.get("REASONING_EFFORT")
    if raw is not None and raw.strip():
        candidate = raw.strip().lower()
        if candidate not in REASONING_EFFORTS:
            raise SystemExit(
                f"ralph-afk: error: REASONING_EFFORT must be one of "
                f"{sorted(REASONING_EFFORTS)}, got {raw!r}"
            )
        return candidate
    return _derive_reasoning_effort_from_model(model)


def _build_config(args: argparse.Namespace) -> RunConfig:
    """Compose a :class:`RunConfig` from parsed CLI args + env vars."""
    # CLI flags + env-var union for the denylists.
    deny_tools = set(args.deny_tools) | set(
        _parse_csv_env(os.environ.get("RALPH_DENY_TOOLS"))
    )
    deny_skills = set(args.deny_skills) | set(
        _parse_csv_env(os.environ.get("RALPH_DENY_SKILLS"))
    )

    verbosity = min(max(int(args.verbosity), 0), 3)

    issue_source = _resolve_issue_source()
    max_nmt_strikes = _resolve_max_nmt_strikes()

    model = os.environ.get("MODEL") or _DEFAULT_MODEL
    reasoning_effort = _resolve_reasoning_effort(model)

    return RunConfig(
        model=model,
        reasoning_effort=reasoning_effort,
        issue_source=issue_source,  # type: ignore[arg-type]
        max_iterations=int(args.max_iterations),
        max_nmt_strikes=max_nmt_strikes,
        deny_tools=frozenset(deny_tools),
        deny_skills=frozenset(deny_skills),
        verbosity=verbosity,
        render_reasoning=bool(args.render_reasoning),
        otel_enabled=_otel_enabled(),
        pricing_file=_resolve_pricing_file(),
    )


def main(argv: list[str] | None = None) -> int:
    """Entry point registered as the ``ralph-afk`` console script.

    Returns:
        Process exit code from :func:`ralph_afk.loop.run`.

    Raises:
        SystemExit: For early validation errors that we want to surface
            via argparse-style stderr handling (negative iterations,
            unknown ISSUE_SOURCE, malformed MAX_NMT_STRIKES).
    """
    parser = build_parser()
    args = parser.parse_args(argv)

    # Early git-root resolution so cwd-not-a-repo crashes with a clean
    # message before we pay the cost of importing the loop module
    # (which transitively pulls in the SDK and Rich).
    try:
        repo_root = resolve_repo_root()
    except RuntimeError as exc:
        print(f"ralph-afk: error: {exc}", file=sys.stderr)
        return 1

    # _build_config runs cheap env-var validations (ISSUE_SOURCE,
    # MAX_NMT_STRIKES, REASONING_EFFORT, …) that may raise SystemExit.
    # We want those argparse-style failures to surface before the
    # agent-skills preflight so an operator who set ``ISSUE_SOURCE=gitlab``
    # sees the unknown-source error rather than a "config missing" red
    # herring.
    config = _build_config(args)

    # Preflight: the AFK loop cannot safely run /setup-agent-skills itself
    # (the skill is interactive — it asks the operator to pick an issue
    # tracker, label vocabulary, and context-doc layout, then shows a draft
    # for confirmation before writing). Under ``copilot --yolo -p`` the
    # agent would have to invent those answers, baking the wrong defaults
    # into ``docs/agents/*.md``. So we refuse to start until a human has
    # run ``/setup-agent-skills`` in an interactive copilot session and
    # produced the config files. Detection signal: existence of
    # ``docs/agents/issue-tracker.md`` (the first of the triplet
    # ``/setup-agent-skills`` writes). Mirrors the bash runner's preflight
    # at ``ralph/sh-afk.sh``.
    issue_tracker_doc = repo_root / "docs" / "agents" / "issue-tracker.md"
    if not issue_tracker_doc.is_file():
        print(
            "ralph-afk: error: docs/agents/issue-tracker.md not found.\n"
            "  This repo has not been configured with /setup-agent-skills yet.\n"
            "  In an interactive 'copilot' session from the repo root, run:\n"
            "    > /setup-agent-skills\n"
            "  Then re-run ralph-afk. See docs/customization.md for details.",
            file=sys.stderr,
        )
        return 1

    # Import here so the SDK / Rich / pricing only load if we're
    # actually going to run. Keeps `ralph-afk --help` snappy.
    from ralph_afk import loop as _loop

    return asyncio.run(_loop.run(config))


if __name__ == "__main__":  # pragma: no cover - import-as-script convenience
    sys.exit(main())
