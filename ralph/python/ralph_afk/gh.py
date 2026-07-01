"""``ralph_afk.gh`` — typed subprocess wrapper around the ``gh`` CLI.

This module is the **only** place in ``ralph_afk/`` that talks to GitHub.
Every external GitHub call flows through ``subprocess.run(["gh", ...])`` so
the user's existing ``gh auth login`` (including GitHub Enterprise endpoints,
SSO tokens, and device-flow refresh) remains the single source of truth.

Issue I/O uses ``gh`` + the stdlib :mod:`json` (no ``jq`` dependency).

GitHub is a **real seam** (mirroring :class:`ralph_afk.git.GitClient`, #46):
:class:`ralph_afk.sources.GitHubIssueSource` holds a :class:`GitHubClient` (an
injectable Protocol) rather than calling module functions, so the sources tests
substitute one object (``tests.fakes.FakeGitHubClient``) instead of
monkeypatching a handful of free functions. Unlike the git seam there is **no
cwd binding** — ``gh`` runs in the process cwd — so the Protocol methods keep
their natural signatures and the adapter is stateless (``SubprocessGitHubClient()``
takes no arguments).

Public surface:

* :exc:`GhError` — typed failure from any client method.
* :class:`Repo`, :class:`Issue`, :class:`Comment`, :class:`PullRequest` — frozen
  value objects the Protocol references. :class:`PullRequest` carries
  ``head_sha`` (``headRefOid``) and ``head_branch`` (``headRefName``) so the loop
  can detect a PR-branch advance by SHA without a local checkout.
* :class:`GitHubClient` — ``@runtime_checkable`` Protocol naming the GitHub
  **mechanics** the source needs (list / view / close). The **policy** — what
  counts as a closure for **Strike**/progress, any close-keyword semantics —
  stays in the source/loop, never in the client; :meth:`~GitHubClient.issue_close`
  is a pure recorded action that never infers progress.
* :class:`SubprocessGitHubClient` — the production adapter. Stateless; every
  method shells out to real ``gh`` in the process cwd.

The client's mechanics:

* :meth:`~SubprocessGitHubClient.auth_status` — preflight check; returns ``bool``
  (does not raise on "not signed in"; only raises :exc:`GhError` if the ``gh``
  binary itself is missing).
* :meth:`~SubprocessGitHubClient.repo_view` — current repository's ``owner`` /
  ``name`` / default branch.
* :meth:`~SubprocessGitHubClient.issue_list` — list issues filtered by label and
  state. One pass pulls every field the loop's prompt needs; ``comments`` is
  left empty.
* :meth:`~SubprocessGitHubClient.issue_view` — full single-issue view including
  ``comments``.
* :meth:`~SubprocessGitHubClient.issue_close` — close an issue with a wrap-up
  comment **and verify** the close landed (raises :exc:`GhError` if the
  post-close state is not ``CLOSED``).
* :meth:`~SubprocessGitHubClient.pr_list` — list PRs filtered by label and state
  (``comments`` left empty, mirroring :meth:`~SubprocessGitHubClient.issue_list`).
* :meth:`~SubprocessGitHubClient.pr_view` — full single-PR view including
  ``comments``. The wrapper **never** closes or merges a PR (humans merge in QA),
  so there is no ``pr_close`` counterpart to
  :meth:`~SubprocessGitHubClient.issue_close`.

Design notes:

* **No Python-native API libraries.** ``httpx`` / ``requests`` / ``PyGithub``
  are explicitly forbidden — enforced by ``tests/test_no_forbidden_api_libs.py``.
  The seam keeps that posture: the adapter still shells out to real ``gh`` and
  the user's ``gh auth`` stays the single source of truth.
* **One small ``_run`` helper.** Centralises the subprocess invocation, error
  conversion, and stderr-tail extraction so every public function gets the
  same error semantics for free.
* **Defensive JSON parsing.** Malformed JSON or unexpected shape from ``gh``
  is converted to a :exc:`GhError` carrying the command argv and a short
  stdout tail — never leaks ``JSONDecodeError`` / ``KeyError`` / ``TypeError``
  into the loop.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from typing import Final, Protocol, Sequence, runtime_checkable

__all__ = [
    "GhError",
    "Repo",
    "Comment",
    "Issue",
    "PullRequest",
    "GitHubClient",
    "SubprocessGitHubClient",
]

_GH_BIN: Final[str] = "gh"
_STDERR_TAIL_LIMIT: Final[int] = 400


class GhError(RuntimeError):
    """Raised when a ``gh`` invocation fails or returns an unparseable shape.

    Attributes:
        command: The argv tuple that was executed (including ``"gh"``).
        returncode: The subprocess exit code. ``127`` if the binary itself
            was not found on PATH. ``0`` if the failure is a shape/parsing
            problem rather than a non-zero exit.
        stderr_tail: A bounded tail of the process stderr (or the JSON
            decoding error message for shape failures).
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
            f"gh subprocess failed: {' '.join(self.command)!r} "
            f"(exit {returncode}): {stderr_tail}"
        )


