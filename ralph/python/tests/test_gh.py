"""Tests for ``ralph_afk.gh`` (issue #6).

Covers the typed ``gh`` subprocess wrapper with mocked ``subprocess.run`` —
no real network or ``gh`` invocations. Realistic JSON shapes captured from
the real CLI are baked into the test fixtures.

Acceptance criteria reference: issue #6.
"""

from __future__ import annotations

import json
import subprocess
from typing import Any

import pytest

from ralph_afk import gh
from ralph_afk.gh import (
    GhError,
    GitHubClient,
    Issue,
    PullRequest,
    Repo,
    SubprocessGitHubClient,
)

# The ``gh`` mechanics moved from module free functions onto the stateless
# :class:`SubprocessGitHubClient` adapter (#47, mirroring the git seam #46). Bind
# its methods once so every call site below simply *retargets* onto the adapter —
# the ``subprocess.run`` mock (installed per test via ``_install_fake_run``) and
# every parse / error assertion are unchanged. The adapter is stateless, so one
# shared instance is equivalent to constructing a fresh one per call.
_client = SubprocessGitHubClient()
auth_status = _client.auth_status
repo_view = _client.repo_view
issue_list = _client.issue_list
issue_view = _client.issue_view
issue_close = _client.issue_close
issue_comment = _client.issue_comment
pr_list = _client.pr_list
pr_view = _client.pr_view


# --------------------------------------------------------------------------- #
# Test helpers                                                                 #
# --------------------------------------------------------------------------- #


