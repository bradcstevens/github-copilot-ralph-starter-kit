"""Tests for the reusable test fakes in ``tests/fakes.py`` (issues #46, #47).

The loop and source suites lean on these fakes to substitute a whole seam with
one object; these tests pin each fake's *own* contract so a drifting fake cannot
quietly invalidate the suites that build on it — chiefly the
**checkpoint-exclusion** invariant that keeps the Strike rule honest (git), and
the **close-flips-to-CLOSED** modelling the auto-close backstop leans on (gh).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ralph_afk.gh import GhError, GitHubClient, Issue, PullRequest, Repo
from ralph_afk.git import Commit, GitClient, GitError
from tests.fakes import FakeGitClient, FakeGitHubClient


def test_fake_git_client_satisfies_gitclient_protocol(tmp_path: Path) -> None:
    """The fake satisfies the ``@runtime_checkable`` ``GitClient`` structurally."""
    assert isinstance(FakeGitClient(tmp_path), GitClient)
    assert not isinstance(object(), GitClient)


def test_head_and_recent_track_the_linear_log(tmp_path: Path) -> None:
    seed = Commit(sha="s0", subject="root", body="", date="2026-01-01")
    git = FakeGitClient(tmp_path, commits=[seed])
    assert git.head_sha() == "s0"
    git.simulate_agent_commit(subject="first", sha="s1")
    b = git.simulate_agent_commit(subject="second", sha="s2")
    assert git.head_sha() == b == "s2"
    # recent_commits is newest-first and bounded by n.
    assert [c.sha for c in git.recent_commits(2)] == ["s2", "s1"]
    assert [c.sha for c in git.recent_commits(10)] == ["s2", "s1", "s0"]
    assert git.recent_commits(0) == []
    assert git.recent_commits(-1) == []


def test_commits_between_is_positional_and_excludes_checkpoint(
    tmp_path: Path,
) -> None:
    """The load-bearing invariant: a Checkpoint committed after ``head`` is read
    is positionally after ``head`` in the log, so it is excluded from the range."""
    git = FakeGitClient(
        tmp_path, commits=[Commit(sha="base", subject="b", body="")]
    )
    pre = git.head_sha()
    git.simulate_agent_commit(subject="feat", body="Closes #42", sha="agent")
    head = git.head_sha()
    # Runner Checkpoint lands AFTER head is captured.
    checkpoint = git.commit("chore(ralph): checkpoint")
    between = git.commits_between(pre, head)
    assert [c.sha for c in between] == ["agent"]
    assert checkpoint not in [c.sha for c in between]
    assert git.range_count(pre, head) == 1
    # Same pre/head → empty (no self-range).
    assert git.commits_between(head, head) == []
    assert git.range_count(head, head) == 0


def test_commit_appends_records_and_returns_new_head(tmp_path: Path) -> None:
    git = FakeGitClient(tmp_path)
    before = git.head_sha()
    sha = git.commit("subject line\n\nbody text")
    assert sha == git.head_sha()
    assert sha != before
    assert git.commit_messages == ["subject line\n\nbody text"]
    recorded = git.recent_commits(1)[0]
    assert recorded.subject == "subject line"
    assert recorded.body == "body text"


def test_add_all_and_push_are_recorded_spies(tmp_path: Path) -> None:
    git = FakeGitClient(tmp_path)
    git.add_all()
    git.add_all()
    git.push()
    assert git.add_all_calls == 2
    assert git.push_calls == 1


def test_injected_commit_error_is_raised_after_recording(tmp_path: Path) -> None:
    boom = GitError(["git", "commit"], 1, "nothing to commit")
    git = FakeGitClient(tmp_path, commit_error=boom)
    with pytest.raises(GitError):
        git.commit("checkpoint")
    # The message is still recorded: the loop treats a Checkpoint failure as
    # non-fatal, so the spy must witness the attempt.
    assert git.commit_messages == ["checkpoint"]


def test_injected_push_error_is_raised_after_recording(tmp_path: Path) -> None:
    boom = GitError(["git", "push"], 1, "no upstream")
    git = FakeGitClient(tmp_path, push_error=boom)
    with pytest.raises(GitError):
        git.push()
    assert git.push_calls == 1


def test_dirty_and_untracked_are_test_controlled_and_persist(
    tmp_path: Path,
) -> None:
    git = FakeGitClient(tmp_path, dirty=True, untracked=True)
    assert git.is_dirty() is True
    assert git.has_untracked() is True
    # commit does NOT clear them (a real agent re-dirties each iteration), so a
    # multi-iteration test that leaves dirty=True Checkpoints every iteration.
    git.add_all()
    git.commit("checkpoint")
    assert git.is_dirty() is True
    assert git.has_untracked() is True


def test_branch_switch_is_recorded(tmp_path: Path) -> None:
    git = FakeGitClient(tmp_path, branch="main")
    assert git.current_branch() == "main"
    git.switch("feature/x")
    assert git.current_branch() == "feature/x"
    assert git.switch_calls == ["feature/x"]


def test_commits_between_unknown_sha_raises(tmp_path: Path) -> None:
    git = FakeGitClient(tmp_path)
    with pytest.raises(GitError):
        git.commits_between("deadbeef", git.head_sha())


# ---------------------------------------------------------------------------
# FakeGitHubClient (the gh seam, #47)
# ---------------------------------------------------------------------------


def _issue(number: int, *, state: str = "OPEN") -> Issue:
    return Issue(
        number=number,
        title=f"issue {number}",
        body="body",
        labels=["ready-for-agent"],
        state=state,
        url=f"https://example/issues/{number}",
        comments=(),
    )


def _pr(number: int, *, state: str = "OPEN", head_sha: str = "sha0") -> PullRequest:
    return PullRequest(
        number=number,
        title=f"pr {number}",
        body="body",
        labels=["ready-for-agent"],
        state=state,
        url=f"https://example/pull/{number}",
        head_sha=head_sha,
        head_branch=f"feature/{number}",
        comments=(),
    )


def test_fake_github_client_satisfies_githubclient_protocol() -> None:
    """The fake satisfies the ``@runtime_checkable`` ``GitHubClient`` structurally."""
    assert isinstance(FakeGitHubClient(), GitHubClient)
    assert not isinstance(object(), GitHubClient)


def test_auth_and_repo_defaults_and_overrides() -> None:
    default = FakeGitHubClient()
    assert default.auth_status() is True
    assert isinstance(default.repo_view(), Repo)
    signed_out = FakeGitHubClient(authed=False, repo=Repo(owner="o", name="n", default_branch="dev"))
    assert signed_out.auth_status() is False
    assert signed_out.repo_view().nwo == "o/n"


def test_issue_list_and_view_derive_from_one_store() -> None:
    gh = FakeGitHubClient(issues=[_issue(42), _issue(43, state="CLOSED")])
    # issue_list filters by state (open by default); the numbers stay consistent
    # with what issue_view returns.
    assert [i.number for i in gh.issue_list("ready-for-agent")] == [42]
    assert {i.number for i in gh.issue_list("ready-for-agent", "all")} == {42, 43}
    assert [i.number for i in gh.issue_list("ready-for-agent", "closed")] == [43]
    assert gh.issue_view(42).state == "OPEN"
    assert gh.issue_list_calls == [
        ("ready-for-agent", "open"),
        ("ready-for-agent", "all"),
        ("ready-for-agent", "closed"),
    ]
    assert gh.issue_view_calls == [42]


def test_issue_close_records_and_flips_state_to_closed() -> None:
    """The auto-close backstop leans on this: a recorded action that lands the close."""
    gh = FakeGitHubClient(issues=[_issue(42)])
    gh.issue_close(42, "done via Closes #42")
    # Recorded as a pure mechanic...
    assert gh.issue_close_calls == [(42, "done via Closes #42")]
    # ...and the close lands, exactly as real ``gh`` would (a later view sees it).
    assert gh.issue_view(42).state == "CLOSED"
    assert gh.issue_list("ready-for-agent") == []


def test_pr_list_view_and_head_advance() -> None:
    gh = FakeGitHubClient(prs=[_pr(7, head_sha="old"), _pr(8, state="MERGED")])
    assert [p.number for p in gh.pr_list("ready-for-agent")] == [7]
    assert gh.pr_view(7).head_sha == "old"
    # set_pr_head models an agent push between two pr_view reads.
    gh.set_pr_head(7, "new")
    assert gh.pr_view(7).head_sha == "new"
    assert gh.pr_view_calls == [7, 7]


@pytest.mark.parametrize(
    "kwargs, call",
    [
        ({"auth_status_error": GhError(["gh"], 1, "boom")}, lambda gh: gh.auth_status()),
        ({"repo_view_error": GhError(["gh"], 1, "boom")}, lambda gh: gh.repo_view()),
        ({"issue_list_error": GhError(["gh"], 1, "boom")}, lambda gh: gh.issue_list("l")),
        ({"issue_view_errors": {42: GhError(["gh"], 1, "boom")}}, lambda gh: gh.issue_view(42)),
        ({"issue_close_errors": {42: GhError(["gh"], 1, "boom")}}, lambda gh: gh.issue_close(42, "c")),
        ({"pr_list_error": GhError(["gh"], 1, "boom")}, lambda gh: gh.pr_list("l")),
        ({"pr_view_errors": {7: GhError(["gh"], 1, "boom")}}, lambda gh: gh.pr_view(7)),
    ],
)
def test_injected_gh_errors_are_raised(kwargs: dict, call) -> None:
    gh = FakeGitHubClient(issues=[_issue(42)], prs=[_pr(7)], **kwargs)
    with pytest.raises(GhError):
        call(gh)


def test_per_number_errors_leave_the_rest_of_the_pool_working() -> None:
    """A per-number view/close failure isolates to that number; others proceed."""
    gh = FakeGitHubClient(
        issues=[_issue(42), _issue(43)],
        issue_view_errors={43: GhError(["gh"], 1, "boom")},
    )
    assert gh.issue_view(42).number == 42  # sibling unaffected
    with pytest.raises(GhError):
        gh.issue_view(43)


def test_issue_views_override_diverges_view_from_list() -> None:
    """``issue_views`` returns a *different* body than the list (re-verify path)."""
    listed = _issue(42)  # carries a body
    full = Issue(
        number=42,
        title="issue 42",
        body="no discriminator anymore",
        labels=["ready-for-agent"],
        state="OPEN",
        url="u",
        comments=(),
    )
    gh = FakeGitHubClient(issues=[listed], issue_views={42: full})
    assert gh.issue_list("ready-for-agent")[0].body == "body"
    assert gh.issue_view(42).body == "no discriminator anymore"


def test_issue_close_error_still_records_the_attempt() -> None:
    """The source treats a close failure as non-fatal, so the spy must witness it."""
    gh = FakeGitHubClient(
        issues=[_issue(42)], issue_close_errors={42: GhError(["gh"], 1, "boom")}
    )
    with pytest.raises(GhError):
        gh.issue_close(42, "c")
    assert gh.issue_close_calls == [(42, "c")]
    # The state did not flip (the close never landed).
    assert gh.issue_view(42).state == "OPEN"


def test_unknown_number_views_raise_gherror() -> None:
    gh = FakeGitHubClient()
    with pytest.raises(GhError):
        gh.issue_view(999)
    with pytest.raises(GhError):
        gh.pr_view(999)
    # issue_close on an unknown number is a silent no-op (recorded, nothing to flip).
    gh.issue_close(999, "c")
    assert gh.issue_close_calls == [(999, "c")]