@dataclass(frozen=True)
class Repo:
    """The current repository's identifying triple.

    Attributes:
        owner: GitHub login of the repo owner (user or org).
        name: Repository name (the ``name`` half of ``owner/name``).
        default_branch: Name of the repo's default branch (e.g. ``"main"``).
    """

    owner: str
    name: str
    default_branch: str

    @property
    def nwo(self) -> str:
        """Convenience: ``"<owner>/<name>"`` (the "nwo" / "name with owner" form)."""
        return f"{self.owner}/{self.name}"


@dataclass(frozen=True)
class Comment:
    """A single issue comment as returned by ``gh``.

    Attributes:
        author: Commenter's GitHub login. Empty string for comments authored
            by deleted/ghost users (``"author": null`` in the API payload).
        body: Raw markdown body of the comment.
        created_at: ISO-8601 timestamp string as returned by GitHub.
    """

    author: str
    body: str
    created_at: str


@dataclass(frozen=True)
class Issue:
    """A GitHub issue.

    The ``labels`` field is a plain :class:`list` per the issue's acceptance
    criterion — the dataclass is frozen, so the attribute itself cannot be
    reassigned, but the list contents are not deep-frozen.

    ``comments`` is only populated by :func:`issue_view`; :func:`issue_list`
    leaves it empty for performance.

    Attributes:
        number: Issue number.
        title: Issue title.
        body: Raw markdown body. Empty string when the issue has no body
            (GitHub returns ``null`` for "no body"; we normalise to ``""``).
        labels: Label names attached to the issue, in the order ``gh`` returns them.
        state: ``"OPEN"`` or ``"CLOSED"`` (upper-case as ``gh`` returns it).
        url: Canonical https URL to the issue.
        comments: Tuple of :class:`Comment`, only populated by :func:`issue_view`.
    """

    number: int
    title: str
    body: str
    labels: list[str]
    state: str
    url: str
    comments: tuple[Comment, ...] = field(default=())


@dataclass(frozen=True)
class PullRequest:
    """A GitHub pull request.

    Mirrors :class:`Issue` but adds the two head-ref fields the AFK loop
    needs to detect progress on a PR without checking it out locally:

    Attributes:
        number: PR number. Shares GitHub's per-repo number space with
            issues, so a PR and an issue never collide on ``number``.
        title: PR title.
        body: Raw markdown body (``""`` when empty).
        labels: Label names attached to the PR, in ``gh`` order.
        state: ``"OPEN"`` / ``"CLOSED"`` / ``"MERGED"`` (upper-case, as
            ``gh`` returns it).
        url: Canonical https URL to the PR.
        head_sha: The PR head commit SHA (``headRefOid``). The loop captures
            this at collection time and re-reads it after the iteration; a
            change means the agent pushed to the PR branch — i.e. progress —
            even though no commit landed on the base branch locally.
        head_branch: The PR head branch name (``headRefName``) — the branch
            ``gh pr checkout <number>`` puts you on.
        comments: Tuple of :class:`Comment`, only populated by :func:`pr_view`.
    """

    number: int
    title: str
    body: str
    labels: list[str]
    state: str
    url: str
    head_sha: str
    head_branch: str
    comments: tuple[Comment, ...] = field(default=())


def _run(args: Sequence[str], *, check: bool = True) -> str:
    """Invoke ``gh <args>`` and return stdout.

    Args:
        args: Arguments to ``gh`` (without the binary name).
        check: If ``True`` (default), raise :exc:`GhError` on non-zero exit.

    Returns:
        Captured stdout as a string.

    Raises:
        GhError: On ``gh`` binary missing, or (when ``check=True``) on
            non-zero exit.
    """
    cmd = [_GH_BIN, *args]
    try:
        completed = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except FileNotFoundError as exc:
        raise GhError(cmd, 127, "gh not found on PATH") from exc

    if check and completed.returncode != 0:
        raise GhError(cmd, completed.returncode, _stderr_tail(completed.stderr))
    return completed.stdout


