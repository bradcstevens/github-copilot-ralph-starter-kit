"""``ralph_afk.git`` — typed subprocess wrapper around the ``git`` CLI.

Every external ``git`` call in ``ralph_afk/`` flows through this module so
the user's existing ``git`` config (credential helpers, ``safe.directory``,
``user.email``, signing keys) remains the single source of truth.

Public surface:

* :exc:`GitError` — typed failure from any public function.
* :class:`Commit` — frozen value object carrying ``sha`` / ``subject`` /
  ``body`` / ``date``. The :attr:`Commit.message` property returns the full
  message (``subject + "\\n" + body``) so :func:`ralph_afk.wrapper.extract_close_refs`
  can scan both subject and body for closure keywords in one pass.
* :func:`repo_root` — top-level directory via ``git rev-parse --show-toplevel``.
* :func:`head_sha` — current HEAD SHA via ``git rev-parse HEAD``.
* :func:`is_dirty` — mirrors the bash stale-worktree guard at
  ``ralph/afk.sh:315``: returns ``True`` if either ``git diff --quiet`` or
  ``git diff --cached --quiet`` exits with code 1. Codes ``> 1`` indicate a
  real git failure (corrupted index, etc.) and raise :exc:`GitError` rather
  than being conflated with "dirty".
* :func:`commits_between` — list of :class:`Commit` for ``pre..head``.
* :func:`recent_commits` — last ``n`` commits, newest-first.
* :func:`range_count` — ``git rev-list --count`` for ``pre..head``.

Design notes:

* **No Python-native git libraries.** ``GitPython`` / ``pygit2`` are
  explicitly forbidden — enforced by ``tests/test_no_forbidden_api_libs.py``.
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
from typing import Final, Sequence

__all__ = [
    "GitError",
    "Commit",
    "repo_root",
    "head_sha",
    "is_dirty",
    "commits_between",
    "recent_commits",
    "range_count",
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
# Public API                                                                  #
# --------------------------------------------------------------------------- #


def repo_root(start: Path | str | None = None) -> Path:
    """Return the top-level directory of the enclosing git repository.

    Args:
        start: Directory to resolve from. Defaults to the current cwd.

    Returns:
        The repository root as an absolute :class:`Path` (with macOS
        ``/private/var/...`` symlinks resolved via :meth:`Path.resolve`).

    Raises:
        GitError: If ``git`` is not on PATH or ``start`` is not inside a
            git repository.
    """
    out = _run(["rev-parse", "--show-toplevel"], cwd=start)
    return Path(out.strip()).resolve()


def head_sha(start: Path | str | None = None) -> str:
    """Return the current ``HEAD`` commit SHA (full 40-char form).

    Raises:
        GitError: If ``git`` is not on PATH, ``start`` is not inside a git
            repository, or the repo has no commits yet.
    """
    out = _run(["rev-parse", "HEAD"], cwd=start)
    return out.strip()


def is_dirty(start: Path | str | None = None) -> bool:
    """Return ``True`` if the working tree has uncommitted staged or unstaged changes.

    Mirrors the bash stale-worktree guard at ``ralph/afk.sh:315``::

        if ! git diff --quiet || ! git diff --cached --quiet; then
            # dirty
        fi

    ``git diff --quiet`` exits ``0`` on clean and ``1`` on dirty. Codes
    ``> 1`` indicate a real git failure (corrupted index, missing object,
    etc.) and we raise :exc:`GitError` rather than silently treating the
    failure as "dirty" — the loop's stale-worktree guard wants to surface
    a real problem with a real error message.

    Note: Like the bash variant, ``is_dirty`` does NOT check for untracked
    files; an untracked file alone does not make the tree "dirty".

    Args:
        start: Directory inside the repo to run from. Defaults to cwd.

    Raises:
        GitError: If ``git`` is not on PATH, or ``diff --quiet`` returns
            an exit code other than 0 or 1.
    """
    for args in (["diff", "--quiet"], ["diff", "--cached", "--quiet"]):
        cmd = [_GIT_BIN, *args]
        try:
            completed = subprocess.run(
                cmd,
                cwd=str(start) if start is not None else None,
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


def commits_between(
    pre: str, head: str, start: Path | str | None = None
) -> list[Commit]:
    """Return commits in ``pre..head`` (exclusive of ``pre``, inclusive of ``head``).

    Order is git's default for ``log``: newest first. The bash auto-close
    backstop scans these for closure keywords via
    :func:`ralph_afk.wrapper.extract_close_refs` against ``commit.message``.

    Args:
        pre: Exclusive start SHA.
        head: Inclusive end SHA (typically ``HEAD``).
        start: Directory inside the repo to run from. Defaults to cwd.

    Returns:
        A list of :class:`Commit`. Empty if ``pre == head``.

    Raises:
        GitError: On any subprocess failure (invalid SHA, etc.).
    """
    if pre == head:
        return []
    return _parse_log_z(
        ["log", _LOG_FORMAT, "--date=short", "-z", f"{pre}..{head}"],
        cwd=start,
    )


def recent_commits(n: int, start: Path | str | None = None) -> list[Commit]:
    """Return the last ``n`` commits on the current branch, newest first.

    Args:
        n: Maximum number of commits to return. ``n <= 0`` returns ``[]``.
        start: Directory inside the repo to run from. Defaults to cwd.

    Returns:
        A list of :class:`Commit`, length ``min(n, total_commits)``.

    Raises:
        GitError: On any subprocess failure.
    """
    if n <= 0:
        return []
    return _parse_log_z(
        ["log", f"-n{n}", _LOG_FORMAT, "--date=short", "-z"],
        cwd=start,
    )


def range_count(
    pre: str, head: str, start: Path | str | None = None
) -> int:
    """Return the number of commits in ``pre..head``.

    Mirrors ``git rev-list --count $pre..$head``. Returns 0 if ``pre == head``.

    Raises:
        GitError: On any subprocess failure (invalid SHA, etc.).
    """
    if pre == head:
        return 0
    out = _run(["rev-list", "--count", f"{pre}..{head}"], cwd=start)
    return int(out.strip())


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
