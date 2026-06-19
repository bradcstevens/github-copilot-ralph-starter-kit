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
2. **Stale-worktree guard** via :func:`ralph_afk.git.is_dirty`.
3. Collect AFK-ready pool via :meth:`IssueSource.collect_afk_ready`.
4. Clean-exit on empty pool.
5. Build prompt: ``"Previous commits: <last5> Issues: <blocks> " + prompt_md``
   where each ``<block>`` is the source-rendered
   :attr:`AfkReadyItem.rendered_block`.
6. Capture ``pre_sha`` *immediately* before invoking the SDK — so a slow
   ``gh issue_view`` call before this point cannot affect the
   ``commits_between(pre_sha, head)`` accounting after.
7. Open :class:`~ralph_afk.session.IterationSession`,
   ``await session.send_and_wait(prompt, timeout=long)``.
8. ``head_sha = git.head_sha()``; ``commits = git.commits_between(pre, head)``.
9. Emit one ``wrapper.commit.recorded`` per new commit so the renderer
   increments the iteration's commit count.
10. **Completion backstop** via
    :meth:`IssueSource.handle_completions`. Each returned
    :class:`~ralph_afk.sources.Completion` produces one
    ``wrapper.auto_close`` event. The GitHub backend closes issues via
    ``gh issue close``; the PRDs backend returns an empty list (the
    agent owns ``git mv ... prds/<feat>/done/``).
11. NMT strike accounting: progress (``commits>0`` or ``auto_closures>0``)
    resets strikes; no-progress increments, possibly tripping the
    abort threshold.
12. Emit ``wrapper.iteration.end`` (renderer closes snapshot panel) and
    persist :class:`~ralph_afk.persist.IterationCounters` from the
    closed snapshot.

Design notes:

* **Source-agnostic loop body.** The loop holds one
  :class:`IssueSource` and dispatches the three Protocol methods
  through it. Issue #11 lifts the PRDs backend; #10 introduced the
  GitHub backend. Adding a new backend (e.g. a remote API) means
  adding one ``IssueSource`` impl and one factory branch — the
  iteration body never changes.
* **Inter-module fan-out via the renderer.** Every wrapper-level event
  (``wrapper.run.start``, ``wrapper.iteration.start``, etc.) goes through
  :meth:`_emit_wrapper_event` which:
  1. Constructs an envelope via :func:`ralph_afk.events.make_event`.
  2. Writes the JSONL line via the event log writer (scrubber pipeline).
  3. Hands it to the renderer for the Rich-driven terminal output and
     RunSummary accumulator updates.
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
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any, Iterable

from copilot import CopilotClient

from ralph_afk import events as events_module
from ralph_afk import git as git_module
from ralph_afk.config import RunConfig
from ralph_afk.persist import (
    IterationCounters,
    WritersBundle,
    create_writers,
)
from ralph_afk.pricing import Pricing, PricingError, load_pricing
from ralph_afk.session import IterationSession
from ralph_afk.sources import (
    AfkReadyItem,
    GitHubIssueSource,
    IssueSource,
    PrdsIssueSource,
)
from ralph_afk.telemetry import otel as telemetry
from ralph_afk.ui import Renderer, RunSummary, get_console
from ralph_afk.wrapper import NMTStrikeStateMachine

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
        return GitHubIssueSource(diag, include_prs=include_prs)
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


