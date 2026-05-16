"""``ralph_afk.gh`` — typed subprocess wrapper around the ``gh`` CLI.

This module is the **only** place in ``ralph_afk/`` that talks to GitHub.
Every external GitHub call flows through ``subprocess.run(["gh", ...])`` so
the user's existing ``gh auth login`` (including GitHub Enterprise endpoints,
SSO tokens, and device-flow refresh) remains the single source of truth.

The cross-runner contract (see ``ralph/afk.sh``):

* The bash runner uses ``gh`` + ``jq`` for issue I/O.
* The Python variant uses ``gh`` + stdlib :mod:`json` (no ``jq`` dependency).

Public surface:

* :exc:`GhError` — typed failure from any public function.
* :class:`Repo`, :class:`Issue`, :class:`Comment` — frozen value objects.
* :func:`auth_status` — preflight check; returns ``bool`` (does not raise on
  "not signed in"; only raises :exc:`GhError` if the ``gh`` binary itself
  is missing).
* :func:`repo_view` — current repository's ``owner`` / ``name`` / default branch.
* :func:`issue_list` — list issues filtered by label and state. One pass
  pulls every field the loop's prompt needs; ``comments`` is left empty.
* :func:`issue_view` — full single-issue view including ``comments``.
* :func:`issue_close` — close an issue with a wrap-up comment **and verify**
  the close landed (raises :exc:`GhError` if the post-close state is not
  ``CLOSED``). Mirrors the verify-after-close pattern at ``ralph/afk.sh:255``.

Design notes:

* **No Python-native API libraries.** ``httpx`` / ``requests`` / ``PyGithub``
  are explicitly forbidden — enforced by ``tests/test_no_forbidden_api_libs.py``.
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
from typing import Final, Sequence

__all__ = [
    "GhError",
    "Repo",
    "Comment",
    "Issue",
    "auth_status",
    "repo_view",
    "issue_list",
    "issue_view",
    "issue_close",
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


# --------------------------------------------------------------------------- #
# Public API                                                                  #
# --------------------------------------------------------------------------- #


def auth_status() -> bool:
    """Return ``True`` if ``gh`` is signed in, ``False`` otherwise.

    Mirrors the bash preflight at ``ralph/afk.sh:93``. Asymmetric with the
    rest of the module: a "not signed in" state (``gh auth status`` rc=1)
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


def repo_view() -> Repo:
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


def issue_list(label: str, state: str = "open") -> list[Issue]:
    """List issues filtered by label and state.

    Args:
        label: A single label name (matches ``gh``'s single ``--label`` flag).
        state: ``"open"``, ``"closed"``, or ``"all"`` — passed verbatim to
            ``gh issue list --state``. Defaults to ``"open"`` to match the
            bash collector's filter.

    Returns:
        A list of :class:`Issue` with ``comments`` always empty. The loop
        decides whether to fetch comments per-issue via :func:`issue_view`.

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


def issue_view(number: int) -> Issue:
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


def issue_close(number: int, comment: str) -> None:
    """Close an issue with a wrap-up comment, then verify the close landed.

    Mirrors the bash sequence at ``ralph/afk.sh:255-263``: a ``gh issue close``
    success is not trusted alone — we re-read state via ``gh issue view ...
    --json state`` and raise :exc:`GhError` if the post-close state is not
    ``CLOSED``. Closing an already-closed issue is a no-op (``gh`` is
    idempotent on this; the verify step still requires ``CLOSED``).

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
