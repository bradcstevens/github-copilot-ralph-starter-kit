"""Reusable test doubles (fakes) for the ``ralph_afk`` seams.

Created for issue #46 (the ``git`` seam) and extended in #47 (the ``gh`` seam).
A *fake* is a working in-memory implementation of a Protocol seam — richer than
a one-off stub — so a test substitutes a single object instead of monkeypatching
a dozen module functions. Each fake satisfies its Protocol structurally:
``isinstance(fake, GitClient)`` / ``isinstance(fake, GitHubClient)`` hold because
the Protocols are ``@runtime_checkable``.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Mapping, Sequence

from ralph_afk.gate import GateResult, LoopFailure
from ralph_afk.gh import GhError, Issue, PullRequest, Repo
from ralph_afk.git import Commit, GitError


class FakeGitClient:
    """Stateful in-memory :class:`~ralph_afk.git.GitClient` for loop tests.

    Models a linear commit log plus dirty / untracked flags so the read methods
    (:meth:`head_sha` / :meth:`commits_between` / :meth:`recent_commits` /
    :meth:`range_count`) stay consistent by construction. Records the write
    methods (:meth:`add_all` / :meth:`commit` / :meth:`push` / :meth:`switch`)
    for assertions, and offers :meth:`simulate_agent_commit` to script the
    agent's work between the loop's pre- and post-iteration ``head_sha`` reads.
    The ~139 monkeypatch lines the loop tests used to carry collapse into
    constructing one of these.

    **Checkpoint-exclusion invariant (load-bearing for the Strike rule).** A
    runner Checkpoint — authored via :meth:`commit` *after* the loop reads the
    post-iteration ``head`` — must not appear in ``commits_between(pre, head)``
    (a Checkpoint is not progress; an agent commit is). This holds *by
    construction*: :meth:`commits_between` slices the linear log positionally by
    the explicit ``pre`` / ``head`` SHAs, so a commit appended after ``head`` was
    captured falls outside the range. :meth:`simulate_agent_commit` advances the
    head that ``commits_between`` sees; a Checkpoint :meth:`commit` does not
    appear in the range for a ``head`` read before it.

    ``dirty`` / ``untracked`` are plain test-controlled booleans; :meth:`commit`
    does **not** clear them (a real agent re-dirties the tree each iteration), so
    a multi-iteration test that leaves ``dirty=True`` Checkpoints every iteration.

    **Worktree lifecycle (#59 / ADR-0008).** :meth:`add_worktree` models a
    Parallel-mode **Lane**: it returns a child :class:`FakeGitClient` whose log is
    a snapshot copy of this client's (branch-from-base) and which then advances its
    **own** per-worktree log independently — consistent-by-construction, so a
    commit in one Lane never leaks into another or the main worktree.
    :attr:`worktree_adds` / :attr:`worktree_removes` are spies and
    :attr:`active_worktrees` lists the live ones, for orchestrator assertions.

    **Integration (#62 / ADR-0009).** :meth:`merge` lands a Lane branch's own
    commits onto this (base) log and :meth:`delete_branch` drops an integrated
    branch; the branch registry outlives worktree teardown so both work at the
    Wave barrier. :attr:`merge_calls` / :attr:`branch_deletes` are spies.
    """

    def __init__(
        self,
        root: Path,
        *,
        commits: Sequence[Commit] | None = None,
        dirty: bool = False,
        untracked: bool = False,
        branch: str | None = "main",
        commit_error: GitError | None = None,
        push_error: GitError | None = None,
        sha_prefix: str = "face",
        merge_conflicts: Sequence[int] | None = None,
    ) -> None:
        self._root = Path(root)
        self._sha_counter = 0
        self._sha_prefix = sha_prefix
        if commits is None:
            commits = [
                Commit(
                    sha=self._next_sha(),
                    subject="root commit",
                    body="",
                    date="2026-01-01",
                )
            ]
        self._log: list[Commit] = list(commits)
        # Test-controlled worktree state (read by is_dirty / has_untracked).
        self.dirty = dirty
        self.untracked = untracked
        self.branch = branch
        # Injected failures (None = the happy path).
        self.commit_error = commit_error
        self.push_error = push_error
        # Write spies.
        self.add_all_calls = 0
        self.commit_messages: list[str] = []
        self.push_calls = 0
        self.switch_calls: list[str] = []
        # Parallel-mode worktree lifecycle (#59 / ADR-0008): per-worktree child
        # clients plus add/remove spies, kept consistent-by-construction.
        self._worktree_seq = 0
        self._worktrees: dict[Path, FakeGitClient] = {}
        self.worktree_adds: list[tuple[Path, str, str]] = []
        self.worktree_removes: list[Path] = []
        # Integration (#62 / ADR-0009): a branch registry keyed by Lane branch
        # name, populated on add_worktree and — unlike _worktrees — **kept** past
        # remove_worktree (a branch outlives its worktree as a breadcrumb), so
        # merge/delete_branch can land or drop a Lane branch after teardown.
        self._branches: dict[str, FakeGitClient] = {}
        self.merge_calls: list[str] = []
        self.branch_deletes: list[str] = []
        # Integration recovery (#63 / ADR-0009). ``merge_conflicts`` scripts the
        # issue numbers whose **Lane** branch raises on :meth:`merge` (models a
        # conflicting landing) so a test drives the abort + auto-resolution path.
        # Matched on the Lane branch shape (``.../issue-<N>``) and NOT on the
        # auto-resolution *integration* branch (``.../integrate/issue-<N>``), so
        # the resolved branch still merges cleanly. A stack of per-merge deltas
        # (branch + the SHAs that landing appended) lets :meth:`revert_merge` pop
        # the last landing and restore the pre-merge base — the net effect of
        # ``git revert -m 1`` in this linear-log model. :attr:`reverts` /
        # :attr:`merge_aborts` are spies.
        self._merge_conflict_issues: set[int] = set(merge_conflicts or ())
        self._merge_deltas: list[tuple[str, list[str]]] = []
        self.reverts: list[str] = []
        self.merge_aborts: int = 0

    @property
    def root(self) -> Path:
        """The repository root this client is bound to (parity with the adapter)."""
        return self._root

    # -- internal helpers --------------------------------------------------

    def _next_sha(self) -> str:
        self._sha_counter += 1
        # 40-char hex with a distinctive ``face`` prefix so auto-generated SHAs
        # never collide with the explicit SHAs tests pass to
        # simulate_agent_commit (e.g. "abcdef..." / "a" * 40 / "cap0...").
        # Worktree children get a distinct ``sha_prefix`` (``wt1``, ``wt2``, ...)
        # so per-Lane auto-SHAs never collide across worktrees either.
        return f"{self._sha_prefix}{self._sha_counter:036x}"

    def _index(self, sha: str) -> int:
        for i, commit in enumerate(self._log):
            if commit.sha == sha:
                return i
        raise GitError(["git", "rev-parse", sha], 128, f"bad revision {sha!r}")

    # -- GitClient mechanics ----------------------------------------------

    def head_sha(self) -> str:
        if not self._log:
            raise GitError(["git", "rev-parse", "HEAD"], 128, "no commits yet")
        return self._log[-1].sha

    def is_dirty(self) -> bool:
        return self.dirty

    def has_untracked(self) -> bool:
        return self.untracked

    def add_all(self) -> None:
        self.add_all_calls += 1

    def commit(self, message: str) -> str:
        self.commit_messages.append(message)
        if self.commit_error is not None:
            raise self.commit_error
        lines = message.split("\n")
        subject = lines[0]
        body = "\n".join(lines[2:]) if len(lines) > 2 else ""
        commit = Commit(
            sha=self._next_sha(),
            subject=subject,
            body=body.rstrip("\n"),
            date="2026-05-16",
        )
        self._log.append(commit)
        return commit.sha

    def push(self) -> None:
        self.push_calls += 1
        if self.push_error is not None:
            raise self.push_error

    def current_branch(self) -> str | None:
        return self.branch

    def switch(self, branch: str) -> None:
        self.switch_calls.append(branch)
        self.branch = branch

    def commits_between(self, pre: str, head: str) -> list[Commit]:
        if pre == head:
            return []
        pre_idx = self._index(pre)
        head_idx = self._index(head)
        # Commits after ``pre`` up to and including ``head``, newest-first
        # (mirroring ``git log`` default order).
        window = self._log[pre_idx + 1 : head_idx + 1]
        return list(reversed(window))

    def recent_commits(self, n: int) -> list[Commit]:
        if n <= 0:
            return []
        return list(reversed(self._log[-n:]))

    def range_count(self, pre: str, head: str) -> int:
        return len(self.commits_between(pre, head))

    def add_worktree(self, path: Path, *, branch: str, base: str) -> FakeGitClient:
        """Model ``git worktree add -b <branch> <path> <base>``.

        Records the add in :attr:`worktree_adds` and returns a **child**
        :class:`FakeGitClient` bound to ``path`` on ``branch``. Branch-from-base
        is modelled consistent-by-construction: the child's log is a snapshot copy
        of this client's current log (this fake's single linear log represents the
        base branch), so the child starts at the base head and then advances its
        **own** per-worktree log independently — a commit in one Lane never
        appears in another or in the main worktree. The child carries a distinct
        ``sha_prefix`` (``wt1``, ``wt2``, ...) so auto-generated SHAs never collide
        across worktrees.
        """
        wt_path = Path(path)
        self.worktree_adds.append((wt_path, branch, base))
        self._worktree_seq += 1
        child = FakeGitClient(
            wt_path,
            commits=list(self._log),
            branch=branch,
            sha_prefix=f"wt{self._worktree_seq}",
        )
        self._worktrees[wt_path] = child
        self._branches[branch] = child
        return child

    def remove_worktree(self, path: Path, *, force: bool = False) -> None:
        """Model ``git worktree remove`` — drop the child, keep the branch.

        Records the teardown in :attr:`worktree_removes` and forgets the child
        worktree, but **keeps** the branch in :attr:`_branches` — as with real git,
        removing a worktree leaves its branch a breadcrumb, so Integration can
        still :meth:`merge` (or later :meth:`delete_branch`) it after the barrier.
        """
        wt_path = Path(path)
        self.worktree_removes.append(wt_path)
        self._worktrees.pop(wt_path, None)

    @property
    def active_worktrees(self) -> list[Path]:
        """Paths of the worktrees currently live (added and not yet removed)."""
        return list(self._worktrees)

    def worktree_client(self, path: Path) -> FakeGitClient | None:
        """Return the live child client bound to ``path`` (or ``None``).

        A public accessor over :attr:`_worktrees` so a Parallel-mode
        orchestrator test can reach into a Lane's worktree — while it is still
        live, i.e. after :meth:`add_worktree` and before :meth:`remove_worktree`
        — and drive :meth:`simulate_agent_commit` on the right per-Lane log.
        Complements :attr:`active_worktrees` (which lists the paths).
        """
        return self._worktrees.get(Path(path))

    def _branch_conflicts(self, branch: str) -> bool:
        """Whether a scripted merge conflict applies to ``branch`` (#63).

        Matches only the **Lane** branch for a scripted issue
        (``.../issue-<N>``), never its auto-resolution *integration* branch
        (``.../integrate/issue-<N>``) — so a conflicting Lane's resolved branch
        still merges cleanly.
        """
        if "/integrate/" in branch:
            return False
        return any(
            branch.endswith(f"/issue-{n}") for n in self._merge_conflict_issues
        )

    def merge(self, branch: str) -> None:
        """Model ``git merge --no-ff <branch>`` — land a Lane branch on base.

        Looks up the branch's child client (registered by :meth:`add_worktree` and
        retained past :meth:`remove_worktree`) and appends its **own** commits —
        those whose SHA is not already in this base log — advancing ``head_sha`` and
        making them visible to ``commits_between(pre, post)`` so Integration can read
        the landed ``Closes #N`` commit and drive closure. Records the branch in
        :attr:`merge_calls` and the appended SHAs as a revertable delta (#63).

        Raises:
            GitError: If ``branch`` is unknown (never added, or already deleted),
                or if ``branch`` is scripted to conflict via ``merge_conflicts``
                (models a conflicting landing — nothing is appended).
        """
        child = self._branches.get(branch)
        if child is None:
            raise GitError(["git", "merge", "--no-ff", branch], 1, f"merge: {branch}")
        if self._branch_conflicts(branch):
            # A scripted conflict: base is left mid-merge (nothing landed) for
            # the caller to :meth:`abort_merge`.
            raise GitError(
                ["git", "merge", "--no-ff", branch], 1, f"conflict: {branch}"
            )
        self.merge_calls.append(branch)
        known = {commit.sha for commit in self._log}
        appended: list[str] = []
        for commit in child._log:
            if commit.sha not in known:
                self._log.append(commit)
                appended.append(commit.sha)
        self._merge_deltas.append((branch, appended))

    def revert_merge(self) -> None:
        """Model ``git revert -m 1 --no-edit HEAD`` — undo the last landing (#63).

        Pops the most recent :meth:`merge` delta and removes those commits from
        this base log, so ``head_sha`` returns to the pre-merge base (green) — the
        net effect of a real ``git revert -m 1`` in this linear-log model, without
        modelling the extra inverse commit git would append. Records the reverted
        branch in :attr:`reverts`.

        Raises:
            GitError: If there is no landing to revert (``HEAD`` is not a merge).
        """
        if not self._merge_deltas:
            raise GitError(
                ["git", "revert", "-m", "1", "--no-edit", "HEAD"],
                1,
                "no merge to revert",
            )
        branch, appended = self._merge_deltas.pop()
        removed = set(appended)
        self._log = [c for c in self._log if c.sha not in removed]
        self.reverts.append(branch)

    def abort_merge(self) -> None:
        """Model ``git merge --abort`` — unwind a conflicted merge (#63).

        A conflicting :meth:`merge` appended nothing to the base log (it raised),
        so the base is already at its pre-merge state; this only records the
        abort in :attr:`merge_aborts` for assertions.
        """
        self.merge_aborts += 1

    def delete_branch(self, branch: str) -> None:
        """Model ``git branch -D <branch>`` — drop an integrated Lane branch.

        Removes the branch from the registry and records it in
        :attr:`branch_deletes`.

        Raises:
            GitError: If ``branch`` is unknown (never added, or already deleted).
        """
        if branch not in self._branches:
            raise GitError(["git", "branch", "-D", branch], 1, f"not found: {branch}")
        del self._branches[branch]
        self.branch_deletes.append(branch)

    # -- test scripting ----------------------------------------------------

    def simulate_agent_commit(
        self,
        *,
        subject: str,
        body: str = "",
        sha: str | None = None,
        date: str = "2026-05-16",
    ) -> str:
        """Append an agent commit, advancing ``head_sha`` / ``commits_between``.

        Models the agent's own work between the loop's pre- and post-iteration
        head reads. The returned SHA is what the post-iteration ``head_sha`` sees
        and what ``commits_between(pre, head)`` includes — unlike a Checkpoint
        :meth:`commit`, which lands *after* ``head`` is read and so is excluded.

        Args:
            subject: Commit subject line (may carry a ``Closes #N`` keyword).
            body: Commit body (may carry a ``Closes #N`` keyword).
            sha: Explicit SHA for the commit; auto-generated when omitted.
            date: ``--date=short`` string for the commit.

        Returns:
            The SHA of the appended agent commit.
        """
        commit = Commit(
            sha=sha if sha is not None else self._next_sha(),
            subject=subject,
            body=body,
            date=date,
        )
        self._log.append(commit)
        return commit.sha


def _state_matches(actual: str, wanted: str) -> bool:
    """Match a stored ``state`` against a ``gh ... list --state`` filter value.

    ``gh`` accepts ``all`` (everything) plus case-insensitive lifecycle states
    (``open`` / ``closed``, and ``merged`` for PRs). Stored states are upper-case
    (``"OPEN"`` / ``"CLOSED"`` / ``"MERGED"``, matching the value objects).
    """
    if wanted == "all":
        return True
    return actual.upper() == wanted.upper()


class FakeGitHubClient:
    """Stateful in-memory :class:`~ralph_afk.gh.GitHubClient` for source/loop tests.

    Extends the seam pattern #46 established for ``git`` to GitHub (#47). Models a
    per-number **store** of issues and pull requests so the read methods
    (:meth:`issue_list` / :meth:`issue_view` / :meth:`pr_list` / :meth:`pr_view`)
    stay consistent by construction, records the mutating :meth:`issue_close` for
    assertions, and injects per-method :exc:`~ralph_afk.gh.GhError` failures so a
    test drives the source's resilience paths without monkeypatching. The ~57
    monkeypatch lines the sources tests used to carry collapse into constructing
    one of these.

    **issue_close is a recorded mechanic, never a policy.** It appends to
    :attr:`issue_close_calls` and flips the stored issue's ``state`` to
    ``"CLOSED"`` (so a later :meth:`issue_view` sees the close land, exactly as
    the real ``gh`` does) — it does *not* decide whether the closure counts as
    **Strike** progress or interpret close-keywords. That policy stays in
    :class:`ralph_afk.sources.GitHubIssueSource`, never in the client.

    **List and single-view are independently scriptable.** ``issue_list`` /
    ``pr_list`` return the seeded stores (filtered by state); ``issue_view`` /
    ``pr_view`` return the same objects by number *unless* overridden. Pass
    ``issue_views={n: issue}`` to make :meth:`issue_view` return a *different*
    body than the list did — this exercises the source's re-verify-on-full-body
    path (list body carries the discriminator, the full view does not). Per-number
    ``*_view_errors`` / ``issue_close_errors`` inject a :exc:`~ralph_afk.gh.GhError`
    for one number while the rest of the pool proceeds (the source's resilience
    paths); the whole-operation ``auth_status_error`` / ``repo_view_error`` /
    ``issue_list_error`` / ``pr_list_error`` fail a list/preflight call outright.

    Unlike :class:`FakeGitClient` there is **no root / cwd binding** — ``gh`` runs
    in the process cwd — so the constructor takes no ``root`` and the methods keep
    their real signatures.
    """

    def __init__(
        self,
        *,
        authed: bool = True,
        repo: Repo | None = None,
        issues: Sequence[Issue] = (),
        prs: Sequence[PullRequest] = (),
        issue_views: Mapping[int, Issue] | None = None,
        auth_status_error: GhError | None = None,
        repo_view_error: GhError | None = None,
        issue_list_error: GhError | None = None,
        pr_list_error: GhError | None = None,
        issue_view_errors: Mapping[int, GhError] | None = None,
        issue_close_errors: Mapping[int, GhError] | None = None,
        issue_comment_errors: Mapping[int, GhError] | None = None,
        pr_view_errors: Mapping[int, GhError] | None = None,
    ) -> None:
        self.authed = authed
        self.repo = (
            repo if repo is not None else Repo(owner="octo", name="kit", default_branch="main")
        )
        # Backing stores keyed by number (insertion order preserved for *_list).
        self._issues: dict[int, Issue] = {issue.number: issue for issue in issues}
        self._prs: dict[int, PullRequest] = {pr.number: pr for pr in prs}
        # Optional per-number single-view overrides (diverge view from list).
        self._issue_views: dict[int, Issue] = dict(issue_views or {})
        # Whole-operation injected failures (None = the happy path).
        self.auth_status_error = auth_status_error
        self.repo_view_error = repo_view_error
        self.issue_list_error = issue_list_error
        self.pr_list_error = pr_list_error
        # Per-number injected failures (a single item fails; the pool proceeds).
        self._issue_view_errors: dict[int, GhError] = dict(issue_view_errors or {})
        self._issue_close_errors: dict[int, GhError] = dict(issue_close_errors or {})
        self._issue_comment_errors: dict[int, GhError] = dict(
            issue_comment_errors or {}
        )
        self._pr_view_errors: dict[int, GhError] = dict(pr_view_errors or {})
        # Read/write spies.
        self.issue_list_calls: list[tuple[str, str]] = []
        self.issue_view_calls: list[int] = []
        self.issue_close_calls: list[tuple[int, str]] = []
        self.issue_comment_calls: list[tuple[int, str]] = []
        self.pr_list_calls: list[tuple[str, str]] = []
        self.pr_view_calls: list[int] = []

    # -- GitHubClient mechanics -------------------------------------------

    def auth_status(self) -> bool:
        if self.auth_status_error is not None:
            raise self.auth_status_error
        return self.authed

    def repo_view(self) -> Repo:
        if self.repo_view_error is not None:
            raise self.repo_view_error
        return self.repo

    def issue_list(self, label: str, state: str = "open") -> list[Issue]:
        self.issue_list_calls.append((label, state))
        if self.issue_list_error is not None:
            raise self.issue_list_error
        return [issue for issue in self._issues.values() if _state_matches(issue.state, state)]

    def issue_view(self, number: int) -> Issue:
        self.issue_view_calls.append(number)
        err = self._issue_view_errors.get(number)
        if err is not None:
            raise err
        if number in self._issue_views:
            return self._issue_views[number]
        try:
            return self._issues[number]
        except KeyError:
            raise GhError(
                ["gh", "issue", "view", str(number)],
                1,
                f"issue #{number} not found",
            ) from None

    def issue_close(self, number: int, comment: str) -> None:
        self.issue_close_calls.append((number, comment))
        err = self._issue_close_errors.get(number)
        if err is not None:
            raise err
        existing = self._issues.get(number)
        if existing is not None:
            self._issues[number] = replace(existing, state="CLOSED")

    def issue_comment(self, number: int, comment: str) -> None:
        """Record a breadcrumb comment (#63) — never changes the issue state.

        Appends to :attr:`issue_comment_calls` and, unlike :meth:`issue_close`,
        leaves the stored issue's ``state`` untouched (a comment resolves
        nothing). Per-number ``issue_comment_errors`` inject a
        :exc:`~ralph_afk.gh.GhError` so a test drives the source's
        breadcrumb-resilience path.
        """
        self.issue_comment_calls.append((number, comment))
        err = self._issue_comment_errors.get(number)
        if err is not None:
            raise err

    def pr_list(self, label: str, state: str = "open") -> list[PullRequest]:
        self.pr_list_calls.append((label, state))
        if self.pr_list_error is not None:
            raise self.pr_list_error
        return [pr for pr in self._prs.values() if _state_matches(pr.state, state)]

    def pr_view(self, number: int) -> PullRequest:
        self.pr_view_calls.append(number)
        err = self._pr_view_errors.get(number)
        if err is not None:
            raise err
        try:
            return self._prs[number]
        except KeyError:
            raise GhError(
                ["gh", "pr", "view", str(number)],
                1,
                f"pr #{number} not found",
            ) from None

    # -- test scripting ----------------------------------------------------

    def set_pr_head(self, number: int, head_sha: str) -> None:
        """Advance a stored PR's ``head_sha`` (models an agent push to the branch).

        The PR analogue of :meth:`FakeGitClient.simulate_agent_commit`: drive it
        from the SDK stub's ``on_send`` hook so the head advances *between* the
        loop's collection-time :meth:`pr_view` (the baseline SHA captured for the
        brief) and the post-iteration advance-check :meth:`pr_view`, so
        ``_detect_pr_advances`` sees the branch move.
        """
        self._prs[number] = replace(self._prs[number], head_sha=head_sha)


class FakeGateRunner:
    """Scriptable in-memory :class:`~ralph_afk.gate.GateRunner` for Integration tests.

    Extends the seam-fake pattern (#46 git, #47 gh) to the runner-side Integration
    gate (#60, ADR-0009). Integration tests (#62 happy-path, #63 auto-resolution)
    need to script whether the feedback loops go **green** or **red** for a given
    worktree / integration attempt *without* really running any loops, so they can
    assert land-on-green / revert-on-red and the K=3 retry bound deterministically.

    Outcomes for each :meth:`run` resolve in this order:

    1. a **per-worktree** queue (``by_worktree={path: [True, False, ...]}``) — models
       repeated Integration attempts on the *same* worktree (e.g. the dedicated
       integration worktree the auto-resolution agent reuses: red-then-green);
    2. the **global call-ordered** queue (``outcomes=[...]``), popped once per call;
    3. the ``default`` (green unless overridden).

    Every worktree passed to :meth:`run` is recorded in :attr:`calls` for assertions.
    Red results carry ``failure`` (a shared :class:`~ralph_afk.gate.LoopFailure`) so a
    test can assert failure detail flows through, exactly as the production runner's
    does.
    """

    def __init__(
        self,
        *,
        outcomes: Sequence[bool] | None = None,
        by_worktree: Mapping[Path, Sequence[bool]] | None = None,
        default: bool = True,
        failure: LoopFailure | None = None,
    ) -> None:
        self._outcomes: list[bool] = list(outcomes) if outcomes else []
        self._by_worktree: dict[Path, list[bool]] = {
            Path(key): list(value) for key, value in (by_worktree or {}).items()
        }
        self._default = default
        self._failure = failure or LoopFailure(
            name="gate",
            command="<scripted>",
            returncode=1,
            output_tail="scripted red",
        )
        self.calls: list[Path] = []

    def run(self, worktree: Path) -> GateResult:
        worktree = Path(worktree)
        self.calls.append(worktree)
        queue = self._by_worktree.get(worktree)
        if queue:
            passed = queue.pop(0)
        elif self._outcomes:
            passed = self._outcomes.pop(0)
        else:
            passed = self._default
        if passed:
            return GateResult.green(("scripted",))
        return GateResult.red(("scripted",), self._failure)