class _Loop:
    """Stateful orchestrator for one ``ralph-afk`` invocation.

    Bundles the long-lived per-run state — writers, summary, renderer,
    SDK client, source, strike state machine — so the public
    :func:`run` function stays small and the per-iteration helper
    methods can read self instead of threading every value through
    their signatures.
    """

    def __init__(
        self,
        *,
        config: RunConfig,
        repo_root: Path,
        prompt_text: str,
        pricing: Pricing,
        writers: WritersBundle,
        renderer: Renderer,
        summary: RunSummary,
        client: CopilotClient,
        source: IssueSource,
        diag: logging.Logger,
        include_prs: bool = False,
    ) -> None:
        self._config = config
        self._repo_root = repo_root
        self._prompt_text = prompt_text
        self._pricing = pricing
        self._writers = writers
        self._renderer = renderer
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

    # -- event fan-out ------------------------------------------------------

    def _emit(
        self,
        event_type: str,
        *,
        iter_num: int | None,
        **payload: Any,
    ) -> dict[str, Any]:
        """Compose, persist, and render one wrapper-level event."""
        envelope = events_module.make_event(
            type=event_type,
            run_id=self._writers.run_id,
            iter=iter_num,
            **payload,
        )
        try:
            self._writers.event_log.write(envelope)
        except Exception as exc:  # pragma: no cover - defensive
            self._diag.warning("event log write failed: %s", exc)
        try:
            self._renderer.render(envelope)
        except Exception as exc:  # pragma: no cover - defensive
            self._diag.warning("renderer failed on %s: %s", event_type, exc)
        return envelope

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
            * ``"stale_worktree"`` — dirty worktree; abort exit 1.
            * ``"aborted"`` — NMT strike machine tripped; abort exit 1.

        OTel span tree: opens ``ralph_afk.iteration`` for the entire body,
        with three children — ``ralph_afk.collect_issues`` around the
        pool discovery, ``ralph_afk.session`` around the SDK session
        lifecycle, and ``ralph_afk.enforce_closures`` around the
        source-specific completion backstop. Empty-pool and
        stale-worktree paths emit only the partial subtree (no
        ``session`` / ``enforce_closures`` spans); see
        ``tests/test_iteration_end_to_end.py::test_loop_emits_otel_span_tree_when_enabled``.
        """
        with telemetry.span(
            "ralph_afk.iteration", iter=iter_num
        ) as iteration_span:
            self._emit(
                events_module.WRAPPER_ITERATION_START,
                iter_num=iter_num,
            )

            # 1) Stale-worktree guard.
            if git_module.is_dirty(self._repo_root):
                self._emit(
                    events_module.WRAPPER_STALE_WORKTREE_ABORTED,
                    iter_num=iter_num,
                )
                # Close the snapshot the renderer just opened so the run-end
                # Table doesn't show a half-open row.
                self._emit(events_module.WRAPPER_ITERATION_END, iter_num=iter_num)
                self._record_counters(iter_num)
                return ("stale_worktree", 0, 0)

            # 1b) PR branch hygiene. A prior PR iteration may have run
            #     `gh pr checkout <N>` and left HEAD on the PR branch. The
            #     worktree is clean (the guard above just passed), so restore
            #     the captured base branch — otherwise this iteration's
            #     commits and `commits_between` accounting would land on the
            #     PR branch. Gated on `include_prs` so the default (issues-only)
            #     path is byte-for-byte unchanged and never touches branches.
            if self._include_prs and self._base_branch is not None:
                try:
                    on_branch = git_module.current_branch(self._repo_root)
                except git_module.GitError as exc:
                    self._diag.warning(
                        "current_branch check failed: %s; skipping base "
                        "restore",
                        exc,
                    )
                    on_branch = None
                if on_branch is not None and on_branch != self._base_branch:
                    try:
                        git_module.switch(self._base_branch, self._repo_root)
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
                recent = git_module.recent_commits(5, self._repo_root)
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
                pre_sha = git_module.head_sha(self._repo_root)
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
                        renderer=self._renderer,
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
                head = git_module.head_sha(self._repo_root)
            except git_module.GitError as exc:
                self._diag.warning(
                    "post-iteration git head_sha failed: %s; "
                    "skipping commit accounting", exc,
                )
                head = pre_sha
            try:
                new_commits = git_module.commits_between(
                    pre_sha, head, self._repo_root
                )
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

            # 8) Strike state machine + emit appropriate events.
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

            # 9) Close the iteration snapshot, persist counters.
            self._emit(events_module.WRAPPER_ITERATION_END, iter_num=iter_num)
            self._record_counters(iter_num)

            if outcome == "aborted":
                return ("aborted", len(new_commits), auto_closures)
            return ("continue", len(new_commits), auto_closures)

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
                self._base_branch = git_module.current_branch(self._repo_root)
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
                    if outcome == "stale_worktree":
                        outcome_label = "stale_worktree"
                        exit_code = 1
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


async def run(config: RunConfig) -> int:
    """Drive one ``ralph-afk`` invocation to completion.

    Constructs the long-lived per-run state (writers, summary, renderer,
    client, source), drives the iteration loop, and returns the
    appropriate process exit code.

    Args:
        config: The frozen :class:`RunConfig` composed by
            :func:`ralph_afk.cli.main`.

    Returns:
        Process exit code:

        * ``0`` — clean termination (empty AFK-ready pool or
          ``max_iterations`` cap reached).
        * ``1`` — abort (stale worktree, NMT strike threshold,
          preflight / setup failure).
    """
    # 1) Repo root + prompt file.
    try:
        repo_root = git_module.repo_root()
    except git_module.GitError as exc:
        print(
            f"ralph-afk: failed to resolve git repository root: {exc}",
            file=sys.stderr,
        )
        return 1

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

    # 3) Writers + diagnostics logger + renderer.
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

    loop = _Loop(
        config=config,
        repo_root=repo_root,
        prompt_text=prompt_text,
        pricing=pricing,
        writers=writers,
        renderer=renderer,
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
                        exit_code = await loop.drive()
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