def _completed(
    cmd: list[str],
    *,
    stdout: str = "",
    stderr: str = "",
    code: int = 0,
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(cmd, code, stdout=stdout, stderr=stderr)


def _install_fake_run(monkeypatch, handler):
    """Install ``handler`` as the new ``subprocess.run`` used by gh.

    ``handler(cmd, **kwargs) -> CompletedProcess``. The handler also has its
    captured argvs available via the closure pattern callers use.
    """
    monkeypatch.setattr(gh.subprocess, "run", handler)


# --------------------------------------------------------------------------- #
# Protocol conformance                                                         #
# --------------------------------------------------------------------------- #


def test_subprocess_github_client_satisfies_githubclient_protocol() -> None:
    """The adapter satisfies the ``@runtime_checkable`` ``GitHubClient`` structurally."""
    assert isinstance(SubprocessGitHubClient(), GitHubClient)
    assert not isinstance(object(), GitHubClient)


# --------------------------------------------------------------------------- #
# Dataclass shape                                                              #
# --------------------------------------------------------------------------- #


def test_repo_nwo_property() -> None:
    r = Repo(owner="o", name="n", default_branch="main")
    assert r.nwo == "o/n"


def test_issue_dataclass_default_comments_is_empty_tuple() -> None:
    i = Issue(
        number=1,
        title="t",
        body="b",
        labels=["x"],
        state="OPEN",
        url="https://example/1",
    )
    assert i.comments == ()


def test_issue_labels_field_is_a_list_per_acceptance_criterion() -> None:
    """Acceptance criterion says ``labels: list[str]`` — enforce the type."""
    i = Issue(
        number=1,
        title="t",
        body="b",
        labels=["a", "b"],
        state="OPEN",
        url="https://example/1",
    )
    assert isinstance(i.labels, list)
    assert i.labels == ["a", "b"]


# --------------------------------------------------------------------------- #
# auth_status                                                                  #
# --------------------------------------------------------------------------- #


def test_auth_status_true_when_gh_exits_zero(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        return _completed(cmd, code=0)

    _install_fake_run(monkeypatch, fake_run)
    assert auth_status() is True
    assert captured["cmd"][0] == "gh"
    assert captured["cmd"][1:] == ["auth", "status"]


def test_auth_status_false_when_gh_exits_nonzero(monkeypatch) -> None:
    def fake_run(cmd, **kw):
        return _completed(cmd, code=1, stderr="You are not logged into any GitHub hosts.\n")

    _install_fake_run(monkeypatch, fake_run)
    assert auth_status() is False


def test_auth_status_raises_gh_error_when_binary_missing(monkeypatch) -> None:
    def fake_run(cmd, **kw):
        raise FileNotFoundError(2, "No such file", "gh")

    _install_fake_run(monkeypatch, fake_run)
    with pytest.raises(GhError) as exc_info:
        auth_status()
    assert exc_info.value.returncode == 127
    assert "not found" in exc_info.value.stderr_tail.lower()


# --------------------------------------------------------------------------- #
# repo_view                                                                    #
# --------------------------------------------------------------------------- #


def test_repo_view_happy_path(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        return _completed(
            cmd,
            stdout=json.dumps(
                {
                    "owner": {"id": "id1", "login": "bradcstevens"},
                    "name": "github-copilot-ralph-starter-kit",
                    "defaultBranchRef": {"name": "main"},
                }
            ),
        )

    _install_fake_run(monkeypatch, fake_run)
    r = repo_view()
    assert r.owner == "bradcstevens"
    assert r.name == "github-copilot-ralph-starter-kit"
    assert r.default_branch == "main"
    assert r.nwo == "bradcstevens/github-copilot-ralph-starter-kit"
    # argv shape: gh repo view --json owner,name,defaultBranchRef
    assert captured["cmd"][0] == "gh"
    assert "repo" in captured["cmd"] and "view" in captured["cmd"]
    assert "--json" in captured["cmd"]


def test_repo_view_nonzero_exit_raises_gh_error(monkeypatch) -> None:
    def fake_run(cmd, **kw):
        return _completed(cmd, code=1, stderr="no git remotes found\n")

    _install_fake_run(monkeypatch, fake_run)
    with pytest.raises(GhError) as exc_info:
        repo_view()
    assert exc_info.value.returncode == 1
    assert "no git remotes" in exc_info.value.stderr_tail


def test_repo_view_malformed_json_raises_gh_error(monkeypatch) -> None:
    def fake_run(cmd, **kw):
        return _completed(cmd, stdout="not-json-{{")

    _install_fake_run(monkeypatch, fake_run)
    with pytest.raises(GhError) as exc_info:
        repo_view()
    assert "unparseable JSON" in exc_info.value.stderr_tail


def test_repo_view_missing_field_raises_gh_error(monkeypatch) -> None:
    def fake_run(cmd, **kw):
        # Missing defaultBranchRef entirely.
        return _completed(cmd, stdout=json.dumps({"owner": {"login": "x"}, "name": "y"}))

    _install_fake_run(monkeypatch, fake_run)
    with pytest.raises(GhError) as exc_info:
        repo_view()
    assert "missing or malformed" in exc_info.value.stderr_tail


# --------------------------------------------------------------------------- #
# issue_list                                                                   #
# --------------------------------------------------------------------------- #


_ISSUE_JSON_LIST_PAYLOAD = [
    {
        "number": 13,
        "title": "Docs parity",
        "body": "## Parent\n\n#1\n\n## Acceptance criteria\n\n- [ ] foo",
        "labels": [
            {"id": "L1", "name": "ready-for-agent", "description": "x", "color": "0e8a16"},
            {"id": "L2", "name": "docs"},
        ],
        "state": "OPEN",
        "url": "https://github.com/bradcstevens/github-copilot-ralph-starter-kit/issues/13",
    },
    {
        "number": 6,
        "title": "gh.py + git.py",
        "body": "## Parent\n#1\n## Acceptance criteria\n",
        "labels": [{"name": "ready-for-agent"}],
        "state": "OPEN",
        "url": "https://github.com/bradcstevens/github-copilot-ralph-starter-kit/issues/6",
    },
]


def test_issue_list_happy_path(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        return _completed(cmd, stdout=json.dumps(_ISSUE_JSON_LIST_PAYLOAD))

    _install_fake_run(monkeypatch, fake_run)
    items = issue_list("ready-for-agent")
    assert len(items) == 2
    assert items[0].number == 13
    assert items[0].title == "Docs parity"
    assert items[0].state == "OPEN"
    assert items[0].labels == ["ready-for-agent", "docs"]
    assert items[0].body.startswith("## Parent")
    # issue_list MUST leave comments empty per docstring contract.
    assert items[0].comments == ()
    # argv contains the expected flags.
    assert "--label" in captured["cmd"]
    assert "ready-for-agent" in captured["cmd"]
    assert "--state" in captured["cmd"]
    assert "open" in captured["cmd"]
    # one-pass fetch — body+labels+state+url all in one --json arg
    json_arg_idx = captured["cmd"].index("--json")
    json_fields = captured["cmd"][json_arg_idx + 1]
    for f in ("number", "title", "body", "labels", "state", "url"):
        assert f in json_fields


def test_issue_list_custom_state_arg_propagates(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        return _completed(cmd, stdout="[]")

    _install_fake_run(monkeypatch, fake_run)
    issue_list("ready-for-agent", state="all")
    assert "all" in captured["cmd"]


def test_issue_list_empty_array_returns_empty_list(monkeypatch) -> None:
    def fake_run(cmd, **kw):
        return _completed(cmd, stdout="[]")

    _install_fake_run(monkeypatch, fake_run)
    assert issue_list("anything") == []


def test_issue_list_nonzero_exit_raises_gh_error(monkeypatch) -> None:
    def fake_run(cmd, **kw):
        return _completed(cmd, code=1, stderr="HTTP 401\n")

    _install_fake_run(monkeypatch, fake_run)
    with pytest.raises(GhError) as exc_info:
        issue_list("ready-for-agent")
    assert exc_info.value.returncode == 1
    assert "HTTP 401" in exc_info.value.stderr_tail


def test_issue_list_non_array_payload_raises_gh_error(monkeypatch) -> None:
    def fake_run(cmd, **kw):
        return _completed(cmd, stdout=json.dumps({"oops": "object instead of array"}))

    _install_fake_run(monkeypatch, fake_run)
    with pytest.raises(GhError) as exc_info:
        issue_list("anything")
    assert "expected JSON array" in exc_info.value.stderr_tail


def test_issue_list_normalises_null_body_to_empty_string(monkeypatch) -> None:
    """``gh`` returns ``"body": null`` when the issue has no body; we want ``""``."""

    def fake_run(cmd, **kw):
        return _completed(
            cmd,
            stdout=json.dumps(
                [
                    {
                        "number": 1,
                        "title": "t",
                        "body": None,
                        "labels": [],
                        "state": "OPEN",
                        "url": "u",
                    }
                ]
            ),
        )

    _install_fake_run(monkeypatch, fake_run)
    [i] = issue_list("foo")
    assert i.body == ""


# --------------------------------------------------------------------------- #
# issue_view                                                                   #
# --------------------------------------------------------------------------- #


def test_issue_view_includes_comments(monkeypatch) -> None:
    payload = {
        "number": 13,
        "title": "Docs parity",
        "body": "...",
        "labels": [{"name": "ready-for-agent"}],
        "state": "OPEN",
        "url": "https://example/13",
        "comments": [
            {
                "author": {"login": "bradcstevens", "is_bot": False},
                "body": "first comment",
                "createdAt": "2026-05-10T12:34:56Z",
            },
            {
                "author": {"login": "Copilot", "is_bot": True},
                "body": "second comment",
                "createdAt": "2026-05-12T01:23:45Z",
            },
        ],
    }
    captured: dict[str, Any] = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        return _completed(cmd, stdout=json.dumps(payload))

    _install_fake_run(monkeypatch, fake_run)
    i = issue_view(13)
    assert i.number == 13
    assert len(i.comments) == 2
    assert i.comments[0].author == "bradcstevens"
    assert i.comments[0].body == "first comment"
    assert i.comments[0].created_at == "2026-05-10T12:34:56Z"
    assert i.comments[1].author == "Copilot"
    # argv must include 'comments' field
    json_arg_idx = captured["cmd"].index("--json")
    assert "comments" in captured["cmd"][json_arg_idx + 1]


def test_issue_view_with_null_author_yields_empty_author(monkeypatch) -> None:
    """Comments authored by deleted/ghost users have ``"author": null``."""

    def fake_run(cmd, **kw):
        return _completed(
            cmd,
            stdout=json.dumps(
                {
                    "number": 1,
                    "title": "t",
                    "body": "",
                    "labels": [],
                    "state": "OPEN",
                    "url": "u",
                    "comments": [
                        {"author": None, "body": "ghosted", "createdAt": "2026-05-15T00:00:00Z"},
                    ],
                }
            ),
        )

    _install_fake_run(monkeypatch, fake_run)
    i = issue_view(1)
    assert len(i.comments) == 1
    assert i.comments[0].author == ""
    assert i.comments[0].body == "ghosted"


def test_issue_view_nonzero_exit_raises_gh_error(monkeypatch) -> None:
    def fake_run(cmd, **kw):
        return _completed(cmd, code=1, stderr="GraphQL: Could not resolve to an issue\n")

    _install_fake_run(monkeypatch, fake_run)
    with pytest.raises(GhError) as exc_info:
        issue_view(999999)
    assert exc_info.value.returncode == 1
    assert "Could not resolve" in exc_info.value.stderr_tail


# --------------------------------------------------------------------------- #
# issue_close (with verify-after-close)                                        #
# --------------------------------------------------------------------------- #


def test_issue_close_verifies_state_after_close(monkeypatch) -> None:
    """``gh issue close`` success is not trusted alone — the wrapper
    re-reads state via ``gh issue view --json state``."""
    calls: list[list[str]] = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        if "close" in cmd:
            return _completed(cmd, code=0)
        # Verify call: gh issue view <n> --json state
        return _completed(cmd, stdout=json.dumps({"state": "CLOSED"}))

    _install_fake_run(monkeypatch, fake_run)
    issue_close(42, "wrap-up")

    # Two subprocess calls: the close + the verify.
    assert len(calls) == 2
    assert "close" in calls[0]
    assert "--comment" in calls[0]
    assert "wrap-up" in calls[0]
    assert "view" in calls[1]
    assert "--json" in calls[1] and "state" in calls[1]


def test_issue_close_raises_when_verify_state_is_not_closed(monkeypatch) -> None:
    """If ``gh issue close`` returns success but state is still OPEN, raise.

    A successful close subprocess that did not actually close the issue must
    be surfaced so the loop does not miscount closures.
    """

    def fake_run(cmd, **kw):
        if "close" in cmd:
            return _completed(cmd, code=0)
        return _completed(cmd, stdout=json.dumps({"state": "OPEN"}))

    _install_fake_run(monkeypatch, fake_run)
    with pytest.raises(GhError) as exc_info:
        issue_close(42, "wrap-up")
    assert "'OPEN'" in str(exc_info.value) or "OPEN" in exc_info.value.stderr_tail


def test_issue_close_nonzero_close_subprocess_raises(monkeypatch) -> None:
    def fake_run(cmd, **kw):
        return _completed(cmd, code=1, stderr="not authorized\n")

    _install_fake_run(monkeypatch, fake_run)
    with pytest.raises(GhError) as exc_info:
        issue_close(42, "comment body")
    assert exc_info.value.returncode == 1


# --------------------------------------------------------------------------- #
# issue_comment (breadcrumb: comment without closing, #63)                     #
# --------------------------------------------------------------------------- #


def test_issue_comment_posts_body_without_closing(monkeypatch) -> None:
    """``issue_comment`` runs one ``gh issue comment N --body`` and never closes."""
    calls: list[list[str]] = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return _completed(cmd, code=0)

    _install_fake_run(monkeypatch, fake_run)
    issue_comment(42, "auto-resolution exhausted; falling back to serial")

    # Exactly one subprocess: the comment. No close, no state verify.
    assert len(calls) == 1
    assert calls[0][-5:] == ["issue", "comment", "42", "--body", "auto-resolution exhausted; falling back to serial"]
    assert "close" not in calls[0]


def test_issue_comment_nonzero_subprocess_raises(monkeypatch) -> None:
    """A failing comment subprocess surfaces a typed ``GhError``."""

    def fake_run(cmd, **kw):
        return _completed(cmd, code=1, stderr="not found\n")

    _install_fake_run(monkeypatch, fake_run)
    with pytest.raises(GhError) as exc_info:
        issue_comment(42, "body")
    assert exc_info.value.returncode == 1


def test_issue_close_passes_comment_via_argv_no_escaping(monkeypatch) -> None:
    """The comment is passed via argv — no shell — so special chars are safe."""
    captured: dict[str, Any] = {}

    def fake_run(cmd, **kw):
        if "close" in cmd:
            captured["close_cmd"] = list(cmd)
            return _completed(cmd, code=0)
        return _completed(cmd, stdout=json.dumps({"state": "CLOSED"}))

    _install_fake_run(monkeypatch, fake_run)
    body = "Implemented in abc123.\n\n`special` chars 'quoted' \"and\" $shell"
    issue_close(7, body)
    assert body in captured["close_cmd"]


# --------------------------------------------------------------------------- #
# GhError shape                                                                #
# --------------------------------------------------------------------------- #


def test_gh_error_carries_command_returncode_stderr_tail() -> None:
    e = GhError(["gh", "issue", "view", "999"], 1, "not found")
    assert e.command == ("gh", "issue", "view", "999")
    assert e.returncode == 1
    assert e.stderr_tail == "not found"
    assert "gh issue view 999" in str(e)


def test_gh_error_truncates_long_stderr_via_helper() -> None:
    """The _stderr_tail helper trims to a bounded length so error logs stay readable."""
    long = "x" * 1000
    trimmed = gh._stderr_tail(long)
    assert len(trimmed) < 1000
    assert trimmed.startswith("...")


# --------------------------------------------------------------------------- #
# PullRequest dataclass + pr_list / pr_view / _parse_pr                        #
# --------------------------------------------------------------------------- #


_PR_JSON_LIST_PAYLOAD = [
    {
        "number": 7,
        "title": "Add caching layer",
        "body": "## Summary\nWIP",
        "labels": [
            {"id": "L1", "name": "ready-for-agent"},
            {"id": "L2", "name": "enhancement"},
        ],
        "state": "OPEN",
        "url": "https://github.com/x/y/pull/7",
        "headRefOid": "f" * 40,
        "headRefName": "feature/caching",
    }
]


def test_pull_request_dataclass_default_comments_is_empty_tuple() -> None:
    pr = PullRequest(
        number=7,
        title="t",
        body="b",
        labels=["x"],
        state="OPEN",
        url="https://example/pull/7",
        head_sha="abc",
        head_branch="feat",
    )
    assert pr.comments == ()
    assert pr.head_sha == "abc"
    assert pr.head_branch == "feat"


def test_pr_list_happy_path(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        return _completed(cmd, stdout=json.dumps(_PR_JSON_LIST_PAYLOAD))

    _install_fake_run(monkeypatch, fake_run)
    [pr] = pr_list("ready-for-agent")
    assert pr.number == 7
    assert pr.title == "Add caching layer"
    assert pr.state == "OPEN"
    assert pr.labels == ["ready-for-agent", "enhancement"]
    assert pr.head_sha == "f" * 40
    assert pr.head_branch == "feature/caching"
    # pr_list MUST leave comments empty per docstring contract (mirrors issue_list).
    assert pr.comments == ()
    # argv shape: gh pr list --state open --label ... --json ...,headRefOid,headRefName
    assert captured["cmd"][0] == "gh"
    assert "pr" in captured["cmd"] and "list" in captured["cmd"]
    assert "--label" in captured["cmd"] and "ready-for-agent" in captured["cmd"]
    assert "--state" in captured["cmd"] and "open" in captured["cmd"]
    json_arg = captured["cmd"][captured["cmd"].index("--json") + 1]
    for f in (
        "number",
        "title",
        "body",
        "labels",
        "state",
        "url",
        "headRefOid",
        "headRefName",
    ):
        assert f in json_arg


def test_pr_list_custom_state_arg_propagates(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        return _completed(cmd, stdout="[]")

    _install_fake_run(monkeypatch, fake_run)
    pr_list("ready-for-agent", state="all")
    assert "all" in captured["cmd"]


def test_pr_list_empty_array_returns_empty_list(monkeypatch) -> None:
    def fake_run(cmd, **kw):
        return _completed(cmd, stdout="[]")

    _install_fake_run(monkeypatch, fake_run)
    assert pr_list("anything") == []


def test_pr_list_non_array_payload_raises_gh_error(monkeypatch) -> None:
    def fake_run(cmd, **kw):
        return _completed(cmd, stdout=json.dumps({"oops": "object"}))

    _install_fake_run(monkeypatch, fake_run)
    with pytest.raises(GhError) as exc_info:
        pr_list("anything")
    assert "expected JSON array" in exc_info.value.stderr_tail


def test_pr_view_happy_path_includes_comments_and_head(monkeypatch) -> None:
    captured: dict[str, Any] = {}
    payload = {
        "number": 7,
        "title": "Add caching layer",
        "body": "## Summary\nWIP",
        "labels": [{"name": "ready-for-agent"}],
        "state": "OPEN",
        "url": "https://github.com/x/y/pull/7",
        "headRefOid": "a" * 40,
        "headRefName": "feature/caching",
        "comments": [
            {
                "author": {"login": "triage-bot"},
                "body": "## Agent Brief\nDo X",
                "createdAt": "2026-05-16T00:00:00Z",
            }
        ],
    }

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        return _completed(cmd, stdout=json.dumps(payload))

    _install_fake_run(monkeypatch, fake_run)
    pr = pr_view(7)
    assert pr.number == 7
    assert pr.head_sha == "a" * 40
    assert pr.head_branch == "feature/caching"
    assert len(pr.comments) == 1
    assert pr.comments[0].author == "triage-bot"
    assert pr.comments[0].body.startswith("## Agent Brief")
    # argv requests comments + head refs in one --json field.
    json_arg = captured["cmd"][captured["cmd"].index("--json") + 1]
    assert "comments" in json_arg and "headRefOid" in json_arg


def test_pr_view_null_body_and_missing_head_normalised(monkeypatch) -> None:
    payload = {
        "number": 8,
        "title": "t",
        "body": None,
        "labels": [],
        "state": "OPEN",
        "url": "u",
        # headRefOid / headRefName absent → normalised to "".
        "comments": [],
    }

    def fake_run(cmd, **kw):
        return _completed(cmd, stdout=json.dumps(payload))

    _install_fake_run(monkeypatch, fake_run)
    pr = pr_view(8)
    assert pr.body == ""
    assert pr.head_sha == ""
    assert pr.head_branch == ""


def test_pr_view_nonzero_exit_raises_gh_error(monkeypatch) -> None:
    def fake_run(cmd, **kw):
        return _completed(cmd, code=1, stderr="no pull requests found\n")

    _install_fake_run(monkeypatch, fake_run)
    with pytest.raises(GhError) as exc_info:
        pr_view(999)
    assert exc_info.value.returncode == 1


def test_parse_pr_non_dict_raises_gh_error() -> None:
    with pytest.raises(GhError) as exc_info:
        gh._parse_pr(["not", "a", "dict"], ["gh", "pr", "view"])
    assert "expected JSON object for pull request" in exc_info.value.stderr_tail