def _stderr_tail(stderr: str | None) -> str:
    """Trim a process's stderr to a bounded, readable tail."""
    tail = (stderr or "").strip()
    if not tail:
        return "(no stderr)"
    if len(tail) > _STDERR_TAIL_LIMIT:
        return "..." + tail[-_STDERR_TAIL_LIMIT:]
    return tail


def _parse_json(raw: str, cmd: Sequence[str]) -> object:
    """Parse ``gh`` JSON stdout, converting any failure to :exc:`GhError`."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        head = raw[:200].replace("\n", "\\n")
        raise GhError(
            cmd,
            0,
            f"gh produced unparseable JSON: {exc.msg} (stdout head: {head!r})",
        ) from exc


def _parse_issue(data: object, cmd: Sequence[str]) -> Issue:
    """Convert one ``gh`` issue JSON object into an :class:`Issue`.

    Any unexpected shape (missing required key, wrong type) is surfaced as a
    :exc:`GhError` so the loop sees a single error class.
    """
    if not isinstance(data, dict):
        raise GhError(
            cmd, 0, f"expected JSON object for issue, got {type(data).__name__}"
        )
    try:
        labels_raw = data.get("labels") or []
        labels: list[str] = []
        for lab in labels_raw:
            if isinstance(lab, dict) and "name" in lab:
                labels.append(str(lab["name"]))
        comments_raw = data.get("comments") or []
        comments: list[Comment] = []
        for c in comments_raw:
            if not isinstance(c, dict):
                continue
            author = (c.get("author") or {}).get("login") or ""
            comments.append(
                Comment(
                    author=str(author),
                    body=str(c.get("body") or ""),
                    created_at=str(c.get("createdAt") or ""),
                )
            )
        return Issue(
            number=int(data["number"]),
            title=str(data["title"]),
            body=str(data.get("body") or ""),
            labels=labels,
            state=str(data["state"]),
            url=str(data["url"]),
            comments=tuple(comments),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise GhError(
            cmd, 0, f"gh issue JSON missing or malformed field: {exc}"
        ) from exc


def _parse_pr(data: object, cmd: Sequence[str]) -> PullRequest:
    """Convert one ``gh`` pull-request JSON object into a :class:`PullRequest`.

    Parallels :func:`_parse_issue` (same defensive contract: any unexpected
    shape becomes a :exc:`GhError`) but also reads ``headRefOid`` /
    ``headRefName`` into ``head_sha`` / ``head_branch``.
    """
    if not isinstance(data, dict):
        raise GhError(
            cmd,
            0,
            f"expected JSON object for pull request, got {type(data).__name__}",
        )
    try:
        labels_raw = data.get("labels") or []
        labels: list[str] = []
        for lab in labels_raw:
            if isinstance(lab, dict) and "name" in lab:
                labels.append(str(lab["name"]))
        comments_raw = data.get("comments") or []
        comments: list[Comment] = []
        for c in comments_raw:
            if not isinstance(c, dict):
                continue
            author = (c.get("author") or {}).get("login") or ""
            comments.append(
                Comment(
                    author=str(author),
                    body=str(c.get("body") or ""),
                    created_at=str(c.get("createdAt") or ""),
                )
            )
        return PullRequest(
            number=int(data["number"]),
            title=str(data["title"]),
            body=str(data.get("body") or ""),
            labels=labels,
            state=str(data["state"]),
            url=str(data["url"]),
            head_sha=str(data.get("headRefOid") or ""),
            head_branch=str(data.get("headRefName") or ""),
            comments=tuple(comments),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise GhError(
            cmd, 0, f"gh pull request JSON missing or malformed field: {exc}"
        ) from exc


# --------------------------------------------------------------------------- #
# GitHubClient seam                                                           #
# --------------------------------------------------------------------------- #


@runtime_checkable
class GitHubClient(Protocol):
    """The GitHub **mechanics** the source needs, as an injectable seam.

    Stateless: unlike :class:`ralph_afk.git.GitClient` there is **no cwd
    binding** — ``gh`` runs in the process cwd — so the methods keep their
    natural signatures. :class:`ralph_afk.sources.GitHubIssueSource` holds one
    ``GitHubClient`` and owns the **policy** (what counts as a closure for
    **Strike**/progress, any close-keyword semantics); the client only provides
    raw list / view / close mechanics and never infers progress.
    :meth:`issue_close` in particular is a pure recorded action — it must not
    filter by Strike rules or interpret close-keywords.

    :class:`SubprocessGitHubClient` is the production adapter;
    ``tests.fakes.FakeGitHubClient`` the in-memory test double. Both satisfy this
    Protocol structurally — no subclassing required, but ``isinstance(impl,
    GitHubClient)`` works because the decorator marks it ``@runtime_checkable``.
    """

    def auth_status(self) -> bool:
        """Return ``True`` if ``gh`` is signed in, ``False`` otherwise."""
        ...

    def repo_view(self) -> Repo:
        """Return the current repository's identifying ``owner``/``name`` triple."""
        ...

    def issue_list(self, label: str, state: str = "open") -> list[Issue]:
        """List issues filtered by ``label`` / ``state`` (``comments`` left empty)."""
        ...

    def issue_view(self, number: int) -> Issue:
        """Fetch one issue including its ``comments``."""
        ...

    def issue_close(self, number: int, comment: str) -> None:
        """Close an issue with a wrap-up comment and verify the close landed."""
        ...

    def pr_list(self, label: str, state: str = "open") -> list[PullRequest]:
        """List pull requests filtered by ``label`` / ``state`` (``comments`` empty)."""
        ...

    def pr_view(self, number: int) -> PullRequest:
        """Fetch one pull request including its ``comments`` and head-ref fields."""
        ...


