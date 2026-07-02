"""``ralph_afk.loop`` — async iteration driver for the AFK runner.

This is the orchestrator that ties every previously-merged module
together into a working ``ralph-afk`` invocation. It owns:

* The long-running :class:`copilot.CopilotClient` (one per ``ralph-afk``
  invocation; reused across iterations).
* The per-run :class:`~ralph_afk.persist.WritersBundle`
  (:class:`~ralph_afk.persist.EventLogWriter`,
  :class:`~ralph_afk.persist.RunSummaryWriter`, and the diagnostics
  logger).
* The per-run :class:`~ralph_afk.ui.RunSummary` and
  :class:`~ralph_afk.ui.Renderer`.
* The :class:`~ralph_afk.wrapper.NMTStrikeStateMachine`.
* The :class:`~ralph_afk.sources.IssueSource` — the per-invocation
  backend that discovers AFK-ready work and applies the
  source-specific completion backstop. Constructed from
  :attr:`RunConfig.issue_source` via the module-level
  :func:`_make_issue_source` factory so the loop body is unaware
  whether it is feeding off GitHub issues or local-markdown PRDs.

The per-iteration :class:`~ralph_afk.session.IterationSession` is opened
inside :func:`run` once per iteration.

Per-iteration sequence:

1. Cap check on ``max_iterations``.
2. Collect AFK-ready pool via :meth:`IssueSource.collect_afk_ready`.
3. Clean-exit on empty pool.
4. Build prompt: ``"Previous commits: <last5> Issues: <blocks> " + prompt_md``
   where each ``<block>`` is the source-rendered
   :attr:`AfkReadyItem.rendered_block`.
5. Capture ``pre_sha`` *immediately* before invoking the SDK — so a slow
   ``gh issue_view`` call before this point cannot affect the
   ``commits_between(pre_sha, head)`` accounting after.
6. Open :class:`~ralph_afk.session.IterationSession`,
   ``await session.send_and_wait(prompt, timeout=long)``.
7. ``head_sha = git.head_sha()``; ``commits = git.commits_between(pre, head)``.
8. Emit one ``wrapper.commit.recorded`` per new commit so the renderer
   increments the iteration's commit count.
9. **Completion backstop** via
   :meth:`IssueSource.handle_completions`. Each returned
   :class:`~ralph_afk.sources.Completion` produces one
   ``wrapper.auto_close`` event. The GitHub backend closes issues via
   ``gh issue close``; the PRDs backend returns an empty list (the
   agent owns ``git mv ... prds/<feat>/done/``).
10. **Runner Checkpoint** (ADR-0004) via :meth:`_maybe_checkpoint`: a dirty
    or untracked worktree is staged and captured in a single
    close-keyword-free ``wrapper.checkpoint.recorded`` commit attributed to
    the Active issue. Deliberately ordered *after* the agent-commit
    accounting (step 8) and *before* strike accounting (step 12), so the
    Checkpoint is excluded from both the Summary commit tally and the Strike
    machine. Non-fatal: a failure warns and the loop carries on.
11. **Auto-push** (ADR-0004) via :meth:`_maybe_push`: whenever the iteration
    produced new commits — agent commits (step 8) and/or the Checkpoint from
    step 10 — the current branch is pushed to its upstream
    (``wrapper.push.recorded`` on success) so the work reaches the remote.
    Non-fatal: a missing remote/upstream, an auth failure, or a
    non-fast-forward warns and the loop carries on, so a local-only repo
    completes normally.
12. NMT strike accounting: progress (``commits>0`` or ``auto_closures>0``)
    resets strikes; no-progress increments, possibly tripping the
    abort threshold. Checkpoints and pushes are *not* progress.
13. Emit ``wrapper.iteration.end`` (renderer closes snapshot panel) and
    persist :class:`~ralph_afk.persist.IterationCounters` from the
    closed snapshot.

Design notes:

* **Source-agnostic loop body.** The loop holds one
  :class:`IssueSource` and dispatches the three Protocol methods
  through it. Issue #11 lifts the PRDs backend; #10 introduced the
  GitHub backend. Adding a new backend (e.g. a remote API) means
  adding one ``IssueSource`` impl and one factory branch — the
  iteration body never changes.
* **Inter-module fan-out via the shared ``EventEmitter``.** Every
  wrapper-level event (``wrapper.run.start``, ``wrapper.iteration.start``,
  etc.) goes through :meth:`_emit`, a one-line delegator onto the shared
  :class:`~ralph_afk.emit.EventEmitter` (issue #45). The emitter:
  1. Constructs an envelope via :func:`ralph_afk.events.make_event`.
  2. Scrubs it **once**, then writes that scrubbed dict as the JSONL line via
     the event log writer — always-on and independent of which sinks are
     registered.
  3. Hands the *same scrubbed* dict to the :class:`~ralph_afk.sinks.SinkFanout`,
     which dispatches to every registered sink (issue #22) — so the sinks
     receive an already-scrubbed envelope (the sink contract), closing the
     pre-#45 scrub gap. For the non-interactive path the sole sink is the
     line-printer :class:`~ralph_afk.ui.renderer.Renderer`, which drives the
     Rich terminal output and RunSummary accumulator updates; the same fan-out
     is handed to each :class:`~ralph_afk.session.IterationSession` so SDK
     events and streaming deltas flow through the identical seam.
* **SDK + source failure containment.** ``send_and_wait`` failures are
  caught and treated as no-progress. Per-issue ``gh.issue_close`` failures are
  logged via the diagnostics logger inside the source impl and the
  loop continues — losing one closure is preferable to skipping the
  rest of the iteration's bookkeeping.
* **One ``CopilotClient`` per invocation.** Constructed lazily inside
  :func:`run` via the module-level :func:`_make_client` factory (which
  tests monkeypatch). Disconnected via ``await client.stop()`` in a
  ``finally`` block so even an early-loop crash releases the SDK's
  subprocess.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Coroutine, Iterable, Protocol

from copilot import CopilotClient
from rich.console import Console

from ralph_afk import events as events_module
from ralph_afk import gate as gate_module
from ralph_afk import gh as gh_module
from ralph_afk import git as git_module
from ralph_afk.config import RunConfig
from ralph_afk.emit import EventEmitter
from ralph_afk.persist import (
    IterationCounters,
    WritersBundle,
    create_writers,
)
from ralph_afk.pricing import Pricing, PricingError, load_pricing
from ralph_afk.session import IterationSession
from ralph_afk.sinks import EventSink, SinkFanout
from ralph_afk.sources import (
    AfkReadyItem,
    GitHubIssueSource,
    IssueSource,
    PrdsIssueSource,
)
from ralph_afk.telemetry import otel as telemetry
from ralph_afk.ui import Renderer, RunSummary, get_console
from ralph_afk.wrapper import (
    NMTStrikeStateMachine,
    checkpoint_message,
    extract_close_refs,
    filter_to_pool,
)

__all__ = ["run"]

# Default SDK ``send_and_wait`` timeout. AFK iterations can run for an
# hour or more; the SDK's default 60s is far too aggressive. Tunable
# via the ``RALPH_SEND_TIMEOUT_SECONDS`` env var so an operator can
# tighten it when debugging a wedged session.
_DEFAULT_SEND_TIMEOUT_SECONDS: float = 7200.0


def _build_telemetry_config() -> dict[str, Any] | None:
    """Construct the SDK telemetry config used by :func:`_make_client`.

    Factored out so the OTel telemetry seam is the **single switch** —
    the loop body and the production :func:`_make_client` do not contain
    ``if otel_enabled`` branches. When OTel is disabled,
    :func:`telemetry.build_sdk_telemetry_config` returns ``None`` and
    the SDK skips its telemetry env-var setup; when enabled, the SDK
    sets ``COPILOT_OTEL_ENABLED=true`` and forwards ``OTLP_ENDPOINT``
    when present.

    Returns:
        A :class:`~copilot.client.TelemetryConfig`-shaped dict (or
        ``None`` when OTel is disabled) passed verbatim to the
        ``telemetry`` keyword of :class:`copilot.CopilotClient`. All
        other client knobs (``connection``, ``log_level``, etc.) are
        left at SDK defaults; operators who need custom values can set
        the SDK's documented env vars (e.g. ``COPILOT_CLI_PATH``) — the
        SDK reads them during subprocess setup.
    """
    return telemetry.build_sdk_telemetry_config()


def _make_client() -> CopilotClient:
    """Construct the per-invocation :class:`CopilotClient`.

    Factored to its own module-level function so tests can monkeypatch
    it (``monkeypatch.setattr("ralph_afk.loop._make_client", ...)``) to
    return a fake. Production callers get the SDK's default
    construction with the telemetry config produced by
    :func:`_build_telemetry_config` — which is ``None`` (a no-op) when
    OTel is disabled.
    """
    return CopilotClient(telemetry=_build_telemetry_config())


def _make_git_client() -> git_module.SubprocessGitClient:
    """Construct the per-invocation root-bound git client.

    Factored to its own module-level function — mirroring :func:`_make_client`
    — so tests can monkeypatch it
    (``monkeypatch.setattr("ralph_afk.loop._make_git_client", ...)``) to inject
    a single fake object (``tests.fakes.FakeGitClient``) instead of
    monkeypatching a dozen ``git.*`` free functions. Production callers get a
    :class:`~ralph_afk.git.SubprocessGitClient` discovered from the process cwd:
    it resolves the repository root once (``git rev-parse --show-toplevel``) and
    binds every subsequent git call to it.

    Returns the concrete :class:`~ralph_afk.git.SubprocessGitClient` rather than
    the :class:`~ralph_afk.git.GitClient` protocol so :func:`run` can read
    ``.root`` (a construction detail, not part of the injected seam) for the
    writers/prompt/source setup before injecting the client into :class:`_Loop`.

    Raises:
        git.GitError: If ``git`` is not on PATH or the cwd is not inside a git
            repository. :func:`run` catches this and exits 1 cleanly.
    """
    return git_module.SubprocessGitClient.discover()


def _make_github_client() -> gh_module.SubprocessGitHubClient:
    """Construct the per-invocation GitHub client.

    Factored to its own module-level function — mirroring :func:`_make_git_client`
    — so tests can monkeypatch it
    (``monkeypatch.setattr("ralph_afk.loop._make_github_client", ...)``) to inject
    a single fake object (``tests.fakes.FakeGitHubClient``) instead of
    monkeypatching a handful of ``gh.*`` free functions. Production callers get a
    :class:`~ralph_afk.gh.SubprocessGitHubClient`.

    Unlike :func:`_make_git_client` there is **no cwd binding** — ``gh`` runs in
    the process cwd — so the client is stateless and takes no construction
    arguments. Only the ``github`` :class:`IssueSource` backend needs it; the
    ``prds`` backend has no GitHub dependency.
    """
    return gh_module.SubprocessGitHubClient()


def _make_gate_runner() -> gate_module.AgentsMdGateRunner:
    """Construct the per-invocation runner-side Integration gate (#60, ADR-0009).

    Factored to its own module-level function — mirroring :func:`_make_git_client`
    / :func:`_make_github_client` — so the later Wave/Lane orchestrator (#61) and
    Integration slices (#62/#63) inject it, and their tests monkeypatch it
    (``monkeypatch.setattr("ralph_afk.loop._make_gate_runner", ...)``) to a scripted
    ``tests.fakes.FakeGateRunner``. Production callers get a
    :class:`~ralph_afk.gate.AgentsMdGateRunner`, which runs a worktree's ``AGENTS.md``
    feedback loops as the load-bearing Integration gate.

    **Unused by the serial path.** Integration only exists in Parallel mode; the
    serial loop never gates from the runner side (the agent runs the loops inside its
    own session), so :func:`run` does not call this factory. It ships now purely as
    the injectable seam the parallel slices consume.
    """
    return gate_module.AgentsMdGateRunner()


def _make_issue_source(
    config: RunConfig,
    repo_root: Path,
    diag: logging.Logger,
    *,
    include_prs: bool = False,
) -> IssueSource:
    """Construct the per-invocation :class:`IssueSource`.

    Dispatches on :attr:`RunConfig.issue_source`. Factored to module
    scope so tests can monkeypatch it for end-to-end fakes. Returns a
    :class:`GitHubIssueSource` for ``"github"`` and a
    :class:`PrdsIssueSource` for ``"prds"``.

    Args:
        config: The frozen run configuration.
        repo_root: The resolved repository root (used by the PRDs backend).
        diag: Diagnostics logger handed to the source.
        include_prs: Whether the GitHub backend should also collect
            ``ready-for-agent`` PRs (see :func:`_resolve_include_prs`). The
            PRDs backend ignores it — local-markdown has no PRs.

    Raises:
        ValueError: If ``config.issue_source`` is neither known value.
            Should not happen in practice — :class:`RunConfig` rejects
            unknown values at construction time — but defence-in-depth
            in case the config grows new variants without a matching
            branch here.
    """
    if config.issue_source == "github":
        return GitHubIssueSource(
            diag, gh=_make_github_client(), include_prs=include_prs
        )
    if config.issue_source == "prds":
        return PrdsIssueSource(repo_root, diag)
    raise ValueError(
        f"unknown issue_source {config.issue_source!r}; expected "
        f"'github' or 'prds'"
    )


# Matches the PR-surface flag ``/setup-agent-skills`` writes into
# ``docs/agents/issue-tracker.md`` — e.g. ``**PRs as a request surface: yes.**``.
_RE_PR_SURFACE: re.Pattern[str] = re.compile(
    r"PRs as a request surface:\s*(yes|no)", re.IGNORECASE
)


def _resolve_include_prs(config: RunConfig, repo_root: Path) -> bool:
    """Resolve whether ``ready-for-agent`` PRs join the AFK-ready pool.

    Precedence:

    1. :attr:`RunConfig.include_prs` when not ``None`` — the ``INCLUDE_PRS``
       env override resolved by the CLI.
    2. Otherwise auto-detect from ``docs/agents/issue-tracker.md``: PRs are
       included only when it carries ``PRs as a request surface: yes`` (the
       exact flag ``/setup-agent-skills`` writes and ``/triage`` reads).
    3. ``False`` when the file is missing or the flag is absent / ``no`` — so
       PR support stays off unless a repo has explicitly opted in.

    Only ``issue_source == "github"`` can collect PRs; for ``"prds"`` the
    flag is meaningless (the factory hands it a PRDs source that ignores it).
    """
    if config.include_prs is not None:
        return config.include_prs
    tracker = repo_root / "docs" / "agents" / "issue-tracker.md"
    try:
        text = tracker.read_text(encoding="utf-8")
    except OSError:
        return False
    match = _RE_PR_SURFACE.search(text)
    if match is None:
        return False
    return match.group(1).lower() == "yes"


def _send_timeout_seconds() -> float:
    """Resolve the ``send_and_wait`` timeout from env or default."""
    raw = os.environ.get("RALPH_SEND_TIMEOUT_SECONDS")
    if raw is None or not raw.strip():
        return _DEFAULT_SEND_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return _DEFAULT_SEND_TIMEOUT_SECONDS
    if value <= 0:
        return _DEFAULT_SEND_TIMEOUT_SECONDS
    return value


def _read_prompt(repo_root: Path) -> str:
    """Load the runner's prompt file.

    Checks ``<repo>/ralph/prompt.md`` first, then ``<repo>/ralph/PROMPT.md``.
    The kit ships the uppercase variant; on case-insensitive filesystems
    (HFS+ default on macOS) either lookup succeeds, but case-sensitive
    filesystems (most Linux setups) need the explicit fallback.
    """
    candidates = (
        repo_root / "ralph" / "prompt.md",
        repo_root / "ralph" / "PROMPT.md",
    )
    for cand in candidates:
        if cand.exists():
            return cand.read_text(encoding="utf-8")
    raise FileNotFoundError(
        f"ralph prompt file not found under {repo_root}/ralph/ "
        f"(looked for prompt.md / PROMPT.md)"
    )


def _format_recent_commits(commits: Iterable[git_module.Commit]) -> str:
    """Render the last-5-commits block fed into the prompt prefix.

    One line per commit: sha, date, then the message body terminated by
    ``---``.
    """
    parts: list[str] = []
    for c in commits:
        parts.append(f"{c.sha}\n{c.date}\n{c.message}---")
    if not parts:
        return "No commits found"
    return "\n".join(parts)


def _lane_worktree_path(
    repo_root: Path, run_id: str, issue_number: int | str
) -> Path:
    """Compute a Lane's worktree path (ADR-0008: sibling, outside the repo).

    Lanes live in ``<repo_root>.worktrees/<run_id>/issue-<N>`` — a sibling
    directory of the repository, grouped by run so a run's worktrees are easy
    to find and reap, and one directory per issue so concurrent Lanes never
    share a tree. Kept *outside* the repo so a Lane's worktree is never itself
    picked up as untracked content by the main worktree's git status.
    """
    return (
        repo_root.parent
        / f"{repo_root.name}.worktrees"
        / run_id
        / f"issue-{issue_number}"
    )


_AUTO_RESOLUTION_MAX_ATTEMPTS = 3
"""K — the bound on auto-resolution attempts before serial fallback (#63)."""


_AUTO_RESOLUTION_FALLBACK_COMMENT = (
    "Automated Integration could not land this issue's parallel Lane after "
    f"{_AUTO_RESOLUTION_MAX_ATTEMPTS} auto-resolution attempts (merge conflict "
    "or feedback-loop failure). Base stayed green; falling back to a serial "
    "Iteration and keeping the Lane branch as a breadcrumb. -- copiloop"
)
"""The single automated breadcrumb left on an issue that fell back to serial."""


def _integration_worktree_path(
    repo_root: Path, run_id: str, issue_number: int | str
) -> Path:
    """Compute an auto-resolution integration worktree path (#63, ADR-0009).

    Integration recovery for a red / conflicting Lane runs its dedicated
    auto-resolution agent in ``<repo_root>.worktrees/<run_id>/integrate/issue-<N>``
    — a sibling of the Lane worktrees under the same per-run directory but in an
    ``integrate/`` subgroup, so it never collides with the Lane's own
    ``issue-<N>`` worktree. The leaf stays ``issue-<N>`` (matching
    :func:`_lane_worktree_path`) so the worktree still addresses exactly one
    issue.
    """
    return (
        repo_root.parent
        / f"{repo_root.name}.worktrees"
        / run_id
        / "integrate"
        / f"issue-{issue_number}"
    )


class _Loop:
    """Stateful orchestrator for one ``ralph-afk`` invocation.

    Bundles the long-lived per-run state — writers, summary, sink
    fan-out, SDK client, source, strike state machine — so the public
    :func:`run` function stays small and the per-iteration helper
    methods can read self instead of threading every value through
    their signatures.
    """

    def __init__(
        self,
        *,
        config: RunConfig,
        git: git_module.GitClient,
        prompt_text: str,
        pricing: Pricing,
        writers: WritersBundle,
        sinks: SinkFanout,
        summary: RunSummary,
        client: CopilotClient,
        source: IssueSource,
        diag: logging.Logger,
        include_prs: bool = False,
    ) -> None:
        self._config = config
        self._git = git
        self._prompt_text = prompt_text
        self._pricing = pricing
        self._writers = writers
        self._sinks = sinks
        self._summary = summary
        self._client = client
        self._source = source
        self._diag = diag
        self._include_prs = include_prs
        # Base branch to restore to after a PR iteration (captured in
        # ``drive`` only when PRs are in scope). ``None`` = unknown / detached
        # HEAD, which disables the defensive restore.
        self._base_branch: str | None = None
        self._strike_machine = NMTStrikeStateMachine(
            max_strikes=config.max_nmt_strikes
        )
        # The one scrub-and-fan-out seam (issue #43): compose -> scrub once ->
        # write the replay JSONL + fan out to the sinks. Built here so ``_emit``
        # is a one-line delegator and the sinks receive the *scrubbed* envelope
        # by construction — #45 closed the loop's scrub gap (the pre-#45 inline
        # copy fanned the raw envelope out to the sinks). ``diag=self._diag``
        # preserves the loop's warn-and-continue policy on a write / sink failure.
        self._emitter = EventEmitter(
            run_id=self._writers.run_id,
            event_log=self._writers.event_log,
            sinks=self._sinks,
            diag=self._diag,
        )

    # -- event fan-out ------------------------------------------------------

    def _emit(
        self,
        event_type: str,
        *,
        iter_num: int | None,
        **payload: Any,
    ) -> dict[str, Any]:
        """Compose, scrub, persist, then fan out one wrapper-level event.

        Delegates to the shared :class:`~ralph_afk.emit.EventEmitter` (issue
        #45): it composes the envelope via :func:`ralph_afk.events.make_event`,
        scrubs it **once**, writes that scrubbed dict as the replay JSONL line
        (always-on, independent of the sink list), and fans the *same scrubbed*
        dict out to the :class:`~ralph_afk.sinks.SinkFanout` — so the on-screen
        sinks receive an already-scrubbed envelope (the sink contract), not the
        raw one the pre-#45 inline copy leaked. The write and render are each
        individually guarded; on failure the emitter warns via the loop's
        ``diag`` (warn-and-continue). Returns the composed **pre-scrub** envelope
        so callers can still read the SHA / subject off their own events.
        """
        return self._emitter.emit(event_type, iter_num=iter_num, **payload)

    # -- iteration body ----------------------------------------------------

    async def _run_one_iteration(
        self, iter_num: int
    ) -> tuple[str, int, int]:
        """Run a single AFK iteration.

        Returns:
            ``(outcome, commits_in_iter, auto_closures_in_iter)``.

            ``outcome`` is one of:

            * ``"continue"`` — iteration completed, loop should keep going.
            * ``"empty_pool"`` — AFK-ready pool was empty; clean exit 0.
            * ``"aborted"`` — NMT strike machine tripped; abort exit 1.

        OTel span tree: opens ``ralph_afk.iteration`` for the entire body,
        with three children — ``ralph_afk.collect_issues`` around the
        pool discovery, ``ralph_afk.session`` around the SDK session
        lifecycle, and ``ralph_afk.enforce_closures`` around the
        source-specific completion backstop. The empty-pool path emits
        only the partial subtree (no ``session`` / ``enforce_closures``
        spans); see
        ``tests/test_iteration_end_to_end.py::test_loop_emits_otel_span_tree_when_enabled``.
        """
        with telemetry.span(
            "ralph_afk.iteration", iter=iter_num
        ) as iteration_span:
            self._emit(
                events_module.WRAPPER_ITERATION_START,
                iter_num=iter_num,
            )

            # 1) PR branch hygiene. A prior PR iteration may have run
            #     `gh pr checkout <N>` and left HEAD on the PR branch. The
            #     worktree is clean (the guard above just passed), so restore
            #     the captured base branch — otherwise this iteration's
            #     commits and `commits_between` accounting would land on the
            #     PR branch. Gated on `include_prs` so the default (issues-only)
            #     path is byte-for-byte unchanged and never touches branches.
            if self._include_prs and self._base_branch is not None:
                try:
                    on_branch = self._git.current_branch()
                except git_module.GitError as exc:
                    self._diag.warning(
                        "current_branch check failed: %s; skipping base "
                        "restore",
                        exc,
                    )
                    on_branch = None
                if on_branch is not None and on_branch != self._base_branch:
                    try:
                        self._git.switch(self._base_branch)
                        self._diag.info(
                            "restored base branch %s (iteration started on %s)",
                            self._base_branch,
                            on_branch,
                        )
                    except git_module.GitError as exc:
                        self._diag.warning(
                            "could not restore base branch %s: %s; "
                            "continuing on %s",
                            self._base_branch,
                            exc,
                            on_branch,
                        )

            # 2) Collect AFK-ready pool via the source.
            with telemetry.span("ralph_afk.collect_issues"):
                pool = self._source.collect_afk_ready()
            pool_refs: list[int | str] = [item.ref for item in pool]
            # Late-bind the iteration span's `issue` / `issues` attributes
            # now that we know the pool. `set_attribute` is no-op-safe so
            # this works whether OTel is enabled or not.
            if pool_refs:
                iteration_span.set_attribute("issue", pool_refs[0])
                iteration_span.set_attribute("issues", pool_refs)
            self._emit(
                events_module.WRAPPER_AFK_READY_COLLECTED,
                iter_num=iter_num,
                issues=pool_refs,
            )
            if not pool:
                # Close the iteration cleanly so the snapshot lifecycle is
                # consistent even on the empty-pool path.
                self._emit(
                    events_module.WRAPPER_ITERATION_END,
                    iter_num=iter_num,
                )
                self._record_counters(iter_num)
                return ("empty_pool", 0, 0)

            # 3) Build prompt (last-5 commits + AFK-ready item blocks + prompt body).
            try:
                recent = self._git.recent_commits(5)
            except git_module.GitError as exc:
                self._diag.warning("recent_commits failed: %s; using empty prefix", exc)
                recent = []
            commits_block = _format_recent_commits(recent)
            issues_block = "\n\n".join(item.rendered_block for item in pool)
            prompt = (
                f"Previous commits: {commits_block} "
                f"Issues: {issues_block} {self._prompt_text}"
            )

            # 4) Capture pre_sha *after* the slow source-collection step so
            #    any commit that landed while we were enriching the pool
            #    isn't incorrectly attributed to this iteration.
            try:
                pre_sha = self._git.head_sha()
            except git_module.GitError as exc:
                self._diag.error("git head_sha failed: %s; aborting iteration", exc)
                self._emit(events_module.WRAPPER_ITERATION_END, iter_num=iter_num)
                self._record_counters(iter_num)
                return ("continue", 0, 0)

            # 5) Run the SDK session.
            send_timeout = _send_timeout_seconds()
            with telemetry.span("ralph_afk.session"):
                try:
                    async with IterationSession(
                        self._client,
                        config=self._config,
                        event_log=self._writers.event_log,
                        sinks=self._sinks,
                        run_id=self._writers.run_id,
                        iter_num=iter_num,
                        model=self._config.model,
                        reasoning_effort=self._config.reasoning_effort,
                    ) as sdk_session:
                        try:
                            await sdk_session.send_and_wait(
                                prompt, timeout=send_timeout
                            )
                        except asyncio.TimeoutError:
                            self._diag.warning(
                                "SDK send_and_wait timed out after %ss; "
                                "treating iteration as no-progress",
                                send_timeout,
                            )
                        except Exception as exc:
                            # Treat any copilot failure as no-progress;
                            # bookkeeping below still runs.
                            self._diag.warning(
                                "SDK send_and_wait raised %s: %s; "
                                "treating iteration as no-progress",
                                type(exc).__name__, exc,
                            )
                except Exception as exc:
                    self._diag.error(
                        "IterationSession lifecycle failed: %s: %s; iteration aborted",
                        type(exc).__name__, exc,
                    )

            # 6) Post-iteration accounting.
            try:
                head = self._git.head_sha()
            except git_module.GitError as exc:
                self._diag.warning(
                    "post-iteration git head_sha failed: %s; "
                    "skipping commit accounting", exc,
                )
                head = pre_sha
            try:
                new_commits = self._git.commits_between(pre_sha, head)
            except git_module.GitError as exc:
                self._diag.warning(
                    "post-iteration commits_between failed: %s; "
                    "skipping commit accounting", exc,
                )
                new_commits = []

            for c in new_commits:
                self._emit(
                    events_module.WRAPPER_COMMIT_RECORDED,
                    iter_num=iter_num,
                    sha=c.sha,
                    subject=c.subject,
                    date=c.date,
                )

            # 7) Completion backstop — source-specific. The GitHub backend
            #    closes the issue via gh; the PRDs backend always returns
            #    [] (the agent owns the `git mv ... done/` step).
            with telemetry.span("ralph_afk.enforce_closures"):
                completions = self._handle_completions_safely(pool, new_commits)
            for completion in completions:
                if getattr(completion, "kind", "issue") == "pr":
                    # A PR advance (head SHA moved). Different event so the
                    # renderer says "advanced PR #N" rather than
                    # "auto-closed #N"; still counted toward progress below.
                    self._emit(
                        events_module.WRAPPER_PR_ADVANCED,
                        iter_num=iter_num,
                        pr=completion.ref,
                        sha=completion.sha,
                        shas=list(completion.shas),
                    )
                else:
                    self._emit(
                        events_module.WRAPPER_AUTO_CLOSE,
                        iter_num=iter_num,
                        issue=completion.ref,
                        sha=completion.sha,
                        shas=list(completion.shas),
                    )
            auto_closures = len(completions)

            # 8) Runner Checkpoint (ADR-0004). Capture any dirty / untracked
            #    work-in-progress in a single close-keyword-free Checkpoint
            #    commit so the next iteration starts from a clean tree and no
            #    work is ever lost. Deliberately AFTER the agent-commit
            #    accounting above (step 6) and BEFORE the Strike machine below,
            #    so the Checkpoint is structurally excluded from both: it never
            #    counts as a commit in the Summary (it emits
            #    ``wrapper.checkpoint.recorded``, not ``wrapper.commit.recorded``)
            #    and it never resets a Strike. Non-fatal — a failure warns and
            #    the loop carries on (a local-only repo still completes).
            checkpoint_sha = self._maybe_checkpoint(
                iter_num, pool, completions, new_commits
            )

            # 9) Auto-push (ADR-0004, second half). Whenever this iteration
            #    produced new commits — agent commits (step 6) and/or the
            #    Checkpoint just made (step 8) — push the current branch to its
            #    upstream so the work reaches the remote instead of piling up
            #    locally. Non-fatal: a missing remote/upstream, an auth failure,
            #    or a non-fast-forward warns and the loop carries on. Like the
            #    Checkpoint, a push is NOT Strike progress (it creates no commit).
            self._maybe_push(iter_num, new_commits, checkpoint_sha)

            # 10) Strike state machine + emit appropriate events.
            outcome = self._strike_machine.tick(
                commits_in_iter=len(new_commits),
                auto_closures_in_iter=auto_closures,
            )
            if outcome == "aborted" or (
                len(new_commits) == 0 and auto_closures == 0
            ):
                # Either we just hit the strike threshold OR this iteration
                # had no progress (a single strike). Either way emit the
                # wrapper.strike event so the renderer + persist see it.
                self._emit(
                    events_module.WRAPPER_STRIKE,
                    iter_num=iter_num,
                    strikes=self._strike_machine.strikes,
                    max_strikes=self._config.max_nmt_strikes,
                    outcome=("abort" if outcome == "aborted" else "warn"),
                )

            # 11) Close the iteration snapshot, persist counters.
            self._emit(events_module.WRAPPER_ITERATION_END, iter_num=iter_num)
            self._record_counters(iter_num)

            if outcome == "aborted":
                return ("aborted", len(new_commits), auto_closures)
            return ("continue", len(new_commits), auto_closures)

    def _maybe_checkpoint(
        self,
        iter_num: int,
        pool: list[AfkReadyItem],
        completions: list[Any],
        new_commits: list[git_module.Commit],
    ) -> str | None:
        """Capture a dirty / untracked worktree in one Checkpoint commit.

        The runner-authored safety net of ADR-0004. If the worktree has any
        uncommitted tracked change (:func:`git.is_dirty`) or any untracked,
        non-ignored file (:func:`git.has_untracked`), stage everything
        (``git add -A``, honouring ``.gitignore``) and make a single
        **close-keyword-free** Checkpoint commit attributed to the Active issue
        (so the auto-close backstop never fires on it), then emit
        ``wrapper.checkpoint.recorded``.

        Every git interaction is wrapped: a missing remote, an empty index, or
        any other :exc:`git.GitError` warns and returns ``None`` rather than
        aborting the run, so a clean tree, a non-repo, and a local-only repo all
        complete normally.

        Returns:
            The new Checkpoint commit SHA, or ``None`` when nothing was
            captured (clean tree) or the Checkpoint could not be made.
        """
        try:
            dirty = self._git.is_dirty()
            untracked = self._git.has_untracked()
        except git_module.GitError as exc:
            self._diag.warning(
                "checkpoint dirty-check failed: %s; skipping checkpoint", exc
            )
            return None
        if not (dirty or untracked):
            return None

        active_ref = self._infer_active_ref(pool, completions, new_commits)
        try:
            self._git.add_all()
            sha = self._git.commit(checkpoint_message(active_ref))
        except git_module.GitError as exc:
            self._diag.warning(
                "checkpoint commit failed: %s; continuing without it", exc
            )
            return None

        self._emit(
            events_module.WRAPPER_CHECKPOINT_RECORDED,
            iter_num=iter_num,
            sha=sha,
            issue=active_ref,
        )
        self._diag.info(
            "recorded checkpoint %s (attributed to %s)", sha, active_ref
        )
        return sha

    def _infer_active_ref(
        self,
        pool: list[AfkReadyItem],
        completions: list[Any],
        new_commits: list[git_module.Commit],
    ) -> int | str | None:
        """Best-effort guess of the iteration's Active issue for a Checkpoint.

        The non-interactive loop has no ``<working issue=N>`` marker tap (that
        lives in the interactive state), so it infers attribution from what it
        does know, in priority order:

        1. The first completion's ref (an issue we just auto-closed / a PR we
           advanced) — the strongest signal of what was worked.
        2. The first AFK-ready pool member referenced by a closing keyword in
           this iteration's agent commits — the agent named it even if the
           closure didn't fire.
        3. A single-member pool — the only candidate.

        Falls back to ``None`` (an *unattributed* Checkpoint) when the pool has
        several issues and nothing above disambiguates them.
        """
        if completions:
            return completions[0].ref
        pool_ints = {item.ref for item in pool if isinstance(item.ref, int)}
        if pool_ints:
            joined = "\n".join(c.message for c in new_commits)
            refs = filter_to_pool(extract_close_refs(joined), pool_ints)
            if refs:
                return refs[0]
        if len(pool) == 1:
            return pool[0].ref
        return None

    def _maybe_push(
        self,
        iter_num: int,
        new_commits: list[git_module.Commit],
        checkpoint_sha: str | None,
    ) -> bool:
        """Push the current branch to its upstream after an iteration's new commits.

        The remote half of ADR-0004's durability net. Whenever this iteration
        produced new commits — agent commits (``new_commits``) and/or the runner
        Checkpoint just authored (``checkpoint_sha``) — :func:`git.push` sends
        the current branch to its configured upstream so the work reaches the
        remote instead of accumulating locally. An iteration that produced
        neither (a clean tree with no agent commit, or a pure PR advance the
        agent pushed itself) skips the push entirely.

        Non-fatal by construction: a missing upstream, a missing/unreachable
        remote, an auth failure, or a non-fast-forward rejection raises
        :exc:`git.GitError`, which is caught and warned — a local-only repo
        completes normally. A successful push emits ``wrapper.push.recorded``;
        a failure emits nothing (it only warns), mirroring the failed-Checkpoint
        path so the JSONL records pushes that actually landed.

        Returns:
            ``True`` if a push was attempted and succeeded; ``False`` if there
            was nothing to push or the push failed non-fatally.
        """
        if not new_commits and checkpoint_sha is None:
            return False
        try:
            self._git.push()
        except git_module.GitError as exc:
            self._diag.warning(
                "auto-push failed: %s; continuing (work stays local)", exc
            )
            return False
        self._emit(events_module.WRAPPER_PUSH_RECORDED, iter_num=iter_num)
        self._diag.info("auto-pushed current branch after new commits")
        return True

    def _handle_completions_safely(
        self,
        pool: list[AfkReadyItem],
        new_commits: list[git_module.Commit],
    ) -> list[Any]:
        """Call ``source.handle_completions`` with crash containment.

        A source-level crash inside ``handle_completions`` must not
        abort the iteration — the commit accounting and strike
        bookkeeping still need to run. Returns an empty list on
        failure (logged at WARNING via the diagnostics logger).
        """
        try:
            return list(
                self._source.handle_completions(
                    pool=pool, new_commits=new_commits
                )
            )
        except Exception as exc:  # pragma: no cover - defensive
            self._diag.warning(
                "source.handle_completions raised %s: %s; "
                "continuing iteration with zero completions",
                type(exc).__name__, exc,
            )
            return []

    def _record_counters(self, iter_num: int) -> None:
        """Persist the iteration's counter row.

        Reads the last completed snapshot from the renderer's
        :class:`RunSummary` (closed on :data:`WRAPPER_ITERATION_END`)
        and translates it to an :class:`~ralph_afk.persist.IterationCounters`
        via the UI's :meth:`IterationSnapshot.to_counters_kwargs` seam.
        """
        completed = self._summary.completed
        if not completed:
            return
        # Find the snapshot for this iter_num (most-recent match wins).
        snap = None
        for s in reversed(completed):
            if s.iter_num == iter_num:
                snap = s
                break
        if snap is None:
            return
        kwargs = snap.to_counters_kwargs(pricing=self._pricing)
        # Carry the strike-machine's current count into the persisted row
        # so the run-summary JSON shows what the wrapper actually saw.
        kwargs["strikes"] = self._strike_machine.strikes
        try:
            self._writers.run_summary.record(IterationCounters(**kwargs))
        except Exception as exc:  # pragma: no cover - defensive
            self._diag.warning(
                "RunSummaryWriter.record failed for iter %d: %s",
                iter_num, exc,
            )

    # -- public driver -----------------------------------------------------

    async def drive(self) -> int:
        """Drive the iteration loop to its terminal outcome."""
        # Preflight via the source — GitHub validates gh + repo; PRDs
        # is a no-op (returns None) so an empty / missing prds/ dir is
        # not a preflight failure, just an empty pool.
        rc = self._source.preflight()
        if rc is not None:
            return rc

        # Capture the base branch once, before any iteration can run
        # `gh pr checkout`, so PR iterations can return to it (see the
        # branch-hygiene step in `_run_one_iteration`). Only when PRs are in
        # scope; a detached HEAD or git failure leaves it None, which simply
        # disables the defensive restore.
        if self._include_prs:
            try:
                self._base_branch = self._git.current_branch()
            except git_module.GitError as exc:
                self._diag.warning(
                    "could not determine base branch for PR restore: %s", exc
                )
                self._base_branch = None

        self._emit(
            events_module.WRAPPER_RUN_START,
            iter_num=None,
            issue_source=self._config.issue_source,
            max_iterations=self._config.max_iterations,
            max_nmt_strikes=self._config.max_nmt_strikes,
        )

        exit_code = 0
        outcome_label = "iteration_cap"
        iter_num = 0
        try:
            try:
                while True:
                    iter_num += 1
                    if (
                        self._config.max_iterations != 0
                        and iter_num > self._config.max_iterations
                    ):
                        outcome_label = "iteration_cap"
                        break

                    outcome, _commits, _closures = await self._run_one_iteration(
                        iter_num
                    )
                    if outcome == "empty_pool":
                        outcome_label = "empty_pool"
                        exit_code = 0
                        break
                    if outcome == "aborted":
                        outcome_label = "stuck"
                        exit_code = 1
                        break
            except Exception as exc:
                # An unhandled crash inside an iteration MUST surface in
                # the wrapper.run.end envelope so a replay-side reader
                # doesn't mistake the run for a clean cap-out. Re-raise
                # so the outer ``run()`` can log + return 1.
                outcome_label = "crashed"
                exit_code = 1
                self._diag.error(
                    "ralph_afk iteration %d crashed: %s: %s",
                    iter_num, type(exc).__name__, exc,
                )
                raise
        finally:
            # Final wrapper.run.end always emits — even on early break or crash.
            try:
                self._emit(
                    events_module.WRAPPER_RUN_END,
                    iter_num=None,
                    outcome=outcome_label,
                    iterations_run=(
                        iter_num
                        if outcome_label != "iteration_cap"
                        else iter_num - 1
                    ),
                )
            except Exception as exc:  # pragma: no cover - defensive
                self._diag.warning("wrapper.run.end emit failed: %s", exc)
        return exit_code


@dataclass
class _Lane:
    """One Parallel-mode Lane: an issue pinned to its own worktree + branch.

    Bundles the per-Lane state the Wave orchestrator threads from worktree
    creation, through the concurrent session, to post-barrier accounting: the
    :class:`~ralph_afk.sources.AfkReadyItem` it works, the branch cut for it,
    the worktree path, the root-bound child :class:`~ralph_afk.git.GitClient`
    addressing that worktree, and the pre-session head SHA captured at creation
    for per-Lane commit accounting.
    """

    item: AfkReadyItem
    branch: str
    path: Path
    git: git_module.GitClient
    pre_sha: str | None = None


def _lane_sort_key(lane: _Lane) -> tuple[int, int, str]:
    """Ascending, deterministic Integration order for a Wave's Lanes.

    Lanes are only ever created for **integer** issue numbers (Wave eligibility
    requires an ``int`` ref), so this orders by that number. The leading
    discriminator keeps the key total-orderable even if a non-int ref ever
    slipped through — ints first (by value), then any string refs
    (lexicographically) — so the sort can never raise on a mixed key.
    """
    ref = lane.item.ref
    if isinstance(ref, int):
        return (0, ref, "")
    return (1, 0, str(ref))


class _ParallelLoop:
    """Opt-in Parallel-mode Wave/Lane orchestrator (#61, ADR-0008).

    The concurrent-execution core: each *round* is either a **Wave** — up to
    ``config.parallel`` **Lanes**, each an agent working one ``parallel-safe``
    issue in its own git worktree + branch, run concurrently on one long-lived
    client via :func:`asyncio.gather` and pinned to its worktree via the SDK's
    per-session ``working_directory`` — or, when fewer than two eligible issues
    are available, a single serial **Iteration** fallback (the proven path), so
    opting into Parallel mode never strands eligible work.

    Eligibility is a **human assertion**, never inferred: only pool items
    carrying ``parallel-safe`` (alongside ``ready-for-agent``) may become a
    Lane. Commit accounting is per-Lane (each branch's own pre/post SHA) and a
    per-worktree Checkpoint (ADR-0004) runs on each Lane branch; at the Wave
    barrier the worktrees are torn down (branches kept as breadcrumbs).

    **Integration (#62, ADR-0009).** A Wave ends by landing its green Lanes on
    base: for each finished Lane branch in ascending issue-number order, merge
    into base, re-run the feedback loops from the runner side via the injected
    :class:`~ralph_afk.gate.GateRunner` as the load-bearing gate, and on green
    close the issue (the same runner-driven closure as serial mode) + delete the
    integrated branch — a successful Integration is the round's Strike progress
    signal. This is the **happy path only**; conflict / red-gate handling
    (revert + auto-resolution + serial fallback) is the next slice (#63), so a
    failed Lane is skipped here and its branch kept as a breadcrumb.

    Composes a serial :class:`_Loop` (``self._serial``) both for the serial
    fallback rounds and to share ONE Strike machine, event emitter, summary,
    and Checkpoint policy — so a Wave round and a serial round tick the same
    Strike machine and write one consistent event / counter stream. The serial
    path is unaffected: :func:`run` only builds a ``_ParallelLoop`` when
    ``config.parallel > 1``.
    """

    def __init__(
        self,
        *,
        config: RunConfig,
        git: git_module.GitClient,
        prompt_text: str,
        pricing: Pricing,
        writers: WritersBundle,
        sinks: SinkFanout,
        summary: RunSummary,
        client: CopilotClient,
        source: IssueSource,
        diag: logging.Logger,
        gate_runner: gate_module.GateRunner,
        include_prs: bool = False,
    ) -> None:
        self._config = config
        self._git = git
        self._prompt_text = prompt_text
        self._writers = writers
        self._sinks = sinks
        self._summary = summary
        self._client = client
        self._source = source
        self._diag = diag
        # Injected runner-side Integration gate (#60, ADR-0009): re-runs the
        # feedback loops from the runner side as the load-bearing gate when
        # landing a Lane branch on base. Consumed by `_integrate_wave` (#62);
        # the conflict / red-gate recovery paths are the next slice (#63).
        self._gate_runner = gate_runner
        self._run_id = writers.run_id
        self._repo_root = git.root
        # Issues already dispatched to a Lane this run. A Lane branch name is
        # derived from the issue number, so re-dispatching the same issue would
        # collide on ``git worktree add -b``; tracking worked refs keeps a Wave
        # idempotent across rounds — a still-open issue falls through to a
        # serial Iteration, never a second Lane.
        self._worked: set[int | str] = set()
        # Compose a serial ``_Loop`` for fallback rounds AND to share its Strike
        # machine / event emitter / summary counters / Checkpoint policy, so a
        # Wave round and a serial fallback round tick ONE Strike machine and
        # write ONE consistent event + counter stream.
        self._serial = _Loop(
            config=config,
            git=git,
            prompt_text=prompt_text,
            pricing=pricing,
            writers=writers,
            sinks=sinks,
            summary=summary,
            client=client,
            source=source,
            diag=diag,
            include_prs=include_prs,
        )

    async def drive(self) -> int:
        """Drive the Parallel-mode round loop to its terminal outcome.

        Mirrors :meth:`_Loop.drive` — same preflight, ``wrapper.run.start`` /
        ``wrapper.run.end`` envelope, ``max_iterations`` cap, and exit-code
        contract — but each round is either a **Wave** (>= 2 eligible
        ``parallel-safe`` issues) or a single serial **Iteration** fallback. The
        shared Strike machine ticks exactly once per round.
        """
        rc = self._source.preflight()
        if rc is not None:
            return rc

        self._serial._emit(
            events_module.WRAPPER_RUN_START,
            iter_num=None,
            issue_source=self._config.issue_source,
            max_iterations=self._config.max_iterations,
            max_nmt_strikes=self._config.max_nmt_strikes,
        )

        exit_code = 0
        outcome_label = "iteration_cap"
        iter_num = 0
        try:
            try:
                while True:
                    iter_num += 1
                    if (
                        self._config.max_iterations != 0
                        and iter_num > self._config.max_iterations
                    ):
                        outcome_label = "iteration_cap"
                        break

                    outcome, _commits, _closures = await self._run_one_round(
                        iter_num
                    )
                    if outcome == "empty_pool":
                        outcome_label = "empty_pool"
                        exit_code = 0
                        break
                    if outcome == "aborted":
                        outcome_label = "stuck"
                        exit_code = 1
                        break
            except Exception as exc:
                outcome_label = "crashed"
                exit_code = 1
                self._diag.error(
                    "ralph_afk parallel round %d crashed: %s: %s",
                    iter_num, type(exc).__name__, exc,
                )
                raise
        finally:
            try:
                self._serial._emit(
                    events_module.WRAPPER_RUN_END,
                    iter_num=None,
                    outcome=outcome_label,
                    iterations_run=(
                        iter_num
                        if outcome_label != "iteration_cap"
                        else iter_num - 1
                    ),
                )
            except Exception as exc:  # pragma: no cover - defensive
                self._diag.warning("wrapper.run.end emit failed: %s", exc)
        return exit_code

    async def _run_one_round(self, iter_num: int) -> tuple[str, int, int]:
        """Run one round: a Wave when eligible, else a serial Iteration.

        Peeks the AFK-ready pool and counts eligible ``parallel-safe`` issues
        (int-ref, carrying the label, not already worked this run). Two or more
        dispatches a Wave; otherwise the round is a single serial Iteration —
        the proven path that also handles the empty pool, a lone issue, and any
        plain ``ready-for-agent`` work — so Parallel mode drains everything.
        """
        pool = self._collect_pool_safely()
        eligible = [
            item
            for item in pool
            if isinstance(item.ref, int)
            and "parallel-safe" in item.labels
            and item.ref not in self._worked
        ]
        if len(eligible) >= 2:
            return await self._run_one_wave(iter_num, eligible)
        # < 2 fresh eligible parallel-safe issues: a normal serial Iteration.
        # It re-collects the pool itself (authoritative) and owns the
        # empty-pool / single-issue exit semantics.
        return await self._serial._run_one_iteration(iter_num)

    def _collect_pool_safely(self) -> list[AfkReadyItem]:
        """Peek the AFK-ready pool for the Wave-vs-serial decision.

        A source failure degrades to an empty pool (the serial fallback then
        re-collects and owns the real error path), so a transient collection
        error never crashes the round loop.
        """
        try:
            return list(self._source.collect_afk_ready())
        except Exception as exc:
            self._diag.warning(
                "parallel pool peek failed: %s: %s; treating as empty",
                type(exc).__name__, exc,
            )
            return []

    async def _run_one_wave(
        self, iter_num: int, eligible: list[AfkReadyItem]
    ) -> tuple[str, int, int]:
        """Dispatch a Wave of up to N concurrent, isolated Lanes.

        Creates a worktree + branch per Lane (cut from base, in a sibling
        directory outside the repo), runs N :class:`IterationSession`s
        concurrently on one client via :func:`asyncio.gather` — each pinned to
        its Lane's worktree via ``working_directory`` — then, at the Wave
        barrier, does per-Lane commit accounting + a per-worktree Checkpoint and
        tears the worktrees down (keeping branches as breadcrumbs), then runs
        :meth:`_integrate_wave` to land the green Lanes on base and close their
        issues in ascending issue-number order (#62).
        """
        self._serial._emit(
            events_module.WRAPPER_ITERATION_START, iter_num=iter_num
        )
        lane_items = eligible[: self._config.parallel]
        self._serial._emit(
            events_module.WRAPPER_AFK_READY_COLLECTED,
            iter_num=iter_num,
            issues=[item.ref for item in lane_items],
        )

        base = self._resolve_base_ref()
        try:
            recent = self._git.recent_commits(5)
        except git_module.GitError as exc:
            self._diag.warning(
                "recent_commits failed: %s; using empty prefix", exc
            )
            recent = []
        commits_block = _format_recent_commits(recent)

        # 1) Create each Lane's worktree + branch (before the barrier). A failed
        #    add (e.g. a leftover branch) drops just that Lane and leaves its
        #    issue un-worked so it can fall through to a later round.
        lanes: list[_Lane] = []
        for item in lane_items:
            ref = item.ref
            if not isinstance(ref, int):
                # Eligibility (see `_run_one_round`) already guarantees an
                # int issue number; this narrows the type for the branch-name
                # helper and defends against a future non-int ref slipping in.
                continue
            branch = git_module.lane_branch_name(self._run_id, ref)
            path = _lane_worktree_path(self._repo_root, self._run_id, ref)
            try:
                wt_git = self._git.add_worktree(path, branch=branch, base=base)
            except git_module.GitError as exc:
                self._diag.warning(
                    "worktree add for issue #%s failed: %s; skipping lane",
                    ref, exc,
                )
                continue
            lane = _Lane(item=item, branch=branch, path=path, git=wt_git)
            try:
                lane.pre_sha = wt_git.head_sha()
            except git_module.GitError as exc:
                self._diag.warning(
                    "lane #%s pre head_sha failed: %s", ref, exc
                )
            lanes.append(lane)
            self._worked.add(ref)

        # 2) Dispatch the Lanes concurrently, joined at the Wave barrier. One
        #    long-lived client hosts N sessions, each pinned to its worktree.
        if lanes:
            await asyncio.gather(
                *(
                    self._run_lane_session(iter_num, lane, commits_block)
                    for lane in lanes
                )
            )

        # 3) Per-Lane accounting + per-worktree Checkpoint (sequential and
        #    deterministic), then tear the worktree down. Run after the barrier
        #    so the concurrent phase is pure session work. Branches survive the
        #    teardown (kept as breadcrumbs) so Integration can land them next.
        total_commits = 0
        for lane in lanes:
            total_commits += self._account_lane(iter_num, lane)
            try:
                self._git.remove_worktree(lane.path, force=True)
            except git_module.GitError as exc:
                self._diag.warning(
                    "worktree remove for %s failed: %s", lane.path, exc
                )

        # 3.5) Integration (#62 + #63, ADR-0009): serialized, deterministic land
        #     of the green Lane branches on base + issue closure, with revert +
        #     bounded auto-resolution + serial fallback for red / conflicting
        #     Lanes so base stays green. Operates on branch names in the main
        #     worktree, so it runs after the Lane worktrees are gone and before
        #     the round's single Strike tick.
        integration_successes = await self._integrate_wave(iter_num, lanes)

        # 4) Strike tick — once per round. A successful Integration is the round's
        #    progress signal (ADR-0009): Lane commits count only once they LAND on
        #    base, so a round that lands nothing adds a strike even if the agents
        #    committed inside their worktrees.
        outcome = self._tick_round(
            iter_num, commits=0, closures=integration_successes
        )

        self._serial._emit(
            events_module.WRAPPER_ITERATION_END, iter_num=iter_num
        )
        self._serial._record_counters(iter_num)

        if outcome == "aborted":
            return ("aborted", total_commits, integration_successes)
        return ("continue", total_commits, integration_successes)

    async def _run_lane_session(
        self, iter_num: int, lane: _Lane, commits_block: str
    ) -> None:
        """Run one Lane's SDK session, pinned to its worktree.

        The only concurrent phase of a Wave. Bulletproof by construction — a
        timeout, a send failure, or a session-lifecycle error is logged and
        swallowed so one Lane can never abort the :func:`asyncio.gather` join or
        the barrier teardown; the post-barrier accounting then records that Lane
        as no-progress.
        """
        prompt = (
            f"Previous commits: {commits_block} "
            f"Issues: {lane.item.rendered_block} {self._prompt_text}"
        )
        send_timeout = _send_timeout_seconds()
        try:
            async with IterationSession(
                self._client,
                config=self._config,
                event_log=self._writers.event_log,
                sinks=self._sinks,
                run_id=self._run_id,
                iter_num=iter_num,
                model=self._config.model,
                reasoning_effort=self._config.reasoning_effort,
                working_directory=str(lane.git.root),
            ) as sdk_session:
                try:
                    await sdk_session.send_and_wait(
                        prompt, timeout=send_timeout
                    )
                except asyncio.TimeoutError:
                    self._diag.warning(
                        "lane #%s send_and_wait timed out after %ss; "
                        "treating as no-progress",
                        lane.item.ref, send_timeout,
                    )
                except Exception as exc:
                    self._diag.warning(
                        "lane #%s send_and_wait raised %s: %s; "
                        "treating as no-progress",
                        lane.item.ref, type(exc).__name__, exc,
                    )
        except Exception as exc:
            self._diag.error(
                "lane #%s IterationSession lifecycle failed: %s: %s",
                lane.item.ref, type(exc).__name__, exc,
            )

    def _account_lane(self, iter_num: int, lane: _Lane) -> int:
        """Post-barrier per-Lane commit accounting + per-worktree Checkpoint.

        Reads the Lane branch's post-session head, emits one
        ``wrapper.commit.recorded`` per new commit (each Lane's Consumption
        attributed to its own issue), then captures any dirty / untracked work
        in a per-worktree Checkpoint on the Lane branch (ADR-0004) so no agent
        work is lost before teardown. Returns the Lane's new-commit count for
        the round's Strike accounting.
        """
        wt_git = lane.git
        if lane.pre_sha is None:
            new_commits: list[git_module.Commit] = []
        else:
            try:
                head = wt_git.head_sha()
                new_commits = wt_git.commits_between(lane.pre_sha, head)
            except git_module.GitError as exc:
                self._diag.warning(
                    "lane #%s commit accounting failed: %s",
                    lane.item.ref, exc,
                )
                new_commits = []

        for c in new_commits:
            self._serial._emit(
                events_module.WRAPPER_COMMIT_RECORDED,
                iter_num=iter_num,
                sha=c.sha,
                subject=c.subject,
                date=c.date,
            )

        self._maybe_checkpoint_lane(iter_num, lane)
        return len(new_commits)

    def _maybe_checkpoint_lane(
        self, iter_num: int, lane: _Lane
    ) -> str | None:
        """Per-worktree Checkpoint on a Lane branch (ADR-0004, per-Lane).

        Mirrors :meth:`_Loop._maybe_checkpoint` but scoped to the Lane's own
        worktree and attributed to the Lane's issue, so uncommitted agent work
        in a Lane is captured on that Lane's branch before the barrier tears the
        worktree down. Non-fatal — a git error warns and returns ``None``.
        """
        wt_git = lane.git
        try:
            dirty = wt_git.is_dirty()
            untracked = wt_git.has_untracked()
        except git_module.GitError as exc:
            self._diag.warning(
                "lane #%s checkpoint dirty-check failed: %s; skipping",
                lane.item.ref, exc,
            )
            return None
        if not (dirty or untracked):
            return None
        try:
            wt_git.add_all()
            sha = wt_git.commit(checkpoint_message(lane.item.ref))
        except git_module.GitError as exc:
            self._diag.warning(
                "lane #%s checkpoint commit failed: %s; continuing without it",
                lane.item.ref, exc,
            )
            return None
        self._serial._emit(
            events_module.WRAPPER_CHECKPOINT_RECORDED,
            iter_num=iter_num,
            sha=sha,
            issue=lane.item.ref,
        )
        return sha

    async def _integrate_wave(self, iter_num: int, lanes: list[_Lane]) -> int:
        """Serialized, robust Integration for a Wave's Lanes (#62 + #63, ADR-0009).

        Lands each green Lane branch on base in **ascending issue-number order**
        (see :func:`_lane_sort_key`), keeping the base branch always green and
        never waiting on a human. Per Lane, in order (see :meth:`_integrate_lane`):

        1. Merge the Lane branch into base and re-run the full feedback loops from
           the *runner* side via the injected :class:`~ralph_afk.gate.GateRunner`.
        2. On **green**, land it — close the issue via the same runner-driven
           closure as serial mode (``source.handle_completions`` -> ``gh issue
           close`` + the ``Closes #N`` backstop, one ``wrapper.auto_close`` per
           closure) and delete the integrated Lane branch — and count it a success
           (the round's Strike progress signal).
        3. A merge **conflict** (:meth:`~ralph_afk.git.GitClient.abort_merge`) or a
           clean merge whose gate goes **red** or cannot run
           (:meth:`~ralph_afk.git.GitClient.revert_merge`) is undone so base stays
           green, then handed to a bounded (K=:data:`_AUTO_RESOLUTION_MAX_ATTEMPTS`)
           auto-resolution agent in a dedicated integration worktree on base
           (:meth:`_auto_resolve_lane`). A green attempt lands and counts as a
           success; after K failures the issue falls back to a serial Iteration
           with exactly one breadcrumb comment and its Lane branch is kept.

        Returns the number of Lanes that landed green — via the happy path or
        auto-resolution — which is the round's Strike progress. A reverted-then-
        recovered Lane counts once; a fallback Lane counts zero.
        """
        successes = 0
        for lane in sorted(lanes, key=_lane_sort_key):
            successes += await self._integrate_lane(iter_num, lane)
        return successes

    async def _integrate_lane(self, iter_num: int, lane: _Lane) -> int:
        """Integrate one Lane; return ``1`` if it landed green, else ``0``.

        The happy path (#62): merge -> gate -> on green :meth:`_land_lane`. On a
        conflicting merge, or a clean merge whose gate goes red / cannot run, the
        merge is undone so base stays green and the Lane is handed to
        :meth:`_auto_resolve_lane` (#63).
        """
        ref = lane.item.ref
        try:
            pre_base = self._git.head_sha()
        except git_module.GitError as exc:
            self._diag.warning(
                "integration #%s: base head_sha failed: %s; skipping", ref, exc
            )
            return 0

        # 1) Attempt the clean landing.
        try:
            self._git.merge(lane.branch)
        except git_module.GitError as exc:
            # A conflicting merge: unwind it (base untouched) and auto-resolve.
            self._diag.warning(
                "integration #%s: merge of %s conflicted: %s; aborting and "
                "auto-resolving",
                ref, lane.branch, exc,
            )
            self._abort_merge_safely(ref)
            return await self._auto_resolve_lane(iter_num, lane)

        # 2) Merge landed cleanly — gate it from the runner side.
        if self._gate_green(ref, "post-merge"):
            self._land_lane(iter_num, lane, pre_base)
            return 1

        # 3) Clean merge but a red / un-runnable gate: revert so base stays
        #    green, then auto-resolve.
        self._revert_merge_safely(ref)
        return await self._auto_resolve_lane(iter_num, lane)

    def _gate_green(
        self, ref: int | str, phase: str, worktree: Path | None = None
    ) -> bool:
        """Run the injected gate on ``worktree`` (default repo root); ``True`` == green.

        A **red** gate or a :exc:`~ralph_afk.gate.GateError` (cannot gate at all)
        both return ``False`` — Integration then reverts / aborts and drives
        auto-resolution; the ``phase`` label enriches the diagnostic.
        """
        target = worktree if worktree is not None else self._repo_root
        try:
            result = self._gate_runner.run(target)
        except gate_module.GateError as exc:
            self._diag.warning(
                "integration #%s: %s gate could not run: %s; treating as red",
                ref, phase, exc,
            )
            return False
        if not result.passed:
            self._diag.warning(
                "integration #%s: %s gate failed on %r",
                ref, phase,
                result.failure.name if result.failure else "unknown",
            )
            return False
        return True

    def _abort_merge_safely(self, ref: int | str) -> None:
        """``git merge --abort`` a conflicted merge; a failure only warns."""
        try:
            self._git.abort_merge()
        except git_module.GitError as exc:
            self._diag.warning(
                "integration #%s: merge --abort failed: %s", ref, exc
            )

    def _revert_merge_safely(self, ref: int | str) -> None:
        """``git revert`` a clean-but-red landing so base stays green.

        A failed revert is escalated to ``error`` — base may be left carrying a
        red merge, which the operator needs to see.
        """
        try:
            self._git.revert_merge()
        except git_module.GitError as exc:
            self._diag.error(
                "integration #%s: revert of red merge failed: %s; base may "
                "carry a red commit",
                ref, exc,
            )

    def _land_lane(self, iter_num: int, lane: _Lane, pre_base: str) -> None:
        """Finish a green landing: close the issue + delete the integrated branch."""
        self._close_landed(iter_num, lane.item, pre_base)
        self._delete_branch_safely(lane.item.ref, lane.branch)

    def _close_landed(
        self, iter_num: int, item: AfkReadyItem, pre_base: str
    ) -> None:
        """Close a landed issue via the serial closure path + emit ``auto_close``.

        Reads the commits the landing added to base (``pre_base`` -> current
        head) and drives the same runner-side closure as serial mode
        (``source.handle_completions`` -> ``gh issue close`` + the ``Closes #N``
        backstop), emitting one ``wrapper.auto_close`` per closure. Shared by the
        happy-path landing and a successful auto-resolution landing.
        """
        try:
            post_base = self._git.head_sha()
            landed = self._git.commits_between(pre_base, post_base)
        except git_module.GitError as exc:
            self._diag.warning(
                "integration #%s: post-merge accounting failed: %s",
                item.ref, exc,
            )
            landed = []
        for completion in self._serial._handle_completions_safely(
            [item], landed
        ):
            self._serial._emit(
                events_module.WRAPPER_AUTO_CLOSE,
                iter_num=iter_num,
                issue=completion.ref,
                sha=completion.sha,
                shas=list(completion.shas),
            )

    def _delete_branch_safely(self, ref: int | str, branch: str) -> None:
        """``git branch -D`` an integrated branch; a failure only warns."""
        try:
            self._git.delete_branch(branch)
        except git_module.GitError as exc:
            self._diag.warning(
                "integration #%s: delete of %s failed: %s", ref, branch, exc
            )

    async def _auto_resolve_lane(self, iter_num: int, lane: _Lane) -> int:
        """Bounded auto-resolution for a reverted / aborted Lane (#63, ADR-0009).

        Creates ONE dedicated integration worktree on base and, up to
        K=:data:`_AUTO_RESOLUTION_MAX_ATTEMPTS` times, runs a fresh resolution
        agent session pinned to it (:meth:`_run_resolution_session`) and re-gates
        that worktree. The first **green** attempt merges the integration branch
        onto base, closes the issue, deletes both the integration branch and the
        (now-landed) Lane branch, and returns ``1``. If all K attempts stay red
        the Lane falls back to a serial Iteration
        (:meth:`_fallback_lane_to_serial`) and returns ``0``. The integration
        worktree and branch are always reaped; the Lane branch is kept only on
        failure (a breadcrumb).
        """
        ref = lane.item.ref
        if not isinstance(ref, int):
            # Auto-resolution addresses one integer issue (its worktree / branch
            # names derive from the number); a non-int ref cannot be recovered.
            self._fallback_lane_to_serial(lane)
            return 0

        base = self._resolve_base_ref()
        int_branch = git_module.integration_branch_name(self._run_id, ref)
        int_path = _integration_worktree_path(self._repo_root, self._run_id, ref)
        try:
            int_git = self._git.add_worktree(
                int_path, branch=int_branch, base=base
            )
        except git_module.GitError as exc:
            self._diag.warning(
                "integration #%s: could not create integration worktree: %s; "
                "falling back to serial",
                ref, exc,
            )
            self._fallback_lane_to_serial(lane)
            return 0

        landed = False
        try:
            for attempt in range(1, _AUTO_RESOLUTION_MAX_ATTEMPTS + 1):
                try:
                    pre_base = self._git.head_sha()
                except git_module.GitError as exc:
                    self._diag.warning(
                        "integration #%s: base head_sha failed before "
                        "auto-resolution attempt %s: %s",
                        ref, attempt, exc,
                    )
                    break
                await self._run_resolution_session(
                    iter_num, lane, int_git, attempt
                )
                if not self._gate_green(
                    ref, f"auto-resolution attempt {attempt}", int_path
                ):
                    continue
                # Green: land the resolved integration branch on base.
                try:
                    self._git.merge(int_branch)
                except git_module.GitError as exc:
                    self._diag.warning(
                        "integration #%s: merge of resolved %s failed: %s; "
                        "retrying",
                        ref, int_branch, exc,
                    )
                    continue
                self._close_landed(iter_num, lane.item, pre_base)
                self._delete_branch_safely(ref, lane.branch)
                landed = True
                break
        finally:
            try:
                self._git.remove_worktree(int_path, force=True)
            except git_module.GitError as exc:
                self._diag.warning(
                    "integration #%s: integration worktree remove failed: %s",
                    ref, exc,
                )
            self._delete_branch_safely(ref, int_branch)

        if not landed:
            self._fallback_lane_to_serial(lane)
            return 0
        return 1

    async def _run_resolution_session(
        self,
        iter_num: int,
        lane: _Lane,
        int_git: git_module.GitClient,
        attempt: int,
    ) -> None:
        """Run one auto-resolution agent session in the integration worktree (#63).

        A fresh :class:`IterationSession` pinned to the dedicated integration
        worktree, tasked to merge the Lane branch, resolve conflicts, make the
        feedback loops pass, and commit. Bulletproof like
        :meth:`_run_lane_session` — a timeout or error is logged and swallowed so
        the attempt just reads as still-red and the bound advances.
        """
        prompt = self._resolution_prompt(lane, attempt)
        send_timeout = _send_timeout_seconds()
        try:
            async with IterationSession(
                self._client,
                config=self._config,
                event_log=self._writers.event_log,
                sinks=self._sinks,
                run_id=self._run_id,
                iter_num=iter_num,
                model=self._config.model,
                reasoning_effort=self._config.reasoning_effort,
                working_directory=str(int_git.root),
            ) as sdk_session:
                try:
                    await sdk_session.send_and_wait(prompt, timeout=send_timeout)
                except asyncio.TimeoutError:
                    self._diag.warning(
                        "integration #%s: auto-resolution attempt %s timed out "
                        "after %ss; treating as still-red",
                        lane.item.ref, attempt, send_timeout,
                    )
                except Exception as exc:
                    self._diag.warning(
                        "integration #%s: auto-resolution attempt %s raised "
                        "%s: %s; treating as still-red",
                        lane.item.ref, attempt, type(exc).__name__, exc,
                    )
        except Exception as exc:
            self._diag.error(
                "integration #%s: auto-resolution session lifecycle failed: "
                "%s: %s",
                lane.item.ref, type(exc).__name__, exc,
            )

    def _resolution_prompt(self, lane: _Lane, attempt: int) -> str:
        """The dedicated auto-resolution brief (#63).

        Unlike a Lane / serial prompt this is not issue-collection work: it asks
        the agent to merge the Lane branch into the integration worktree's base,
        fix any conflicts, make the feedback loops green, and commit — driving a
        clean Integration the runner can then land.
        """
        return (
            f"Auto-resolution attempt {attempt} of "
            f"{_AUTO_RESOLUTION_MAX_ATTEMPTS} for issue #{lane.item.ref}. Merge "
            f"branch {lane.branch} into this integration worktree's base branch, "
            f"resolve any merge conflicts, make all feedback loops in AGENTS.md "
            f"pass, and commit the result. {self._prompt_text}"
        )

    def _fallback_lane_to_serial(self, lane: _Lane) -> None:
        """Terminal auto-resolution failure -> fall back to a serial Iteration (#63).

        Posts exactly one automated breadcrumb comment on the issue and leaves it
        **OPEN** — and in :attr:`_worked`, so it is never re-Laned — so a later
        round, finding no fresh eligible ``parallel-safe`` work, runs a serial
        ``_run_one_iteration`` (the proven safe path) that re-collects the issue
        and works it. The failed Lane branch is intentionally **kept** (never
        deleted) as a breadcrumb.
        """
        self._source.comment(lane.item.ref, _AUTO_RESOLUTION_FALLBACK_COMMENT)

    def _tick_round(
        self, iter_num: int, *, commits: int, closures: int
    ) -> str:
        """Tick the shared Strike machine once for a round + emit its event.

        A single tick per round (a Wave or a serial Iteration), mirroring the
        serial loop's step-10 semantics: progress (a Lane commit or a closure)
        resets strikes, a no-progress round adds one, and the threshold aborts
        the run.
        """
        outcome = self._serial._strike_machine.tick(
            commits_in_iter=commits,
            auto_closures_in_iter=closures,
        )
        if outcome == "aborted" or (commits == 0 and closures == 0):
            self._serial._emit(
                events_module.WRAPPER_STRIKE,
                iter_num=iter_num,
                strikes=self._serial._strike_machine.strikes,
                max_strikes=self._config.max_nmt_strikes,
                outcome=("abort" if outcome == "aborted" else "warn"),
            )
        return outcome

    def _resolve_base_ref(self) -> str:
        """The base ref new Lane branches are cut from.

        The current branch name when on one (the natural base per ADR-0008),
        else the current commit SHA (detached HEAD), else the literal ``HEAD``
        — so ``git worktree add -b <lane> <path> <base>`` always has a valid
        start point.
        """
        try:
            branch = self._git.current_branch()
        except git_module.GitError:
            branch = None
        if branch:
            return branch
        try:
            return self._git.head_sha()
        except git_module.GitError:
            return "HEAD"


class InteractiveDriver(Protocol):
    """Strategy that runs the loop as an *observed peer* of a Textual app.

    The concrete implementation is
    :class:`ralph_afk.interactive.driver.InteractiveDriver`. It is referenced
    here only as a **structural Protocol** so :mod:`ralph_afk.loop` never
    imports the interactive package — and therefore never imports Textual,
    keeping the import-guard convention (ADR-0001) intact on the loop side.

    The contract is deliberately tiny:

    * :attr:`state` is the Textual-agnostic
      :class:`~ralph_afk.interactive.state.LiveRunState`, registered by
      :func:`run` as the primary sink on the interactive path.
    * :meth:`attach_panes` receives the loop-owned Summary/Log pane sources
      (issue #26) before :meth:`run` builds the app.
    * :meth:`attach_detach` receives the exit-model handoff (issue #28): the
      swappable :class:`~ralph_afk.sinks.SinkFanout`, the parked line-printer
      Renderer to swap in on a **Detach**, and the stdout console for the
      **Stop** scrollback record.
    * :meth:`run` is handed the loop's ``drive`` coroutine-function and is
      responsible for launching it and the Textual app as **peer asyncio
      tasks** (not parent/child), returning the loop's process exit code. A
      user **Stop** (``q`` / ``Ctrl+C``) cancels the loop task; a **Detach**
      (``d``) swaps the sink to the line printer and lets the loop run on.
    """

    state: EventSink

    def attach_panes(
        self,
        *,
        summary: RunSummary | None,
        log_source: Callable[[], str] | None,
    ) -> None: ...

    def attach_detach(
        self,
        *,
        sinks: SinkFanout,
        line_printer: EventSink,
        console: Console,
    ) -> None: ...

    async def run(self, drive: Callable[[], Coroutine[object, object, int]]) -> int: ...


async def run(config: RunConfig, *, driver: InteractiveDriver | None = None) -> int:
    """Drive one ``ralph-afk`` invocation to completion.

    Constructs the long-lived per-run state (writers, summary, renderer,
    client, source), drives the iteration loop, and returns the
    appropriate process exit code.

    Args:
        config: The frozen :class:`RunConfig` composed by
            :func:`ralph_afk.cli.main`.
        driver: Optional interactive driver (ADR-0001 observer model). When
            ``None`` (the default, non-interactive path) the line-printer
            :class:`~ralph_afk.ui.renderer.Renderer` is the sole sink and the
            loop is driven directly — **byte-for-byte unchanged**. When
            supplied, the driver's Textual-agnostic ``state``
            (:class:`~ralph_afk.interactive.state.LiveRunState`) becomes the
            sole sink and :meth:`InteractiveDriver.run` launches the loop and a
            Textual app as **peer asyncio tasks**; ``q`` / ``Ctrl+C`` (Stop)
            cancels the loop task. The Renderer is still constructed but parked
            so issue #28's Detach can swap it back in via
            :meth:`~ralph_afk.sinks.SinkFanout.set_sinks`.

    Returns:
        Process exit code:

        * ``0`` — clean termination (empty AFK-ready pool or
          ``max_iterations`` cap reached).
        * ``1`` — abort (NMT strike threshold or
          preflight / setup failure).
    """
    # 1) Git seam (root-bound) + prompt file. The client resolves and binds the
    #    repository root once; ``.root`` feeds the writers / prompt / source setup.
    try:
        git = _make_git_client()
    except git_module.GitError as exc:
        print(
            f"ralph-afk: failed to resolve git repository root: {exc}",
            file=sys.stderr,
        )
        return 1
    repo_root = git.root

    try:
        prompt_text = _read_prompt(repo_root)
    except FileNotFoundError as exc:
        print(f"ralph-afk: {exc}", file=sys.stderr)
        return 1

    # 2) Pricing — bail out loudly on a malformed override (rubber-duck
    #    feedback: silent fallback hides operator intent).
    try:
        pricing = load_pricing(config.pricing_file)
    except PricingError as exc:
        print(f"ralph-afk: pricing load failed: {exc}", file=sys.stderr)
        return 1

    # 3) Writers + diagnostics logger + renderer + sink fan-out.
    try:
        writers = create_writers(repo_root)
    except Exception as exc:
        print(
            f"ralph-afk: failed to construct writers bundle: "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 1
    summary = RunSummary(pricing=pricing)
    console = get_console()
    renderer = Renderer(
        console=console,
        summary=summary,
        verbosity=config.verbosity,
        render_reasoning=config.render_reasoning,
    )
    # The line-printer Renderer is the sole sink on the non-interactive
    # path (issue #22); JSONL logging is written separately and stays
    # always-on regardless of which sinks are registered. On the interactive
    # path (issue #23, ADR-0001) the driver's Textual-agnostic LiveRunState
    # is the primary sink. For #26's Log + Summary tabs a SECOND sink is
    # registered: a Renderer writing the same line-printer output to an
    # in-memory buffer (safe while Textual owns the real terminal). It drives
    # the shared RunSummary (Summary tab) and its buffer feeds the Log tab.
    # The stdout Renderer above stays parked (not in the sink list) so #28's
    # Detach can swap it back in via set_sinks once the TUI tears down.
    if driver is None:
        sinks = SinkFanout([renderer])
    else:
        log_buffer = io.StringIO()
        log_renderer = Renderer(
            console=Console(file=log_buffer, force_terminal=False),
            summary=summary,
            verbosity=config.verbosity,
            render_reasoning=config.render_reasoning,
        )
        sinks = SinkFanout([driver.state, log_renderer])
        driver.attach_panes(summary=summary, log_source=log_buffer.getvalue)
        # Hand the driver the exit-model seam (issue #28): the swappable sink
        # list, the parked stdout Renderer to swap in on Detach, and the real
        # console for the Stop / natural-completion scrollback summary.
        driver.attach_detach(sinks=sinks, line_printer=renderer, console=console)
    diag = writers.diagnostics

    # 4) IssueSource (factory dispatches on config.issue_source). A
    #    ValueError here means the config carried a value the loop
    #    doesn't recognise — surface a clean exit 1 rather than letting
    #    the exception escape.
    include_prs = _resolve_include_prs(config, repo_root)
    try:
        source = _make_issue_source(
            config, repo_root, diag, include_prs=include_prs
        )
    except ValueError as exc:
        diag.error("issue source construction failed: %s", exc)
        print(f"ralph-afk: {exc}", file=sys.stderr)
        try:
            writers.run_summary.flush()
        except Exception:
            pass
        return 1

    # 5) SDK client (lazy via the factory the tests monkeypatch). If
    #    construction itself raises (SDK install broken, port already
    #    held by another process, etc.) we must surface a clean error
    #    rather than letting the traceback escape ``asyncio.run``.
    client: CopilotClient | None = None
    try:
        client = _make_client()
    except Exception as exc:
        diag.error(
            "CopilotClient construction failed: %s: %s",
            type(exc).__name__, exc,
        )
        print(
            f"ralph-afk: failed to construct CopilotClient: "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        # Best-effort: still flush the writers so the operator gets at
        # least an empty run-summary JSON pointing at the failure.
        try:
            writers.run_summary.flush()
        except Exception:
            pass
        return 1

    # Dispatch: Parallel mode (opt-in, config.parallel > 1) drives the
    # Wave/Lane orchestrator with the injected runner-side Integration gate
    # (#60); serial (the default, parallel == 1) drives the existing loop
    # byte-for-byte unchanged. Both expose the same ``drive()`` contract.
    loop: _Loop | _ParallelLoop
    if config.parallel > 1:
        loop = _ParallelLoop(
            config=config,
            git=git,
            prompt_text=prompt_text,
            pricing=pricing,
            writers=writers,
            sinks=sinks,
            summary=summary,
            client=client,
            source=source,
            diag=diag,
            gate_runner=_make_gate_runner(),
            include_prs=include_prs,
        )
    else:
        loop = _Loop(
            config=config,
            git=git,
            prompt_text=prompt_text,
            pricing=pricing,
            writers=writers,
            sinks=sinks,
            summary=summary,
            client=client,
            source=source,
            diag=diag,
            include_prs=include_prs,
        )

    exit_code = 1
    try:
        try:
            with writers.event_log, writers.run_summary:
                # Root OTel span for the entire iteration loop. The
                # SDK's subprocess telemetry (configured via
                # _build_telemetry_config) nests under this span's
                # W3C trace context — see ralph_afk.telemetry.otel
                # module docstring for the propagation contract.
                with telemetry.span("ralph_afk.run"):
                    try:
                        if driver is None:
                            exit_code = await loop.drive()
                        else:
                            # ADR-0001: the app and the loop run as peer
                            # asyncio tasks; the driver owns the peering and
                            # Stop-cancels the loop task.
                            exit_code = await driver.run(loop.drive)
                    except Exception as exc:
                        diag.error(
                            "ralph_afk loop crashed: %s: %s",
                            type(exc).__name__, exc,
                        )
                        exit_code = 1
        except Exception as exc:
            # Writer __exit__ raised (disk full, perm denied flushing
            # the run-summary JSON, etc.). The body already ran; we
            # just couldn't persist. Don't let this turn into a
            # tracebacked exit.
            diag.error(
                "writers __exit__ failed: %s: %s",
                type(exc).__name__, exc,
            )
            print(
                f"ralph-afk: writers __exit__ failed: "
                f"{type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            exit_code = 1
    finally:
        # Always release the SDK subprocess, even on a body-level crash.
        if client is not None:
            try:
                await client.stop()
            except Exception as exc:
                diag.warning("CopilotClient.stop() failed: %s", exc)
        # Drain OTel exporters AFTER the root `ralph_afk.run` span has
        # closed. BatchSpanProcessor buffers — without an explicit
        # flush, spans queued near the end of the run could be dropped
        # on process exit. No-op when OTel is disabled.
        try:
            telemetry.force_flush()
        except Exception as exc:  # pragma: no cover - defensive
            diag.warning("telemetry.force_flush() failed: %s", exc)

    return exit_code
