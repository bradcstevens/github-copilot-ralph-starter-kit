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

The per-iteration :class:`~ralph_afk.session.IterationSession` is opened
inside :func:`run` once per iteration.

Per-iteration sequence (parity with ``ralph/afk.sh:305-433``):

1. Cap check on ``max_iterations``.
2. **Stale-worktree guard** via :func:`ralph_afk.git.is_dirty`.
3. Collect AFK-ready pool: :func:`ralph_afk.gh.issue_list` filtered by
   body discriminators (``## Parent`` AND ``## Acceptance criteria``).
4. Clean-exit on empty pool.
5. Per-issue :func:`ralph_afk.gh.issue_view` to fetch comments
   (parity with bash line 151).
6. Build prompt: ``"Previous commits: <last5> Issues: <blocks> " + prompt_md``.
7. Capture ``pre_sha`` *immediately* before invoking the SDK — so a slow
   ``gh issue_view`` call before this point cannot affect the
   ``commits_between(pre_sha, head)`` accounting after.
8. Open :class:`~ralph_afk.session.IterationSession`,
   ``await session.send_and_wait(prompt, timeout=long)``.
9. ``head_sha = git.head_sha()``; ``commits = git.commits_between(pre, head)``.
10. Emit one ``wrapper.commit.recorded`` per new commit so the renderer
    increments the iteration's commit count.
11. **Auto-close backstop**: extract closure refs from concatenated
    commit messages via :func:`ralph_afk.wrapper.extract_close_refs`,
    filter to the iteration's pool via
    :func:`ralph_afk.wrapper.filter_to_pool`, re-check each surviving
    issue's state, and close any that are still ``OPEN``.
12. NMT strike accounting: progress (``commits>0`` or ``auto_closures>0``)
    resets strikes; no-progress increments, possibly tripping the
    abort threshold.
13. Emit ``wrapper.iteration.end`` (renderer closes snapshot panel) and
    persist :class:`~ralph_afk.persist.IterationCounters` from the
    closed snapshot.

Design notes:

* **Inter-module fan-out via the renderer.** Every wrapper-level event
  (``wrapper.run.start``, ``wrapper.iteration.start``, etc.) goes through
  :meth:`_emit_wrapper_event` which:
  1. Constructs an envelope via :func:`ralph_afk.events.make_event`.
  2. Writes the JSONL line via the event log writer (scrubber pipeline).
  3. Hands it to the renderer for the Rich-driven terminal output and
     RunSummary accumulator updates.
* **SDK + gh failure containment.** ``send_and_wait`` failures are
  caught and treated as no-progress (matching bash's ``copilot`` exit-rc
  handling at lines 365-367). Per-issue ``gh.issue_close`` failures are
  logged via the diagnostics logger and the loop continues — losing one
  closure is preferable to skipping the rest of the iteration's
  bookkeeping.
* **One ``CopilotClient`` per invocation.** Constructed lazily inside
  :func:`run` via the module-level :func:`_make_client` factory (which
  tests monkeypatch). Disconnected via ``await client.stop()`` in a
  ``finally`` block so even an early-loop crash releases the SDK's
  subprocess.
* **``ISSUE_SOURCE=prds`` is not implemented in this slice.** The
  config dataclass accepts it; the loop raises
  :class:`NotImplementedError` at entry. Issue #11 lifts that.
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
from ralph_afk import gh as gh_module
from ralph_afk import git as git_module
from ralph_afk.config import RunConfig
from ralph_afk.persist import (
    IterationCounters,
    WritersBundle,
    create_writers,
)
from ralph_afk.pricing import Pricing, PricingError, load_pricing
from ralph_afk.session import IterationSession
from ralph_afk.ui import Renderer, RunSummary, get_console
from ralph_afk.wrapper import (
    NMTStrikeStateMachine,
    extract_close_refs,
    filter_to_pool,
)

__all__ = ["run"]

# Default SDK ``send_and_wait`` timeout. AFK iterations can run for an
# hour or more; the SDK's default 60s is far too aggressive. Tunable
# via the ``RALPH_SEND_TIMEOUT_SECONDS`` env var so an operator can
# tighten it when debugging a wedged session.
_DEFAULT_SEND_TIMEOUT_SECONDS: float = 7200.0

# Bash uses ``grep -q '^## Parent'`` (line-anchored). Match the literal
# semantics here so we never accept a body that mentions ``## Parent``
# inside a code fence or quoted block.
_RE_PARENT = re.compile(r"^## Parent", re.MULTILINE)
_RE_AC = re.compile(r"^## Acceptance criteria", re.MULTILINE)


def _make_client() -> CopilotClient:
    """Construct the per-invocation :class:`CopilotClient`.

    Factored to its own module-level function so tests can monkeypatch
    it (``monkeypatch.setattr("ralph_afk.loop._make_client", ...)``) to
    return a fake. Production callers get the SDK's default
    construction.
    """
    return CopilotClient()


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


def _format_issue_block(issue: gh_module.Issue) -> str:
    """Render one AFK-ready issue as the agent-facing prompt block.

    Mirrors the jq filter at ``ralph/afk.sh:137-144``: header, body,
    then up to 5 recent comments sorted newest-first.
    """
    labels_str = ", ".join(issue.labels)
    header = f"=== Issue #{issue.number}: {issue.title} [labels: {labels_str}] ==="
    body = issue.body or ""

    recent = sorted(
        issue.comments,
        key=lambda c: c.created_at,
        reverse=True,
    )[:5]
    if not recent:
        return f"{header}\n{body}"

    comment_lines = [
        f"[{c.created_at} @{c.author}] {c.body}" for c in recent
    ]
    return (
        f"{header}\n{body}\n\n"
        f"--- Recent comments (newest first, up to 5) ---\n"
        + "\n\n".join(comment_lines)
    )


def _format_recent_commits(commits: Iterable[git_module.Commit]) -> str:
    """Render the last-5-commits block fed into the prompt prefix.

    Mirrors the bash format at ``ralph/afk.sh:324``: one line per commit
    (sha, date, then the message body terminated by ``---``).
    """
    parts: list[str] = []
    for c in commits:
        parts.append(f"{c.sha}\n{c.date}\n{c.message}---")
    if not parts:
        return "No commits found"
    return "\n".join(parts)


def _is_afk_ready(body: str) -> bool:
    """Return True iff the issue body satisfies the AFK-ready discriminator.

    Mirrors the bash double-grep at ``ralph/afk.sh:154-155``: body must
    contain BOTH ``^## Parent`` and ``^## Acceptance criteria`` (line-anchored).
    """
    return bool(_RE_PARENT.search(body)) and bool(_RE_AC.search(body))


class _Loop:
    """Stateful orchestrator for one ``ralph-afk`` invocation.

    Bundles the long-lived per-run state — writers, summary, renderer,
    SDK client, strike state machine — so the public :func:`run`
    function stays small and the per-iteration helper methods can read
    self instead of threading every value through their signatures.
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
        diag: logging.Logger,
    ) -> None:
        self._config = config
        self._repo_root = repo_root
        self._prompt_text = prompt_text
        self._pricing = pricing
        self._writers = writers
        self._renderer = renderer
        self._summary = summary
        self._client = client
        self._diag = diag
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

    # -- preflight / collection --------------------------------------------

    def _preflight_github(self) -> int | None:
        """Validate ``gh`` posture before the first iteration.

        Mirrors the bash preflight at ``ralph/afk.sh:87-102``. Returns
        ``None`` on success or a non-zero exit code on failure.
        """
        try:
            authed = gh_module.auth_status()
        except gh_module.GhError as exc:
            self._diag.error(
                "gh preflight failed: %s. Install `gh` from https://cli.github.com/.",
                exc,
            )
            return 1
        if not authed:
            self._diag.error(
                "gh is not authenticated. Run `gh auth login` and re-run ralph-afk."
            )
            return 1
        try:
            repo = gh_module.repo_view()
        except gh_module.GhError as exc:
            self._diag.error(
                "gh repo view failed: %s. Ralph-afk must be run from inside a "
                "clone of a GitHub repository.",
                exc,
            )
            return 1
        self._diag.info("preflight ok: %s", repo.nwo)
        return None

    def _collect_afk_ready(self) -> list[gh_module.Issue]:
        """Fetch the AFK-ready pool with per-issue ``issue_view`` enrichment."""
        try:
            candidates = gh_module.issue_list("ready-for-agent")
        except gh_module.GhError as exc:
            self._diag.error("gh issue list failed: %s", exc)
            return []

        # Cheap filter on bodies BEFORE the N+1 issue_view fetch — saves
        # a round-trip on PRDs and other ready-for-agent issues that
        # don't satisfy the AFK discriminator.
        ready_candidates = [i for i in candidates if _is_afk_ready(i.body or "")]

        enriched: list[gh_module.Issue] = []
        for issue in ready_candidates:
            try:
                full = gh_module.issue_view(issue.number)
            except gh_module.GhError as exc:
                self._diag.warning(
                    "gh issue view #%s failed: %s; skipping for this iteration",
                    issue.number, exc,
                )
                continue
            if _is_afk_ready(full.body or ""):
                enriched.append(full)
        return enriched

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
        """
        self._emit(
            events_module.WRAPPER_ITERATION_START,
            iter_num=iter_num,
        )

        # 1) Stale-worktree guard (bash line 315).
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

        # 2) Collect AFK-ready pool.
        pool = self._collect_afk_ready()
        pool_numbers = [i.number for i in pool]
        self._emit(
            events_module.WRAPPER_AFK_READY_COLLECTED,
            iter_num=iter_num,
            issues=pool_numbers,
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

        # 3) Build prompt (last-5 commits + AFK-ready issue blocks + prompt body).
        try:
            recent = git_module.recent_commits(5, self._repo_root)
        except git_module.GitError as exc:
            self._diag.warning("recent_commits failed: %s; using empty prefix", exc)
            recent = []
        commits_block = _format_recent_commits(recent)
        issues_block = "\n\n".join(_format_issue_block(i) for i in pool)
        prompt = (
            f"Previous commits: {commits_block} "
            f"Issues: {issues_block} {self._prompt_text}"
        )

        # 4) Capture pre_sha *after* the slow gh fetch so any commit
        #    that landed while we were enriching the pool isn't
        #    incorrectly attributed to this iteration.
        try:
            pre_sha = git_module.head_sha(self._repo_root)
        except git_module.GitError as exc:
            self._diag.error("git head_sha failed: %s; aborting iteration", exc)
            self._emit(events_module.WRAPPER_ITERATION_END, iter_num=iter_num)
            self._record_counters(iter_num)
            return ("continue", 0, 0)

        # 5) Run the SDK session.
        send_timeout = _send_timeout_seconds()
        try:
            async with IterationSession(
                self._client,
                config=self._config,
                event_log=self._writers.event_log,
                renderer=self._renderer,
                run_id=self._writers.run_id,
                iter_num=iter_num,
                model=self._config.model,
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
                    # Match bash line 365-367 — treat any copilot failure
                    # as no-progress; bookkeeping below still runs.
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

        # 7) Auto-close backstop.
        auto_closures = 0
        if new_commits:
            concatenated = "\n".join(c.message for c in new_commits)
            refs = extract_close_refs(concatenated)
            surviving = filter_to_pool(refs, set(pool_numbers))
            for ref in surviving:
                if self._try_auto_close(ref, new_commits, iter_num):
                    auto_closures += 1

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

    def _try_auto_close(
        self,
        issue_number: int,
        new_commits: list[git_module.Commit],
        iter_num: int,
    ) -> bool:
        """Re-verify state and close the issue; return True on success.

        Mirrors bash auto-close logic at ``ralph/afk.sh:230-267``.
        """
        try:
            current = gh_module.issue_view(issue_number)
        except gh_module.GhError as exc:
            self._diag.warning(
                "gh issue view #%s during auto-close failed: %s",
                issue_number, exc,
            )
            return False
        if current.state == "CLOSED":
            return False
        if current.state != "OPEN":
            self._diag.warning(
                "issue #%s has unexpected state %r; not auto-closing",
                issue_number, current.state,
            )
            return False

        # Collect SHAs whose commit message contains a *closing* keyword
        # for this issue — uses the same parser as the pool whitelist
        # (``wrapper.extract_close_refs``) so we don't drift between
        # the two seams.
        ref_shas = [
            c.sha
            for c in new_commits
            if issue_number in extract_close_refs(c.message)
        ]
        if not ref_shas:
            # Defence-in-depth — should not happen in practice because
            # we only enter this branch for issues in ``surviving``,
            # which was derived from the same parser. But if a future
            # parser drift introduced an asymmetry, skipping the close
            # is safer than misattributing it to an arbitrary commit.
            self._diag.warning(
                "auto-close #%s: no commit in this iteration explicitly "
                "closes the issue via the closing-keyword parser; "
                "skipping to avoid misattribution",
                issue_number,
            )
            return False
        shas_str = " ".join(ref_shas)

        comment = (
            f"Implemented in {shas_str}.\n\n"
            f"Closed by the ralph_afk loop because the agent did not run "
            f"`gh issue close` itself this iteration (commit messages did "
            f"reference `Closes #{issue_number}`).\n\n"
            f"If this closure looks wrong, reopen with `gh issue reopen "
            f"{issue_number}` — the loop will not re-close it without a "
            f"new commit that references it."
        )
        try:
            gh_module.issue_close(issue_number, comment)
        except gh_module.GhError as exc:
            self._diag.warning(
                "gh issue close #%s failed: %s; issue remains open",
                issue_number, exc,
            )
            return False
        self._emit(
            events_module.WRAPPER_AUTO_CLOSE,
            iter_num=iter_num,
            issue=issue_number,
            sha=ref_shas[0] if ref_shas else "",
            shas=ref_shas,
        )
        return True

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
        # Preflight only the GitHub backend; PRDs would need its own
        # preflight (e.g. ``prds/`` directory existence) when #11 lands.
        if self._config.issue_source == "github":
            rc = self._preflight_github()
            if rc is not None:
                return rc

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
    client), drives the iteration loop, and returns the appropriate
    process exit code.

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
    if config.issue_source == "prds":
        # ISSUE_SOURCE=prds support lives in #11.
        print(
            "ralph-afk: ISSUE_SOURCE=prds is not implemented in this "
            "release (lands in issue #11). Use ISSUE_SOURCE=github.",
            file=sys.stderr,
        )
        return 2

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

    # 4) SDK client (lazy via the factory the tests monkeypatch). If
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
        diag=diag,
    )

    exit_code = 1
    try:
        try:
            with writers.event_log, writers.run_summary:
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

    return exit_code