class SubprocessGitHubClient:
    """Stateless :class:`GitHubClient` shelling out to the real ``gh`` CLI.

    Holds no state — ``gh`` runs in the process cwd, so unlike
    :class:`ralph_afk.git.SubprocessGitClient` there is nothing to bind at
    construction (``SubprocessGitHubClient()`` takes no arguments). Every method
    funnels through the module-level :func:`_run` so the error semantics are
    uniform, and the user's ``gh auth`` stays the single source of truth (no
    ``httpx`` / ``requests`` / ``PyGithub``).
    """

    def auth_status(self) -> bool:
        """Return ``True`` if ``gh`` is signed in, ``False`` otherwise.

        Asymmetric with the rest of the client: a "not signed in" state
        (``gh auth status`` rc=1)
        is a normal outcome the loop wants to recover from with a user-facing
        message, not an exception. Only a missing ``gh`` binary raises
        :exc:`GhError`.
        """
        cmd = [_GH_BIN, "auth", "status"]
        try:
            completed = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
        except FileNotFoundError as exc:
            raise GhError(cmd, 127, "gh not found on PATH") from exc
        return completed.returncode == 0

    def repo_view(self) -> Repo:
        """Return identity of the repository the current cwd resolves to.

        Raises:
            GhError: If ``gh repo view`` fails (e.g. cwd is not a GitHub remote)
                or returns a payload the parser cannot understand.
        """
        cmd = ["repo", "view", "--json", "owner,name,defaultBranchRef"]
        raw = _run(cmd)
        data = _parse_json(raw, [_GH_BIN, *cmd])
        if not isinstance(data, dict):
            raise GhError(
                [_GH_BIN, *cmd],
                0,
                f"expected JSON object for repo view, got {type(data).__name__}",
            )
        try:
            return Repo(
                owner=str(data["owner"]["login"]),
                name=str(data["name"]),
                default_branch=str(data["defaultBranchRef"]["name"]),
            )
        except (KeyError, TypeError) as exc:
            raise GhError(
                [_GH_BIN, *cmd],
                0,
                f"gh repo view JSON missing or malformed field: {exc}",
            ) from exc

    def issue_list(self, label: str, state: str = "open") -> list[Issue]:
        """List issues filtered by label and state.

        Args:
            label: A single label name (matches ``gh``'s single ``--label`` flag).
            state: ``"open"``, ``"closed"``, or ``"all"`` — passed verbatim to
                ``gh issue list --state``. Defaults to ``"open"`` for the
                AFK-ready issue collector.

        Returns:
            A list of :class:`Issue` with ``comments`` always empty. The loop
            decides whether to fetch comments per-issue via :meth:`issue_view`.

        Raises:
            GhError: On any subprocess or parse failure.
        """
        cmd = [
            "issue",
            "list",
            "--state",
            state,
            "--label",
            label,
            "--limit",
            "100",
            "--json",
            "number,title,body,labels,state,url",
        ]
        raw = _run(cmd)
        parsed = _parse_json(raw, [_GH_BIN, *cmd])
        if not isinstance(parsed, list):
            raise GhError(
                [_GH_BIN, *cmd],
                0,
                f"expected JSON array from gh issue list, got {type(parsed).__name__}",
            )
        return [_parse_issue(item, [_GH_BIN, *cmd]) for item in parsed]

    def issue_view(self, number: int) -> Issue:
        """Fetch one issue including its comments.

        Args:
            number: Issue number.

        Returns:
            The :class:`Issue` with ``comments`` populated.

        Raises:
            GhError: On any subprocess or parse failure (e.g. issue not found).
        """
        cmd = [
            "issue",
            "view",
            str(number),
            "--json",
            "number,title,body,labels,state,url,comments",
        ]
        raw = _run(cmd)
        parsed = _parse_json(raw, [_GH_BIN, *cmd])
        return _parse_issue(parsed, [_GH_BIN, *cmd])

    def issue_close(self, number: int, comment: str) -> None:
        """Close an issue with a wrap-up comment, then verify the close landed.

        A ``gh issue close`` success is not trusted alone — we re-read state via
        ``gh issue view ... --json state`` and raise :exc:`GhError` if the
        post-close state is not
        ``CLOSED``. Closing an already-closed issue is a no-op (``gh`` is
        idempotent on this; the verify step still requires ``CLOSED``).

        This is a pure recorded **mechanic**: it closes exactly what it is told
        to close. Deciding *whether* a closure counts as **Strike** progress, or
        interpreting close-keywords, is the source/loop's **policy** — never the
        client's.

        Args:
            number: Issue number to close.
            comment: Markdown body for the wrap-up comment. Passed via argv
                (no shell), so no escaping is required for the caller.

        Raises:
            GhError: If the close subprocess fails, the verify subprocess fails,
                or the post-close state is not ``CLOSED``.
        """
        close_cmd = ["issue", "close", str(number), "--comment", comment]
        _run(close_cmd)
        verify_state = _issue_state(number)
        if verify_state != "CLOSED":
            verify_cmd = [_GH_BIN, "issue", "view", str(number), "--json", "state"]
            raise GhError(
                verify_cmd,
                0,
                f"gh issue close #{number} returned success but state is "
                f"{verify_state!r}, not 'CLOSED'.",
            )

    def pr_list(self, label: str, state: str = "open") -> list[PullRequest]:
        """List pull requests filtered by label and state.

        The PR-surface analogue of :meth:`issue_list`. Used by the AFK loop only
        when PR support is enabled (see
        :class:`ralph_afk.sources.GitHubIssueSource`).

        Args:
            label: A single label name (matches ``gh``'s single ``--label`` flag).
            state: ``"open"`` (default), ``"closed"``, ``"merged"``, or ``"all"`` —
                passed verbatim to ``gh pr list --state``.

        Returns:
            A list of :class:`PullRequest` with ``comments`` always empty
            (mirroring :meth:`issue_list`); the loop enriches per-PR via
            :meth:`pr_view` only for candidates it actually feeds the agent.

        Raises:
            GhError: On any subprocess or parse failure.
        """
        cmd = [
            "pr",
            "list",
            "--state",
            state,
            "--label",
            label,
            "--limit",
            "100",
            "--json",
            "number,title,body,labels,state,url,headRefOid,headRefName",
        ]
        raw = _run(cmd)
        parsed = _parse_json(raw, [_GH_BIN, *cmd])
        if not isinstance(parsed, list):
            raise GhError(
                [_GH_BIN, *cmd],
                0,
                f"expected JSON array from gh pr list, got {type(parsed).__name__}",
            )
        return [_parse_pr(item, [_GH_BIN, *cmd]) for item in parsed]

    def pr_view(self, number: int) -> PullRequest:
        """Fetch one pull request including its comments and head-ref fields.

        Args:
            number: PR number.

        Returns:
            The :class:`PullRequest` with ``comments`` populated and a fresh
            ``head_sha`` — the loop re-reads this after an iteration to decide
            whether the PR branch advanced.

        Raises:
            GhError: On any subprocess or parse failure (e.g. PR not found).
        """
        cmd = [
            "pr",
            "view",
            str(number),
            "--json",
            "number,title,body,labels,state,url,headRefOid,headRefName,comments",
        ]
        raw = _run(cmd)
        parsed = _parse_json(raw, [_GH_BIN, *cmd])
        return _parse_pr(parsed, [_GH_BIN, *cmd])


# --------------------------------------------------------------------------- #
# Internal: single-field state read for the issue_close verify step           #
# --------------------------------------------------------------------------- #


def _issue_state(number: int) -> str:
    """Read just the ``state`` field for an issue. Internal helper for verify."""
    cmd = ["issue", "view", str(number), "--json", "state"]
    raw = _run(cmd)
    parsed = _parse_json(raw, [_GH_BIN, *cmd])
    if not isinstance(parsed, dict) or "state" not in parsed:
        raise GhError(
            [_GH_BIN, *cmd],
            0,
            f"gh issue view #{number} state JSON malformed: {parsed!r}",
        )
    return str(parsed["state"])
