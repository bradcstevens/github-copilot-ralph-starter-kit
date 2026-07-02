"""``ralph_afk.git`` — typed subprocess seam around the ``git`` CLI.

Every external ``git`` call in ``ralph_afk/`` flows through this module so
the user's existing ``git`` config (credential helpers, ``safe.directory``,
``user.email``, signing keys) remains the single source of truth.

Git is a **real seam**: the loop holds a :class:`GitClient` (an injectable
Protocol) rather than calling module functions, so tests substitute one
object (``tests.fakes.FakeGitClient``) instead of monkeypatching a dozen
free functions. The client is **root-bound** — it carries the repository
root, so every call site drops the "which directory does git run in" detail
(no ``start=`` argument); it is captured once at construction.

Public surface:

* :exc:`GitError` — typed failure from any client method.
* :class:`Commit` — frozen value object carrying ``sha`` / ``subject`` /
  ``body`` / ``date``. The :attr:`Commit.message` property returns the full
  message (``subject + "\\n" + body``) so :func:`ralph_afk.wrapper.extract_close_refs`
  can scan both subject and body for closure keywords in one pass.
* :class:`GitClient` — ``@runtime_checkable`` Protocol naming the git
  **mechanics** the loop needs (the loop owns the Iteration **policy** that
  orders them). Root-bound: no ``start=`` parameter.
* :class:`SubprocessGitClient` — the production adapter. Constructed with a
  repository root, or discovered from a starting directory via
  :meth:`SubprocessGitClient.discover`; every method shells out to real
  ``git`` in that root.

The client's mechanics:

* :meth:`~SubprocessGitClient.head_sha` — current HEAD SHA via
  ``git rev-parse HEAD``.
* :meth:`~SubprocessGitClient.is_dirty` — tracked-change probe feeding the
  runner Checkpoint (ADR-0004): returns ``True`` if either
  ``git diff --quiet`` or ``git diff --cached --quiet`` exits with code 1.
  Codes ``> 1`` indicate a real git failure (corrupted index, etc.) and
  raise :exc:`GitError` rather than being conflated with "dirty".
* :meth:`~SubprocessGitClient.has_untracked` — companion probe: ``True`` if
  any untracked, non-ignored file exists
  (``git ls-files --others --exclude-standard``).
* :meth:`~SubprocessGitClient.add_all` / :meth:`~SubprocessGitClient.commit`
  — the mutating half of the runner Checkpoint (``git add -A`` then
  ``git commit -m``); the user's git config stays the single source of truth.
* :meth:`~SubprocessGitClient.push` — the remote half of the durability net
  (ADR-0004): a bare ``git push`` of the current branch to its configured
  upstream. Failures (no upstream, auth, non-fast-forward) raise
  :exc:`GitError` so the loop can warn without aborting; a local-only repo
  keeps working.
* :meth:`~SubprocessGitClient.commits_between` — list of :class:`Commit` for
  ``pre..head``.
* :meth:`~SubprocessGitClient.recent_commits` — last ``n`` commits, newest-first.
* :meth:`~SubprocessGitClient.range_count` — ``git rev-list --count`` for
  ``pre..head``.
* :meth:`~SubprocessGitClient.add_worktree` /
  :meth:`~SubprocessGitClient.remove_worktree` — the Parallel-mode **Lane**
  worktree lifecycle (ADR-0008): ``git worktree add -b <branch> <path> <base>``
  returns a fresh root-bound client for the worktree (so every mechanic above
  then addresses *that* worktree), and ``git worktree remove`` tears it down
  while keeping its branch as a breadcrumb. :func:`lane_branch_name` is the pure
  ``copiloop/<run_id>/issue-<N>`` branch-naming helper the Lane orchestrator
  feeds to ``add_worktree``.

Design notes:

* **No Python-native git libraries.** ``GitPython`` / ``pygit2`` are
  explicitly forbidden — enforced by ``tests/test_no_forbidden_api_libs.py``.
  The seam keeps that ADR-0004 posture: the adapter still shells out to real
  ``git`` and the user's git config stays the single source of truth.
* **NUL-delimited log parsing.** ``git log -z`` separates commits with
  ``\\0`` rather than ``\\n``, which means commit bodies containing
  ``---COMMIT-BOUNDARY---``-style strings cannot fool the parser.
* **Defensive UTF-8 decoding.** ``errors="replace"`` keeps the unattended
  loop alive on commits with non-UTF-8 byte sequences — a strict decode
  failure on an old commit body should not abort an iteration.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol, Sequence, runtime_checkable

__all__ = [
    "GitError",
    "Commit",
    "GitClient",
    "SubprocessGitClient",
    "lane_branch_name",
]

_GIT_BIN: Final[str] = "git"
_STDERR_TAIL_LIMIT: Final[int] = 400

# Shared format string for commits_between and recent_commits.
# `-z` makes inter-commit separators NUL bytes; within a commit, fields are
# separated by `\n` — only the body (`%b`) can contain `\n`, so we split on
# `\n` up to three times to recover [sha, subject, date, body].
_LOG_FORMAT: Final[str] = "--format=%H%n%s%n%ad%n%b"


class GitError(RuntimeError):
    """Raised when a ``git`` invocation fails.

    Attributes:
        command: The argv tuple that was executed (including ``"git"``).
        returncode: The subprocess exit code. ``127`` if the binary was not
            found on PATH.
        stderr_tail: A bounded tail of the process stderr.
    """

    def __init__(
        self,
        command: Sequence[str],
        returncode: int,
        stderr_tail: str,
    ) -> None:
        self.command: tuple[str, ...] = tuple(command)
        self.returncode = returncode
        self.stderr_tail = stderr_tail
        super().__init__(
            f"git subprocess failed: {' '.join(self.command)!r} "
            f"(exit {returncode}): {stderr_tail}"
        )


@dataclass(frozen=True)
class Commit:
    """A single git commit.

    Attributes:
        sha: The full 40-character commit hash.
        subject: The first line of the commit message.
        body: The commit message body (everything after the first line +
            blank-line separator), with trailing newlines stripped.
        date: ``--date=short``-formatted authored date (``YYYY-MM-DD``).
            Empty string when produced via a code path that did not request
            a date.
    """

    sha: str
    subject: str
    body: str
    date: str = ""

    @property
    def message(self) -> str:
        """Full commit message (``subject + "\\n" + body``).

        This is what :func:`ralph_afk.wrapper.extract_close_refs` expects —
        closure keywords (``Closes #N`` / ``Fixes #N`` / ``Resolves #N``)
        commonly live in the subject line, not just the body, so wrapper
        callers should scan ``commit.message``, never just ``commit.body``.
        """
        if not self.body:
            return self.subject
        return f"{self.subject}\n{self.body}"


def _run(
    args: Sequence[str],
    *,
    cwd: Path | str | None = None,
    check: bool = True,
) -> str:
    """Invoke ``git <args>`` and return stdout.

    Args:
        args: Arguments to ``git`` (without the binary name).
        cwd: Directory to invoke ``git`` from. Defaults to the current cwd.
        check: If ``True`` (default), raise :exc:`GitError` on non-zero exit.

    Returns:
        Captured stdout as a string.

    Raises:
        GitError: On ``git`` binary missing, or (when ``check=True``) on
            non-zero exit.
    """
    cmd = [_GIT_BIN, *args]
    try:
        completed = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd is not None else None,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except FileNotFoundError as exc:
        raise GitError(cmd, 127, "git not found on PATH") from exc

    if check and completed.returncode != 0:
        raise GitError(cmd, completed.returncode, _stderr_tail(completed.stderr))
    return completed.stdout


def _stderr_tail(stderr: str | None) -> str:
    tail = (stderr or "").strip()
    if not tail:
        return "(no stderr)"
    if len(tail) > _STDERR_TAIL_LIMIT:
        return "..." + tail[-_STDERR_TAIL_LIMIT:]
    return tail


# --------------------------------------------------------------------------- #
# Parallel-mode Lane branch naming (ADR-0005 / ADR-0008)                       #
# --------------------------------------------------------------------------- #


def lane_branch_name(run_id: str, issue_number: int) -> str:
    """Return the branch name for a Parallel-mode **Lane**.

    Parallel mode (ADR-0008) gives each **Lane** its own worktree on a dedicated
    branch cut from base. The branch follows the **copiloop** convention from
    ADR-0005: ``copiloop/<run_id>/issue-<N>``. Keeping this as a pure,
    seam-level helper lets the Wave/Lane orchestrator (a later slice) construct
    the branch it hands to :meth:`GitClient.add_worktree` without restating the
    format, and pins the convention under test here.

    Args:
        run_id: The run identifier (a 26-char ULID in production, but any
            string is accepted — the helper is a pure formatter).
        issue_number: The Lane's ``parallel-safe`` issue number.

    Returns:
        ``f"copiloop/{run_id}/issue-{issue_number}"``.
    """
    return f"copiloop/{run_id}/issue-{issue_number}"


def integration_branch_name(run_id: str, issue_number: int) -> str:
    """Return the branch name for a Parallel-mode auto-resolution attempt.

    Integration recovery (#63, ADR-0009) merges a red / conflicting **Lane** on a
    dedicated *integration* branch in its own worktree, so the base branch is
    never touched until the feedback loops pass. The branch follows the
    **copiloop** convention (ADR-0005) with an ``integrate/`` segment that keeps
    it distinct from the retained Lane breadcrumb branch
    (:func:`lane_branch_name`): ``copiloop/<run_id>/integrate/issue-<N>``.

    Args:
        run_id: The run identifier.
        issue_number: The Lane's ``parallel-safe`` issue number.

    Returns:
        ``f"copiloop/{run_id}/integrate/issue-{issue_number}"``.
    """
    return f"copiloop/{run_id}/integrate/issue-{issue_number}"


# --------------------------------------------------------------------------- #
# GitClient seam                                                              #
# --------------------------------------------------------------------------- #


@runtime_checkable
class GitClient(Protocol):
    """The git **mechanics** the loop needs, as an injectable seam.

    Root-bound: an implementation carries the repository root, so no method
    takes a ``start=`` directory argument. The loop holds one ``GitClient``
    and owns the Iteration **policy** that orders these calls (pre/post
    ``head_sha`` reads, ``commits_between`` before the Checkpoint, ``add_all``
    then ``commit``, ``push`` after) — the client never sequences them.

    :class:`SubprocessGitClient` is the production adapter;
    ``tests.fakes.FakeGitClient`` the in-memory test double. Both satisfy this
    Protocol structurally — no subclassing required, but ``isinstance(impl,
    GitClient)`` works because the decorator marks it ``@runtime_checkable``.
    """

    @property
    def root(self) -> Path:
        """The repository (or worktree) root this client is bound to.

        Root-bound by construction: :meth:`add_worktree` returns a client whose
        ``root`` is the new worktree, so a Parallel-mode Lane can pin its agent
        session to ``str(client.root)`` via the SDK's ``working_directory``.
        """
        ...

    def head_sha(self) -> str:
        """Return the current ``HEAD`` commit SHA (full 40-char form)."""
        ...

    def is_dirty(self) -> bool:
        """Return ``True`` if the tree has uncommitted staged/unstaged changes."""
        ...

    def has_untracked(self) -> bool:
        """Return ``True`` if the tree has any untracked, non-ignored file."""
        ...

    def add_all(self) -> None:
        """Stage every change in the worktree (``git add -A``)."""
        ...

    def commit(self, message: str) -> str:
        """Create a commit with ``message`` and return the new ``HEAD`` SHA."""
        ...

    def push(self) -> None:
        """Push the current branch to its configured upstream."""
        ...

    def current_branch(self) -> str | None:
        """Return the checked-out branch name, or ``None`` on detached HEAD."""
        ...

    def switch(self, branch: str) -> None:
        """Check out an existing local branch by name."""
        ...

    def commits_between(self, pre: str, head: str) -> list[Commit]:
        """Return commits in ``pre..head`` (exclusive of ``pre``)."""
        ...

    def recent_commits(self, n: int) -> list[Commit]:
        """Return the last ``n`` commits, newest first."""
        ...

    def range_count(self, pre: str, head: str) -> int:
        """Return the number of commits in ``pre..head``."""
        ...

    def add_worktree(self, path: Path, *, branch: str, base: str) -> GitClient:
        """Create a git worktree at ``path`` on a new ``branch`` cut from ``base``.

        The Parallel-mode **Lane** primitive (ADR-0008): each Lane works in its
        own worktree on a dedicated branch (``copiloop/<run_id>/issue-<N>`` — see
        :func:`lane_branch_name`) branched from the base branch, created in a
        sibling directory **outside** the repo (never nested inside it).

        Returns a **root-bound** :class:`GitClient` for the new worktree, so every
        mechanic above (``head_sha`` / ``is_dirty`` / ``has_untracked`` / ``add_all``
        / ``commit`` / ``commits_between`` / ...) then addresses *that* worktree
        independently of the main worktree — the same root-binding trick, one
        client per worktree. ``path`` must not already exist (git creates it).
        """
        ...

    def remove_worktree(self, path: Path, *, force: bool = False) -> None:
        """Remove the worktree at ``path`` at the Wave barrier.

        Tears down the worktree directory but **keeps its branch** — ADR-0008
        deletes integrated branches during Integration and retains failed ones as
        breadcrumbs, so worktree teardown never touches the branch. ``force=True``
        discards any uncommitted changes still in the worktree (a plain remove
        refuses to drop a dirty worktree).
        """
        ...

    def merge(self, branch: str) -> None:
        """Merge ``branch`` into the checked-out base branch at the Wave barrier.

        The Integration primitive (ADR-0009): with the base branch checked out in
        the main worktree, land a finished Lane branch onto it. Deterministic and
        revertable — a merge commit is always created (no fast-forward) so #63's
        auto-resolution can revert a bad landing cleanly.

        Raises:
            GitError: If the merge conflicts (or ``git`` is otherwise unhappy).
                Happy-path Integration (#62) skips a conflicting Lane; the
                auto-resolution slice (#63) owns recovery.
        """
        ...

    def delete_branch(self, branch: str) -> None:
        """Delete the local ``branch`` after it has been integrated.

        ADR-0008: Integration deletes a landed Lane branch, while failed branches
        are kept as breadcrumbs (:meth:`remove_worktree` never touches the branch).

        Raises:
            GitError: If the branch does not exist or ``git`` refuses to delete it.
        """
        ...

    def revert_merge(self) -> None:
        """Revert the merge commit at ``HEAD`` so the base branch stays green.

        Integration recovery (#63, ADR-0009): when a Lane merged cleanly but the
        feedback loops then went red, undo that landing. Because :meth:`merge`
        always creates a ``--no-ff`` merge commit, ``HEAD`` is that merge, so a
        single ``git revert -m 1 --no-edit HEAD`` reverts it against the first
        parent (the base side) — restoring the pre-merge tree while keeping
        history append-only (never a destructive reset of a possibly-pushed
        base).

        Raises:
            GitError: If ``git`` is not on PATH or ``HEAD`` is not a revertable
                merge commit.
        """
        ...

    def abort_merge(self) -> None:
        """Abort an in-progress conflicted merge, restoring the base branch.

        Integration recovery (#63, ADR-0009) for the *conflict* case: a
        :meth:`merge` that conflicts leaves the repo mid-merge; ``git merge
        --abort`` unwinds it so the base branch is exactly where it was before
        the merge attempt (green), ready for the auto-resolution agent.

        Raises:
            GitError: If ``git`` is not on PATH or there is no merge to abort.
        """
        ...


class SubprocessGitClient:
    """Root-bound :class:`GitClient` shelling out to the real ``git`` CLI.

    Carries the repository root captured once at construction (or discovered
    via :meth:`discover`); every method runs ``git`` in that root, so callers
    never restate the directory. Honours ADR-0004: no ``GitPython`` / ``pygit2``
    — the user's ``git`` config stays the single source of truth.
    """

    def __init__(self, root: Path) -> None:
        """Bind the client to ``root`` (the repository top-level directory)."""
        self._root: Path = Path(root)

    @property
    def root(self) -> Path:
        """The repository root every git call runs in."""
        return self._root

    @classmethod
    def discover(cls, start: Path | str | None = None) -> SubprocessGitClient:
        """Construct a client bound to the repo enclosing ``start``.

        Resolves the top-level directory via ``git rev-parse --show-toplevel``
        (with macOS ``/private/var/...`` symlinks resolved via
        :meth:`Path.resolve`). ``repo_root`` **discovery** *produces* the root,
        so it is a classmethod, not a root-bound instance method.

        Args:
            start: Directory to resolve from. Defaults to the current cwd.

        Returns:
            A :class:`SubprocessGitClient` bound to the resolved root.

        Raises:
            GitError: If ``git`` is not on PATH or ``start`` is not inside a
                git repository.
        """
        out = _run(["rev-parse", "--show-toplevel"], cwd=start)
        return cls(Path(out.strip()).resolve())

    def head_sha(self) -> str:
        """Return the current ``HEAD`` commit SHA (full 40-char form).

        Raises:
            GitError: If ``git`` is not on PATH, the root is not inside a git
                repository, or the repo has no commits yet.
        """
        out = _run(["rev-parse", "HEAD"], cwd=self._root)
        return out.strip()

    def is_dirty(self) -> bool:
        """Return ``True`` if the working tree has uncommitted changes.

        Feeds the runner Checkpoint (ADR-0004)::

            if ! git diff --quiet || ! git diff --cached --quiet; then
                # dirty -> capture in a Checkpoint commit
            fi

        ``git diff --quiet`` exits ``0`` on clean and ``1`` on dirty. Codes
        ``> 1`` indicate a real git failure (corrupted index, missing object,
        etc.) and we raise :exc:`GitError` rather than silently treating the
        failure as "dirty" — the loop wants to surface a real problem with a
        real error message.

        Note: ``is_dirty`` does NOT check for untracked files
        (:meth:`has_untracked` does); an untracked file alone does not make
        the tree "dirty". The Checkpoint path ORs the two so it captures both.

        Raises:
            GitError: If ``git`` is not on PATH, or ``diff --quiet`` returns
                an exit code other than 0 or 1.
        """
        for args in (["diff", "--quiet"], ["diff", "--cached", "--quiet"]):
            cmd = [_GIT_BIN, *args]
            try:
                completed = subprocess.run(
                    cmd,
                    cwd=str(self._root),
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    check=False,
                )
            except FileNotFoundError as exc:
                raise GitError(cmd, 127, "git not found on PATH") from exc
            if completed.returncode == 1:
                return True
            if completed.returncode != 0:
                raise GitError(
                    cmd, completed.returncode, _stderr_tail(completed.stderr)
                )
        return False

    def has_untracked(self) -> bool:
        """Return ``True`` if the working tree has any untracked, non-ignored file.

        Complements :meth:`is_dirty` (which sees only tracked-file changes): the
        runner Checkpoint (ADR-0004) captures *both* dirty tracked files and brand
        new untracked files the agent forgot to ``git add``. Uses::

            git ls-files --others --exclude-standard

        ``--others`` lists files git is not tracking; ``--exclude-standard`` honours
        ``.gitignore`` / ``.git/info/exclude`` / the global excludes file, so an
        ignored build artefact never trips a Checkpoint. Non-empty output means at
        least one untracked, non-ignored path exists.

        Raises:
            GitError: If ``git`` is not on PATH or ``ls-files`` fails (e.g. the
                root is not inside a git repository).
        """
        out = _run(["ls-files", "--others", "--exclude-standard"], cwd=self._root)
        return bool(out.strip())

    def add_all(self) -> None:
        """Stage every change in the worktree via ``git add -A``.

        Stages modifications, deletions, and new (non-ignored) files in one pass,
        honouring ``.gitignore`` exactly as the user's git config dictates. This is
        the staging half of the runner Checkpoint (ADR-0004); the user's git config
        stays the single source of truth (no ``--force``, no excludes override).

        Raises:
            GitError: If ``git`` is not on PATH or the ``add`` fails.
        """
        _run(["add", "-A"], cwd=self._root)

    def commit(self, message: str) -> str:
        """Create a commit with ``message`` and return the new ``HEAD`` SHA.

        The commit half of the runner Checkpoint (ADR-0004). A plain
        ``git commit -m <message>`` so the user's git config — identity, signing
        key, hooks — stays the single source of truth; the runner never bypasses
        ``--no-verify`` or overrides the author. ``message`` may carry multiple
        paragraphs (subject, body, trailer) separated by blank lines; they survive
        git's default ``-m`` cleanup.

        The caller is expected to have staged something first (e.g. via
        :meth:`add_all`): ``git commit`` with an empty index exits non-zero and
        raises :exc:`GitError`, which the loop treats as a non-fatal skipped
        Checkpoint rather than an abort.

        Args:
            message: The full commit message (subject + optional body/trailer).

        Returns:
            The full 40-character SHA of the newly created commit.

        Raises:
            GitError: If ``git`` is not on PATH, nothing is staged, or the commit
                otherwise fails (e.g. a pre-commit hook rejected it).
        """
        _run(["commit", "-m", message], cwd=self._root)
        return self.head_sha()

    def push(self) -> None:
        """Push the current branch to its configured upstream via ``git push``.

        The remote half of ADR-0004's durability net. After an iteration produces
        new commits — agent commits and/or a runner :meth:`commit` Checkpoint — the
        loop pushes so the work reaches the remote instead of piling up locally. A
        bare ``git push`` (no ref arguments, no ``--force``) keeps the user's git
        config — ``push.default``, the branch's upstream tracking ref, credential
        helpers — the single source of truth.

        Every failure mode the loop must tolerate *non-fatally* (it warns and
        carries on, so a local-only repo keeps working) surfaces here as
        :exc:`GitError`:

        * no upstream configured for the current branch,
        * no remote, an unreachable remote, or an auth failure,
        * a non-fast-forward rejection (the remote moved under us).

        Raises:
            GitError: If ``git`` is not on PATH or the push is rejected for any of
                the reasons above. The loop's ``_maybe_push`` catches this and
                never lets it abort the run.
        """
        _run(["push"], cwd=self._root)

    def current_branch(self) -> str | None:
        """Return the name of the currently checked-out branch, or ``None``.

        Uses ``git symbolic-ref --quiet --short HEAD``. Returns ``None`` when
        HEAD is detached (no symbolic ref). ``gh pr checkout`` normally leaves
        a named branch, but a detached HEAD is a valid state the caller must
        handle (e.g. skip the base-branch restore rather than guess a name).

        Returns:
            The short branch name (e.g. ``"main"``), or ``None`` on detached HEAD.

        Raises:
            GitError: If ``git`` is not on PATH, or ``symbolic-ref`` fails for
                a reason other than detached HEAD (exit code > 1).
        """
        cmd = [_GIT_BIN, "symbolic-ref", "--quiet", "--short", "HEAD"]
        try:
            completed = subprocess.run(
                cmd,
                cwd=str(self._root),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
        except FileNotFoundError as exc:
            raise GitError(cmd, 127, "git not found on PATH") from exc
        if completed.returncode == 0:
            name = completed.stdout.strip()
            return name or None
        if completed.returncode == 1:
            # `symbolic-ref --quiet` exits 1 with no output on a detached HEAD.
            return None
        raise GitError(cmd, completed.returncode, _stderr_tail(completed.stderr))

    def switch(self, branch: str) -> None:
        """Check out an existing local branch by name.

        Thin wrapper over ``git checkout <branch>`` (``checkout`` rather than the
        newer ``git switch`` for maximum compatibility with the git versions the
        kit targets). The loop uses this to restore the base branch after an
        iteration that ran ``gh pr checkout`` left ``HEAD`` on a PR branch.

        Args:
            branch: Name of an existing local branch to check out.

        Raises:
            GitError: If ``git`` is not on PATH or the checkout fails (e.g. the
                branch doesn't exist, or the checkout would clobber local changes).
        """
        _run(["checkout", branch], cwd=self._root)

    def commits_between(self, pre: str, head: str) -> list[Commit]:
        """Return commits in ``pre..head`` (exclusive of ``pre``, inclusive of ``head``).

        Order is git's default for ``log``: newest first. The auto-close
        backstop scans these for closure keywords via
        :func:`ralph_afk.wrapper.extract_close_refs` against ``commit.message``.

        This range **excludes any runner Checkpoint by construction**: the loop
        reads ``head`` *before* authoring the Checkpoint, so a Checkpoint commit
        (authored after ``head``) falls outside ``pre..head``. That protects the
        Strike rule — a Checkpoint is not progress; an agent commit is.

        Args:
            pre: Exclusive start SHA.
            head: Inclusive end SHA (typically ``HEAD``).

        Returns:
            A list of :class:`Commit`. Empty if ``pre == head``.

        Raises:
            GitError: On any subprocess failure (invalid SHA, etc.).
        """
        if pre == head:
            return []
        return _parse_log_z(
            ["log", _LOG_FORMAT, "--date=short", "-z", f"{pre}..{head}"],
            cwd=self._root,
        )

    def recent_commits(self, n: int) -> list[Commit]:
        """Return the last ``n`` commits on the current branch, newest first.

        Args:
            n: Maximum number of commits to return. ``n <= 0`` returns ``[]``.

        Returns:
            A list of :class:`Commit`, length ``min(n, total_commits)``.

        Raises:
            GitError: On any subprocess failure.
        """
        if n <= 0:
            return []
        return _parse_log_z(
            ["log", f"-n{n}", _LOG_FORMAT, "--date=short", "-z"],
            cwd=self._root,
        )

    def range_count(self, pre: str, head: str) -> int:
        """Return the number of commits in ``pre..head``.

        Mirrors ``git rev-list --count $pre..$head``. Returns 0 if ``pre == head``.

        Raises:
            GitError: On any subprocess failure (invalid SHA, etc.).
        """
        if pre == head:
            return 0
        out = _run(["rev-list", "--count", f"{pre}..{head}"], cwd=self._root)
        return int(out.strip())

    def add_worktree(
        self, path: Path, *, branch: str, base: str
    ) -> SubprocessGitClient:
        """Create a worktree at ``path`` on a new ``branch`` cut from ``base``.

        Shells out to ``git worktree add -b <branch> <path> <base>`` from the
        repo root, then returns a fresh :class:`SubprocessGitClient` **bound to
        the worktree** so every subsequent git call runs there — the per-Lane
        primitive for Parallel mode (ADR-0008). ``git`` creates ``path`` (and any
        missing parent directories); we defensively ensure the parent exists so
        an older ``git`` that does not create intermediate directories still
        succeeds. ``path`` itself must not already exist.

        Args:
            path: Directory for the new worktree — a sibling **outside** the repo
                by convention (never nested inside it).
            branch: Name of the new branch to create (see :func:`lane_branch_name`).
            base: Commit-ish the branch is cut from (typically the base branch,
                e.g. ``"main"``).

        Returns:
            A :class:`SubprocessGitClient` bound to ``path``.

        Raises:
            GitError: If ``git`` is not on PATH, ``branch`` already exists, ``path``
                already exists, or ``base`` is unknown.
        """
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        _run(
            ["worktree", "add", "-b", branch, str(target), base],
            cwd=self._root,
        )
        return SubprocessGitClient(target)

    def remove_worktree(self, path: Path, *, force: bool = False) -> None:
        """Remove the worktree at ``path`` via ``git worktree remove``.

        Run from the repo root. Tears down the worktree directory but leaves its
        branch intact (ADR-0008 keeps failed Lane branches as breadcrumbs; the
        Integration slice deletes integrated ones separately). A plain remove
        refuses to discard a dirty worktree; ``force=True`` passes ``--force`` to
        drop it anyway.

        Args:
            path: The worktree directory to remove.
            force: When ``True``, discard uncommitted changes in the worktree.

        Raises:
            GitError: If ``git`` is not on PATH, ``path`` is not a worktree, or a
                dirty worktree is removed without ``force``.
        """
        args = ["worktree", "remove"]
        if force:
            args.append("--force")
        args.append(str(Path(path)))
        _run(args, cwd=self._root)

    def merge(self, branch: str) -> None:
        """Merge ``branch`` into the current branch via ``git merge --no-ff``.

        Run from the repo root with the base branch checked out. ``--no-ff`` forces
        a merge commit even when the base has not diverged, so Integration history
        is uniform and a landing is revertable by a single ``git revert -m 1`` (the
        seam #63 builds on); ``--no-edit`` takes git's default merge message
        non-interactively.

        Args:
            branch: The Lane branch to land (see :func:`lane_branch_name`).

        Raises:
            GitError: If ``git`` is not on PATH or the merge conflicts. A conflicted
                merge leaves the repo mid-merge for the caller to resolve or abort;
                happy-path Integration (#62) never reaches a conflict, and the
                auto-resolution slice (#63) owns recovery.
        """
        _run(["merge", "--no-ff", "--no-edit", branch], cwd=self._root)

    def delete_branch(self, branch: str) -> None:
        """Delete the local ``branch`` via ``git branch -D``.

        Run from the repo root to remove an integrated Lane branch (ADR-0008). Uses
        ``-D`` (force) so the runner deletes deterministically without first
        re-checking merge status — a Lane branch merged with ``--no-ff`` is already
        fully contained in the base branch.

        Args:
            branch: The branch to delete.

        Raises:
            GitError: If ``git`` is not on PATH or ``branch`` does not exist.
        """
        _run(["branch", "-D", branch], cwd=self._root)

    def revert_merge(self) -> None:
        """Revert the ``HEAD`` merge via ``git revert -m 1 --no-edit HEAD``.

        Run from the repo root right after a clean :meth:`merge` whose gate then
        went red. ``-m 1`` reverts against the merge's first parent (the base
        side) so the Lane's change is undone and the pre-merge tree restored;
        ``--no-edit`` takes git's default revert message non-interactively. The
        base branch stays green **and** append-only (see :meth:`GitClient.
        revert_merge`).
        """
        _run(["revert", "-m", "1", "--no-edit", "HEAD"], cwd=self._root)

    def abort_merge(self) -> None:
        """Abort an in-progress merge via ``git merge --abort``.

        Run from the repo root after a :meth:`merge` conflicted and left the repo
        mid-merge, restoring the base branch to its exact pre-merge state.
        """
        _run(["merge", "--abort"], cwd=self._root)


# --------------------------------------------------------------------------- #
# Internal: NUL-delimited log parser                                          #
# --------------------------------------------------------------------------- #


def _parse_log_z(
    args: Sequence[str], *, cwd: Path | str | None = None
) -> list[Commit]:
    """Parse output of ``git log -z`` with our standard ``_LOG_FORMAT``.

    Each record has the shape::

        <sha>\\n<subject>\\n<date>\\n<body>\\0

    Splits on ``\\0`` to recover records, then on ``\\n`` (max 4 parts) to
    recover the four fields. Trailing empty records (from the final ``\\0``)
    are skipped.
    """
    raw = _run(args, cwd=cwd)
    commits: list[Commit] = []
    for record in raw.split("\0"):
        # Skip the trailing-NUL artefact and any genuinely-empty record.
        if not record:
            continue
        # Strip a leading newline that some git versions emit between -z
        # records when the previous body did not end in a newline.
        record = record.lstrip("\n")
        if not record:
            continue
        parts = record.split("\n", 3)
        # Pad defensively: a commit with no body still has 4 fields, but
        # if the format string ever changes upstream we degrade gracefully.
        while len(parts) < 4:
            parts.append("")
        sha, subject, date, body = parts
        commits.append(
            Commit(
                sha=sha,
                subject=subject,
                date=date,
                body=body.rstrip("\n"),
            )
        )
    return commits
