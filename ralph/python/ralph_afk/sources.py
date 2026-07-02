"""``ralph_afk.sources`` — abstract source of AFK-ready work items.

Defines the :class:`IssueSource` Protocol that abstracts over the two
ways the runner discovers AFK-ready work:

* :class:`GitHubIssueSource` — the default backend used when
  ``ISSUE_SOURCE=github`` (or unset). Discovers issues via ``gh issue
  list --label ready-for-agent``, applies the AFK-ready body
  discriminator (``^## What to build`` AND ``^## Acceptance criteria``), and
  backstops the agent's ``gh issue close`` step via
  :func:`ralph_afk.wrapper.extract_close_refs` + :func:`gh.issue_close`.
* :class:`PrdsIssueSource` — the legacy local-markdown backend used
  when ``ISSUE_SOURCE=prds``. Discovers files matching
  ``prds/<feature>/<NNN>-*.md`` (skipping ``prd.md`` and any path
  under ``done/``), applies the same AFK-ready discriminator, and
  performs **no** wrapper-side filesystem mutation on completion
  — the agent owns the ``git mv prds/<feat>/NNN-*.md
  prds/<feat>/done/`` per ``ralph/PROMPT.md``. The wrapper has no
  PRDs-side completion backstop.

Design notes:

* **The IssueSource Protocol is the seam.** :mod:`ralph_afk.loop` holds
  one ``source: IssueSource`` and calls only the three Protocol methods.
  Tests confirm structural conformance via ``isinstance(impl,
  IssueSource)`` runtime checks (Protocol is ``@runtime_checkable``).
* **Detection-only PRDs completion.** Early drafts proposed an active
  wrapper-side ``os.replace`` to move completed PRDs to ``done/``.
  The rubber-duck pass at design time flagged a hard bug: the move
  dirties the working tree. Under ADR-0004 that dirty tree is now
  absorbed by the runner Checkpoint rather than aborting, but the
  decision stands for a cleaner reason — the agent owns the
  move-and-commit so the closure is a real, attributable agent commit;
  the wrapper just discovers the resulting state on the next iteration.
* **stdlib + ``gh``/``git``/``wrapper`` modules only.** No SDK, no
  Rich, no peer-of-loop imports — the Protocol seam stays light.
* **Format helpers live with the impl that uses them.**
  :func:`_format_github_issue_block` lives here next to
  :class:`GitHubIssueSource` (not in :mod:`loop`) so the loop body
  doesn't carry source-specific knowledge.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from ralph_afk import gh as gh_module
from ralph_afk.git import Commit
from ralph_afk.wrapper import extract_close_refs, filter_to_pool

__all__ = [
    "AfkReadyItem",
    "Completion",
    "IssueSource",
    "GitHubIssueSource",
    "PrdsIssueSource",
    "is_afk_ready",
    "is_pr_afk_ready",
]

# Shared AFK-ready discriminator regexes (line-anchored, multiline).
# Body must contain BOTH ``^## What to build`` and ``^## Acceptance
# criteria`` to be considered AFK-ready. ``## Parent`` is OPTIONAL per the
# to-issues issue template (a slice with no parent issue omits the section),
# so it is deliberately NOT part of the discriminator — requiring it would
# silently drop validly-authored parent-less slices.
_RE_WHAT_TO_BUILD: re.Pattern[str] = re.compile(r"^## What to build", re.MULTILINE)
_RE_AC: re.Pattern[str] = re.compile(r"^## Acceptance criteria", re.MULTILINE)

# PR AFK-ready discriminator. Unlike issues (whose AFK shape lives in the
# body), a PR's agent brief is posted by ``/triage`` as a *comment* headed
# ``## Agent Brief`` (see .copilot/skills/triage/AGENT-BRIEF.md). We scan the
# body and every comment for that header.
_RE_AGENT_BRIEF: re.Pattern[str] = re.compile(r"^## Agent Brief", re.MULTILINE)

# PRDs file-name discriminator: ``<NNN>-<anything>.md`` where ``<NNN>``
# is one or more leading digits. Matches the issue spec at #11 verbatim:
# ``prds/<feature>/NNN-*.md``. ``prd.md`` is excluded by the leading-digit
# requirement; ``notes.md`` is excluded the same way.
_RE_PRDS_NAME: re.Pattern[str] = re.compile(r"^\d+-.*\.md$")


def is_afk_ready(body: str) -> bool:
    """Return ``True`` iff the body satisfies the AFK-ready discriminator.

    Args:
        body: Raw markdown body of an issue or local-markdown file.

    Returns:
        ``True`` if BOTH ``^## What to build`` and ``^## Acceptance
        criteria`` appear as line-anchored section headers in the body.
        ``## Parent`` is optional (a slice without a parent issue omits it
        per the to-issues template) and is intentionally not required, so
        validly-authored parent-less slices are still picked up. Both
        backends apply this identical check so a body that wouldn't be
        picked up via GitHub also won't be picked up via PRDs.
    """
    return bool(_RE_WHAT_TO_BUILD.search(body)) and bool(_RE_AC.search(body))


def is_pr_afk_ready(pr: gh_module.PullRequest) -> bool:
    """Return ``True`` iff a ``ready-for-agent`` PR carries an agent brief.

    Args:
        pr: A :class:`ralph_afk.gh.PullRequest` (typically fetched via
            :func:`ralph_afk.gh.pr_view`, so its ``comments`` are populated).

    Returns:
        ``True`` if a line-anchored ``## Agent Brief`` header appears in the
        PR body or in any comment. ``/triage`` posts the brief as a comment
        when it moves a PR to ``ready-for-agent``, so the comment scan is the
        load-bearing check; the body scan is a harmless robustness net.
    """
    if _RE_AGENT_BRIEF.search(pr.body or ""):
        return True
    return any(_RE_AGENT_BRIEF.search(c.body or "") for c in pr.comments)


@dataclass(frozen=True)
class AfkReadyItem:
    """A source-agnostic AFK-ready item ready to be embedded in the prompt.

    Attributes:
        ref: Source-native identifier — ``int`` (issue or PR number) for the
            GitHub backend, ``str`` (repo-relative POSIX file path) for
            the PRDs backend. The loop uses it for the auto-close pool
            whitelist (GitHub only) and for diagnostics/event payloads.
        title: Human-readable display title used only for diagnostics
            output. Not load-bearing.
        rendered_block: The full prompt block as the agent sees it —
            header + body + (GitHub) up-to-5 recent comments or (PRDs)
            file content, following the collector output format for the
            active source.
        kind: ``"issue"`` (default) or ``"pr"``. Distinguishes a GitHub
            pull request from an issue so the loop renders the right header,
            applies PR mode, routes progress detection (PR advances are
            detected by head-SHA, not local commits), and keeps the
            close-keyword backstop from acting on PR numbers.
        head_sha: For ``kind == "pr"``, the PR head commit SHA captured at
            collection time. The completion backstop re-reads the live head
            SHA after the iteration; a change means the agent pushed to the
            PR branch (progress) even though nothing landed on the base
            branch locally. Empty for issues / PRDs.
        labels: The source item's labels, lowest-cost carrier of the human
            eligibility assertions Parallel mode (ADR-0008) reads — e.g.
            ``parallel-safe``. Populated by the GitHub backend from the
            issue/PR labels; empty for the PRDs backend (local markdown has
            no labels). Eligibility is a label, never an inference.
    """

    ref: int | str
    title: str
    rendered_block: str
    kind: str = "issue"
    head_sha: str = ""
    labels: tuple[str, ...] = ()


@dataclass(frozen=True)
class Completion:
    """An item completed by the wrapper-side backstop this iteration.

    Attributes:
        ref: The ref of the :class:`AfkReadyItem` that was completed —
            same union shape (``int | str``).
        sha: Primary closing commit SHA — the first SHA in ``shas`` for
            the GitHub backend, ``""`` if no SHA attribution is
            applicable.
        shas: All commit SHAs the wrapper attributed this completion
            to. Empty tuple is allowed for sources that don't tie
            completions to specific commits.
        kind: ``"issue"`` (default) for a wrapper-closed issue, or ``"pr"``
            for a detected PR-branch advance. The loop emits a different
            event per kind (``wrapper.auto_close`` vs ``wrapper.pr.advanced``)
            but both count as iteration progress.
    """

    ref: int | str
    sha: str
    shas: tuple[str, ...] = ()
    kind: str = "issue"


@runtime_checkable
class IssueSource(Protocol):
    """Protocol abstracting GitHub-issues vs. local-markdown PRDs.

    The loop holds one :class:`IssueSource` and dispatches the three
    operations through it without knowing which backend is active.

    Both implementations satisfy this Protocol structurally — no
    explicit subclassing is required, but ``isinstance(impl,
    IssueSource)`` works because the decorator marks it
    ``@runtime_checkable``.
    """

    def preflight(self) -> int | None:
        """Source-specific health check before the first iteration.

        Returns:
            ``None`` on success (loop proceeds), or a non-zero exit code
            on failure (loop returns that code without entering the
            iteration body).
        """
        ...

    def collect_afk_ready(self) -> list[AfkReadyItem]:
        """Discover and return the current AFK-ready pool.

        Empty list is the natural "no work" signal — the loop exits 0
        for either backend when this returns ``[]``.
        """
        ...

    def handle_completions(
        self,
        *,
        pool: list[AfkReadyItem],
        new_commits: list[Commit],
    ) -> list[Completion]:
        """Apply the source-specific completion backstop.

        Args:
            pool: The AFK-ready items the iteration was working from.
                Used as a whitelist so a stray closing-keyword reference
                doesn't act on unrelated issues.
            new_commits: Commits the agent produced this iteration
                (commits between the pre-iteration ``HEAD`` SHA and the
                post-iteration ``HEAD`` SHA).

        Returns:
            A list of :class:`Completion` for items the wrapper acted
            on this iteration. Empty list means no wrapper-side
            completions (which is the only outcome for the PRDs
            backend).
        """
        ...

    def comment(self, ref: int | str, body: str) -> None:
        """Post one automated breadcrumb comment on ``ref`` without resolving it.

        Used by Parallel-mode Integration recovery (#63, ADR-0009): when
        auto-resolution exhausts its K=3 attempts the runner leaves exactly one
        comment on the issue and defers it to a later serial **Iteration**. A
        no-op for a source with no per-item comment channel (the PRDs backend).
        """
        ...


# --------------------------------------------------------------------------- #
# GitHub backend                                                              #
# --------------------------------------------------------------------------- #


class GitHubIssueSource:
    """AFK-ready items backed by GitHub issues (and optionally PRs) via ``gh``.

    Collects AFK-ready issues, verifies readiness, and auto-closes completed
    items. Commit closure keywords are parsed using
    :func:`ralph_afk.wrapper.extract_close_refs` so the parser is shared
    between the pool whitelist and the SHA attribution.

    When ``include_prs`` is set, the source *also* collects ``ready-for-agent``
    pull requests that carry an agent brief (``## Agent Brief`` comment) and
    detects progress on them by comparing the PR head SHA across the
    iteration (an agent works a PR on its own branch via ``gh pr checkout``,
    so the pushed commits never reach the base branch locally). The wrapper
    never closes or merges a PR — a human merges it in QA.
    """

    def __init__(
        self,
        diag: logging.Logger,
        *,
        gh: gh_module.GitHubClient,
        include_prs: bool = False,
    ) -> None:
        """Construct a backend that logs diagnostics via ``diag``.

        Args:
            diag: Diagnostics logger.
            gh: The injected :class:`ralph_afk.gh.GitHubClient` seam — the raw
                GitHub mechanics (list / view / close). Production wiring injects
                a :class:`ralph_afk.gh.SubprocessGitHubClient` (via
                :func:`ralph_afk.loop._make_github_client`); the sources tests
                substitute ``tests.fakes.FakeGitHubClient``. The source owns the
                closure **policy** (what counts as **Strike** progress); the
                client only provides mechanics.
            include_prs: When ``True``, ``ready-for-agent`` pull requests with
                an agent brief join the AFK-ready pool and get head-SHA
                progress detection. Defaults to ``False`` so the default
                GitHub-issues behaviour is byte-for-byte unchanged.
        """
        self._diag = diag
        self._gh = gh
        self._include_prs = include_prs

    def preflight(self) -> int | None:
        """Verify ``gh`` is on PATH, authenticated, and resolves a repo.

        GitHub mode requires ``gh`` to be available, authenticated, and repo-scoped.
        """
        try:
            authed = self._gh.auth_status()
        except gh_module.GhError as exc:
            self._diag.error(
                "gh preflight failed: %s. Install `gh` from "
                "https://cli.github.com/.",
                exc,
            )
            return 1
        if not authed:
            self._diag.error(
                "gh is not authenticated. Run `gh auth login` and re-run "
                "ralph-afk."
            )
            return 1
        try:
            repo = self._gh.repo_view()
        except gh_module.GhError as exc:
            self._diag.error(
                "gh repo view failed: %s. Ralph-afk must be run from inside a "
                "clone of a GitHub repository.",
                exc,
            )
            return 1
        self._diag.info("preflight ok: %s", repo.nwo)
        return None

    def collect_afk_ready(self) -> list[AfkReadyItem]:
        """Fetch the AFK-ready GitHub-issue pool with comment enrichment.

        Two-pass: list first (cheap), filter by body discriminator
        BEFORE the N+1 ``issue_view`` enrichment so we don't pay the
        round-trip for PRD-style ready-for-agent issues that don't
        satisfy the AFK shape.
        """
        try:
            candidates = self._gh.issue_list("ready-for-agent")
        except gh_module.GhError as exc:
            self._diag.error("gh issue list failed: %s", exc)
            return []

        ready_candidates = [i for i in candidates if is_afk_ready(i.body or "")]

        items: list[AfkReadyItem] = []
        for issue in ready_candidates:
            try:
                full = self._gh.issue_view(issue.number)
            except gh_module.GhError as exc:
                self._diag.warning(
                    "gh issue view #%s failed: %s; skipping for this iteration",
                    issue.number,
                    exc,
                )
                continue
            if not is_afk_ready(full.body or ""):
                continue
            items.append(
                AfkReadyItem(
                    ref=full.number,
                    title=full.title,
                    rendered_block=_format_github_issue_block(full),
                    labels=tuple(full.labels),
                )
            )

        if self._include_prs:
            items.extend(self._collect_afk_ready_prs())
        return items

    def _collect_afk_ready_prs(self) -> list[AfkReadyItem]:
        """Fetch the AFK-ready PR pool (``ready-for-agent`` + agent brief).

        Two-pass like the issue collector, but the filter order is forced:
        a PR's agent brief lives in a *comment*, which ``gh pr list`` does not
        return, so we must ``pr_view`` each ``ready-for-agent`` candidate to
        pull its comments before applying :func:`is_pr_afk_ready`. The
        ``ready-for-agent`` PR set is normally tiny, so the N+1 is cheap.
        """
        try:
            candidates = self._gh.pr_list("ready-for-agent")
        except gh_module.GhError as exc:
            self._diag.error("gh pr list failed: %s", exc)
            return []

        items: list[AfkReadyItem] = []
        for pr in candidates:
            try:
                full = self._gh.pr_view(pr.number)
            except gh_module.GhError as exc:
                self._diag.warning(
                    "gh pr view #%s failed: %s; skipping for this iteration",
                    pr.number,
                    exc,
                )
                continue
            if not is_pr_afk_ready(full):
                continue
            items.append(
                AfkReadyItem(
                    ref=full.number,
                    title=full.title,
                    rendered_block=_format_github_pr_block(full),
                    kind="pr",
                    head_sha=full.head_sha,
                )
            )
        return items

    def handle_completions(
        self,
        *,
        pool: list[AfkReadyItem],
        new_commits: list[Commit],
    ) -> list[Completion]:
        """Apply the wrapper-side completion backstops.

        Two independent backstops, both reported as :class:`Completion` so
        the loop counts them as iteration progress:

        * **PR advances** (only when ``include_prs``): for each ``kind ==
          "pr"`` pool item, re-read the live head SHA and compare it to the
          SHA captured at collection time. A change means the agent pushed
          to the PR branch — progress — even though no commit reached the
          base branch locally. The wrapper never closes or merges the PR.
          This runs **regardless of** ``new_commits`` because PR work lands
          on the PR branch, not the base branch.
        * **Issue closures:** for each new base-branch commit, extract
          closing-keyword refs (``Closes #N`` / ``Fixes #N`` / ``Resolves
          #N``), filter to the iteration's *issue* pool whitelist, re-verify
          state via the injected client's ``issue_view``, then close via its
          ``issue_close``. Per-issue try/except — one failure doesn't
          lose the rest of the iteration's bookkeeping.
        """
        completions: list[Completion] = []
        if self._include_prs:
            completions.extend(self._detect_pr_advances(pool))
        completions.extend(self._handle_issue_closures(pool, new_commits))
        return completions

    def comment(self, ref: int | str, body: str) -> None:
        """Post one breadcrumb comment via the injected client (non-fatal).

        Only integer issue refs are commentable; a non-int ref, or a client
        failure, is logged and swallowed so a failed breadcrumb never aborts the
        Wave barrier — the issue simply falls through to a serial **Iteration**
        without the note.
        """
        if not isinstance(ref, int):
            return
        try:
            self._gh.issue_comment(ref, body)
        except gh_module.GhError as exc:
            self._diag.warning(
                "gh issue comment #%s failed: %s; continuing without breadcrumb",
                ref,
                exc,
            )

    def _detect_pr_advances(
        self, pool: list[AfkReadyItem]
    ) -> list[Completion]:
        """Return a :class:`Completion` for each open PR whose head advanced."""
        completions: list[Completion] = []
        for item in pool:
            if item.kind != "pr" or not isinstance(item.ref, int):
                continue
            if not item.head_sha:
                # No baseline SHA captured at collection time; can't compare.
                continue
            try:
                current = self._gh.pr_view(item.ref)
            except gh_module.GhError as exc:
                self._diag.warning(
                    "gh pr view #%s during PR-advance check failed: %s",
                    item.ref,
                    exc,
                )
                continue
            # Only an OPEN PR is advanceable work. A MERGED/CLOSED PR is a
            # human's doing, not our completion to record.
            if current.state != "OPEN":
                continue
            if current.head_sha and current.head_sha != item.head_sha:
                completions.append(
                    Completion(
                        ref=item.ref,
                        sha=current.head_sha,
                        shas=(current.head_sha,),
                        kind="pr",
                    )
                )
        return completions

    def _handle_issue_closures(
        self,
        pool: list[AfkReadyItem],
        new_commits: list[Commit],
    ) -> list[Completion]:
        """Close pool *issues* referenced by closing keywords in new commits."""
        if not new_commits:
            return []

        # Restrict to issues: a ``Closes #<prnum>`` must never trigger
        # ``gh issue close`` on a PR sharing that number space.
        pool_numbers: set[int] = {
            item.ref
            for item in pool
            if item.kind == "issue" and isinstance(item.ref, int)
        }
        if not pool_numbers:
            return []

        concatenated = "\n".join(c.message for c in new_commits)
        refs = extract_close_refs(concatenated)
        surviving = filter_to_pool(refs, pool_numbers)

        completions: list[Completion] = []
        for ref in surviving:
            completion = self._try_close_one(ref, new_commits)
            if completion is not None:
                completions.append(completion)
        return completions

    def _try_close_one(
        self,
        issue_number: int,
        new_commits: list[Commit],
    ) -> Completion | None:
        """Re-verify state and close one issue; return the completion or None."""
        ref_shas: tuple[str, ...] = tuple(
            c.sha
            for c in new_commits
            if issue_number in extract_close_refs(c.message)
        )
        if not ref_shas:
            # Defence-in-depth: should not happen since ``surviving``
            # came from the same parser. But if a future parser drift
            # introduced an asymmetry, skipping is safer than
            # misattributing.
            self._diag.warning(
                "auto-close #%s: no commit in this iteration explicitly "
                "closes the issue via the closing-keyword parser; "
                "skipping to avoid misattribution",
                issue_number,
            )
            return None

        try:
            current = self._gh.issue_view(issue_number)
        except gh_module.GhError as exc:
            self._diag.warning(
                "gh issue view #%s during auto-close failed: %s",
                issue_number,
                exc,
            )
            return None
        if current.state == "CLOSED":
            return None
        if current.state != "OPEN":
            self._diag.warning(
                "issue #%s has unexpected state %r; not auto-closing",
                issue_number,
                current.state,
            )
            return None

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
            self._gh.issue_close(issue_number, comment)
        except gh_module.GhError as exc:
            self._diag.warning(
                "gh issue close #%s failed: %s; issue remains open",
                issue_number,
                exc,
            )
            return None

        return Completion(ref=issue_number, sha=ref_shas[0], shas=ref_shas)


def _format_github_issue_block(issue: gh_module.Issue) -> str:
    """Render one GitHub issue as the prompt block.

    Emits a header line, blank line, body, then up to 5 newest-first
    comments behind a separator.
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

    comment_lines = [f"[{c.created_at} @{c.author}] {c.body}" for c in recent]
    return (
        f"{header}\n{body}\n\n"
        f"--- Recent comments (newest first, up to 5) ---\n"
        + "\n\n".join(comment_lines)
    )


def _format_github_pr_block(pr: gh_module.PullRequest) -> str:
    """Render one GitHub pull request as the prompt block.

    Parallel to :func:`_format_github_issue_block`, but the header reads
    ``=== PR #N: <title> [labels: ...] (branch: <head_branch>) ===`` so the
    agent can tell a PR from an issue and apply PR mode per
    ``ralph/PROMPT.md`` (check out the branch, finish the diff, push, do
    **not** close/merge, return to the base branch). The agent brief lives
    in the comments, so the up-to-5 recent comments are always included.
    """
    labels_str = ", ".join(pr.labels)
    header = (
        f"=== PR #{pr.number}: {pr.title} "
        f"[labels: {labels_str}] (branch: {pr.head_branch}) ==="
    )
    body = pr.body or ""

    recent = sorted(
        pr.comments,
        key=lambda c: c.created_at,
        reverse=True,
    )[:5]
    if not recent:
        return f"{header}\n{body}"

    comment_lines = [f"[{c.created_at} @{c.author}] {c.body}" for c in recent]
    return (
        f"{header}\n{body}\n\n"
        f"--- Recent comments (newest first, up to 5) ---\n"
        + "\n\n".join(comment_lines)
    )


# --------------------------------------------------------------------------- #
# PRDs (local-markdown) backend                                               #
# --------------------------------------------------------------------------- #


class PrdsIssueSource:
    """AFK-ready items backed by local-markdown ``prds/<feature>/<NNN>-*.md`` files.

    The AFK-ready body discriminator (``^## What to build`` AND
    ``^## Acceptance criteria``) is applied identically to the GitHub
    backend, per issue #11 acceptance criteria, so a stray non-AFK file
    under ``prds/<feature>/`` is silently skipped rather than fed to the
    agent.

    Discovery rules:

    * Walk ``<repo_root>/prds/`` (returns ``[]`` if the directory
      does not exist).
    * Iterate direct subdirectories of ``prds/`` — these are the
      "feature" directories. Skip a top-level directory literally
      named ``done`` (would be unusual but cheap to guard against).
    * Within each feature directory, list immediate files (not
      sub-directories — so any ``done/`` subdirectory is naturally
      ignored) whose name matches the regex ``^\\d+-.*\\.md$``.
      This excludes ``prd.md`` (no digit prefix), ``notes.md`` (no
      digit prefix), and arbitrary non-numbered markdown files.
    * Sort the combined results by their repo-relative POSIX path so
      cross-feature order is stable and within-feature order is the
      same lexicographic order POSIX ``sort`` would give.

    Completion semantics: **detection-only**. The agent is responsible
    for ``git mv prds/<feat>/NNN-*.md prds/<feat>/done/`` per
    ``ralph/PROMPT.md``'s local-markdown mode contract.
    :meth:`handle_completions` always returns ``[]``. The agent's
    ``git mv`` commit IS the closure signal; next iteration's discovery
    automatically excludes ``done/``. (A wrapper-side move would dirty
    the tree, which ADR-0004's Checkpoint would now capture rather than
    abort on — but the agent owning the commit keeps the closure
    attributable.)
    """

    def __init__(self, repo_root: Path, diag: logging.Logger) -> None:
        """Construct a backend rooted at ``repo_root``.

        Args:
            repo_root: Repository root :class:`Path`. Used as the
                anchor for the ``prds/`` walk and for computing
                repo-relative POSIX paths in :attr:`AfkReadyItem.ref`.
            diag: Diagnostics logger; warnings are emitted on
                unreadable markdown files.
        """
        self._repo_root = repo_root
        self._diag = diag

    def preflight(self) -> int | None:
        """No-op for PRDs mode.

        PRDs mode has no external preflight. An empty / missing
        ``prds/`` directory is not a preflight failure — it just produces
        an empty pool from :meth:`collect_afk_ready`, which the loop treats
        as clean-exit-zero.
        """
        return None

    def collect_afk_ready(self) -> list[AfkReadyItem]:
        """Walk ``prds/<feature>/<NNN>-*.md`` files; apply AFK discriminator."""
        prds_dir = self._repo_root / "prds"
        if not prds_dir.is_dir():
            return []

        items: list[tuple[str, AfkReadyItem]] = []
        for feature_dir in sorted(
            prds_dir.iterdir(), key=lambda p: p.name
        ):
            if not feature_dir.is_dir():
                continue
            if feature_dir.name == "done":
                # Defensive: a top-level prds/done/ wouldn't be a
                # feature directory anyway.
                continue
            for md_path in sorted(feature_dir.iterdir(), key=lambda p: p.name):
                if not md_path.is_file():
                    continue
                if md_path.name == "prd.md":
                    continue
                if not _RE_PRDS_NAME.match(md_path.name):
                    continue
                try:
                    rel_path = md_path.relative_to(self._repo_root).as_posix()
                except ValueError:
                    # Symlink chicanery (md_path resolves outside
                    # repo_root). Skip rather than potentially escape
                    # the worktree.
                    self._diag.warning(
                        "prds: %s does not resolve under repo_root; skipping",
                        md_path,
                    )
                    continue
                try:
                    body = md_path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError) as exc:
                    self._diag.warning(
                        "prds: could not read %s: %s; skipping", rel_path, exc,
                    )
                    continue
                if not is_afk_ready(body):
                    continue
                rendered = f"=== {rel_path} ===\n{body}"
                items.append(
                    (
                        rel_path,
                        AfkReadyItem(
                            ref=rel_path,
                            title=rel_path,
                            rendered_block=rendered,
                        ),
                    )
                )
        # Sort by repo-relative POSIX path for stable cross-feature
        # ordering; within a feature dir, the inner loop's
        # name-keyed sort already produced numerical order with
        # zero-padded NNN.
        items.sort(key=lambda x: x[0])
        return [item for _, item in items]

    def handle_completions(
        self,
        *,
        pool: list[AfkReadyItem],
        new_commits: list[Commit],
    ) -> list[Completion]:
        """Always returns ``[]`` — the agent owns the ``git mv``.

        See class docstring for the design rationale.
        """
        # Suppress unused-argument lint warnings without making the
        # arguments non-keyword (the Protocol contract requires the
        # parameter names so callers can keyword-call).
        _ = pool
        _ = new_commits
        return []

    def comment(self, ref: int | str, body: str) -> None:
        """No-op — the PRDs backend has no per-issue comment channel.

        Integration recovery (#63) is Parallel-mode / GitHub-only; the local
        markdown backend never runs it, so the breadcrumb has nowhere to go.
        """
        _ = ref
        _ = body
