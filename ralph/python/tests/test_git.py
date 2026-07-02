"""Tests for ``ralph_afk.git`` (issue #6).

Exercises the typed ``git`` subprocess wrapper against real ``tmp_path``
git repositories. The PRD calls for this approach explicitly: real-git
tests for ``git.py`` are cheap and high-signal, and they catch divergences
between git's real semantics and the Python wrapper that a mock
would silently miss.

Tests are skipped when ``git`` is not on PATH (environment, not regression).

Acceptance criteria reference: issue #6.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from ralph_afk.git import (
    Commit,
    GitClient,
    GitError,
    SubprocessGitClient,
    integration_branch_name,
    lane_branch_name,
)

# --------------------------------------------------------------------------- #
# Environment guard                                                            #
# --------------------------------------------------------------------------- #


pytestmark = pytest.mark.skipif(
    shutil.which("git") is None,
    reason="git not on PATH; ralph_afk.git wrappers cannot be exercised",
)


# --------------------------------------------------------------------------- #
# tmp_path git fixture                                                         #
# --------------------------------------------------------------------------- #


def _init_repo(path: Path) -> None:
    """Initialise a fresh git repo at ``path`` with deterministic config."""
    subprocess.run(
        ["git", "init", "-q", "-b", "main", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    for key, value in (
        ("user.email", "tester@example.com"),
        ("user.name", "Tester"),
        ("commit.gpgsign", "false"),
        # Ensure trailing-newline behaviour is deterministic across hosts.
        ("core.autocrlf", "false"),
    ):
        subprocess.run(
            ["git", "-C", str(path), "config", key, value],
            check=True,
            capture_output=True,
            text=True,
        )


def _commit(
    path: Path,
    message: str,
    *,
    file_name: str = "file.txt",
    content: str | None = None,
) -> str:
    """Make a commit and return its full SHA."""
    if content is None:
        content = message + "-payload"
    (path / file_name).write_text(content)
    subprocess.run(
        ["git", "-C", str(path), "add", file_name],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "commit", "-q", "-m", message],
        check=True,
        capture_output=True,
        text=True,
    )
    completed = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _branch_exists(repo: Path, branch: str) -> bool:
    """Return ``True`` if ``branch`` exists in the repo at ``repo``."""
    completed = subprocess.run(
        ["git", "-C", str(repo), "branch", "--list", branch],
        check=True,
        capture_output=True,
        text=True,
    )
    return bool(completed.stdout.strip())


def _worktree_paths(repo: Path) -> list[str]:
    """Return the worktree paths git tracks for the repo at ``repo``."""
    completed = subprocess.run(
        ["git", "-C", str(repo), "worktree", "list", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    )
    return [
        line[len("worktree ") :]
        for line in completed.stdout.splitlines()
        if line.startswith("worktree ")
    ]


# --------------------------------------------------------------------------- #
# Commit dataclass                                                             #
# --------------------------------------------------------------------------- #


def test_commit_message_property_joins_subject_and_body() -> None:
    """Closure-keyword scanning runs against the full message — both halves."""
    c = Commit(sha="a" * 40, subject="Closes #42", body="See follow-up.", date="2026-05-15")
    assert c.message == "Closes #42\nSee follow-up."


def test_commit_message_property_subject_only_when_body_empty() -> None:
    c = Commit(sha="a" * 40, subject="Single-line commit", body="", date="2026-05-15")
    assert c.message == "Single-line commit"


def test_commit_default_date_is_empty_string() -> None:
    c = Commit(sha="a" * 40, subject="s", body="b")
    assert c.date == ""


# --------------------------------------------------------------------------- #
# repo_root                                                                    #
# --------------------------------------------------------------------------- #


def test_repo_root_resolves_to_top_level(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    expected = tmp_path.resolve()
    assert SubprocessGitClient.discover(start=tmp_path).root == expected


def test_repo_root_works_from_nested_subdirectory(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    sub = tmp_path / "a" / "b" / "c"
    sub.mkdir(parents=True)
    assert SubprocessGitClient.discover(start=sub).root == tmp_path.resolve()


def test_repo_root_raises_outside_repo(tmp_path: Path) -> None:
    """Non-repo directory → GitError, not a generic CalledProcessError."""
    with pytest.raises(GitError) as exc_info:
        SubprocessGitClient.discover(start=tmp_path).root
    assert exc_info.value.returncode != 0


# --------------------------------------------------------------------------- #
# head_sha                                                                     #
# --------------------------------------------------------------------------- #


def test_head_sha_returns_full_40_hex(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    sha = _commit(tmp_path, "init")
    assert SubprocessGitClient(tmp_path).head_sha() == sha
    assert len(sha) == 40
    assert all(ch in "0123456789abcdef" for ch in sha)


def test_head_sha_raises_in_empty_repo(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    # No commits yet → `git rev-parse HEAD` returns non-zero.
    with pytest.raises(GitError):
        SubprocessGitClient(tmp_path).head_sha()


# --------------------------------------------------------------------------- #
# is_dirty                                                                     #
# --------------------------------------------------------------------------- #


def test_is_dirty_false_on_clean_tree(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _commit(tmp_path, "init")
    assert SubprocessGitClient(tmp_path).is_dirty() is False


def test_is_dirty_true_with_unstaged_modification(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _commit(tmp_path, "init", file_name="a.txt", content="v1")
    (tmp_path / "a.txt").write_text("v2")
    assert SubprocessGitClient(tmp_path).is_dirty() is True


def test_is_dirty_true_with_staged_addition(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _commit(tmp_path, "init", file_name="a.txt", content="v1")
    (tmp_path / "b.txt").write_text("new")
    subprocess.run(
        ["git", "-C", str(tmp_path), "add", "b.txt"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert SubprocessGitClient(tmp_path).is_dirty() is True


def test_is_dirty_false_on_untracked_file(
    tmp_path: Path,
) -> None:
    """``is_dirty`` uses ``diff --quiet`` only — untracked files do NOT make it dirty.

    This is a deliberate choice: ``! git diff --quiet || ! git diff
    --cached --quiet`` ignores untracked files, so an untracked file alone
    does not abort the next iteration.
    """
    _init_repo(tmp_path)
    _commit(tmp_path, "init")
    (tmp_path / "untracked.txt").write_text("nobody added me")
    assert SubprocessGitClient(tmp_path).is_dirty() is False


def test_is_dirty_raises_when_git_returns_error_exit_code(
    tmp_path: Path,
) -> None:
    """``git diff`` outside a repo returns rc > 1 — we raise GitError."""
    with pytest.raises(GitError):
        SubprocessGitClient(tmp_path).is_dirty()


# --------------------------------------------------------------------------- #
# commits_between                                                              #
# --------------------------------------------------------------------------- #


def test_commits_between_returns_only_new_commits(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    sha1 = _commit(tmp_path, "first")
    sha2 = _commit(tmp_path, "second")
    sha3 = _commit(tmp_path, "third")
    commits = SubprocessGitClient(tmp_path).commits_between(sha1, sha3)
    assert len(commits) == 2
    # Newest first, mirroring `git log` default.
    assert commits[0].sha == sha3
    assert commits[0].subject == "third"
    assert commits[1].sha == sha2


def test_commits_between_returns_empty_when_pre_equals_head(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    sha = _commit(tmp_path, "init")
    assert SubprocessGitClient(tmp_path).commits_between(sha, sha) == []


def test_commits_between_preserves_subject_and_body_separately(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    sha1 = _commit(tmp_path, "first")
    sha2 = _commit(
        tmp_path,
        # Multi-line message: subject + blank line + body.
        "Closes #42\n\nLine 1 of body.\nLine 2 of body.",
        file_name="b.txt",
    )
    commits = SubprocessGitClient(tmp_path).commits_between(sha1, sha2)
    assert len(commits) == 1
    c = commits[0]
    assert c.subject == "Closes #42"
    assert "Line 1 of body." in c.body
    assert "Line 2 of body." in c.body
    # Body must NOT contain the subject line (separation invariant).
    assert not c.body.startswith("Closes #42")


def test_commits_between_message_property_carries_closure_keyword_from_subject(
    tmp_path: Path,
) -> None:
    """Closure keywords in the SUBJECT are visible via Commit.message.

    This is the load-bearing invariant for the auto-close backstop —
    wrapper.extract_close_refs scans commit.message, which must include
    both subject and body so a `Fixes #6` in the subject is caught.
    """
    _init_repo(tmp_path)
    sha1 = _commit(tmp_path, "first")
    sha2 = _commit(tmp_path, "Fixes #6", file_name="b.txt")
    commits = SubprocessGitClient(tmp_path).commits_between(sha1, sha2)
    assert "Fixes #6" in commits[0].message


def test_commits_between_message_property_carries_closure_keyword_from_body(
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path)
    sha1 = _commit(tmp_path, "first")
    sha2 = _commit(
        tmp_path,
        "feat(something): land slice\n\nResolves #6.",
        file_name="b.txt",
    )
    commits = SubprocessGitClient(tmp_path).commits_between(sha1, sha2)
    assert "Resolves #6" in commits[0].message


def test_commits_between_raises_on_invalid_sha(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _commit(tmp_path, "init")
    with pytest.raises(GitError):
        SubprocessGitClient(tmp_path).commits_between("deadbeef" * 5, "HEAD")


# --------------------------------------------------------------------------- #
# recent_commits                                                               #
# --------------------------------------------------------------------------- #


def test_recent_commits_returns_newest_first(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _commit(tmp_path, "first")
    sha2 = _commit(tmp_path, "second")
    sha3 = _commit(tmp_path, "third")
    commits = SubprocessGitClient(tmp_path).recent_commits(2)
    assert [c.sha for c in commits] == [sha3, sha2]


def test_recent_commits_clamps_to_repo_size(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _commit(tmp_path, "only")
    commits = SubprocessGitClient(tmp_path).recent_commits(10)
    assert len(commits) == 1


def test_recent_commits_zero_or_negative_n_returns_empty(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _commit(tmp_path, "init")
    assert SubprocessGitClient(tmp_path).recent_commits(0) == []
    assert SubprocessGitClient(tmp_path).recent_commits(-1) == []


def test_recent_commits_populates_date_field(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _commit(tmp_path, "init")
    [c] = SubprocessGitClient(tmp_path).recent_commits(1)
    # YYYY-MM-DD via --date=short
    assert len(c.date) == 10
    assert c.date.count("-") == 2


# --------------------------------------------------------------------------- #
# range_count                                                                  #
# --------------------------------------------------------------------------- #


def test_range_count_matches_actual_commit_count(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    sha1 = _commit(tmp_path, "first")
    _commit(tmp_path, "second")
    _commit(tmp_path, "third")
    head = SubprocessGitClient(tmp_path).head_sha()
    assert SubprocessGitClient(tmp_path).range_count(sha1, head) == 2


def test_range_count_returns_zero_when_pre_equals_head(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    sha = _commit(tmp_path, "init")
    assert SubprocessGitClient(tmp_path).range_count(sha, sha) == 0


def test_range_count_raises_on_invalid_sha(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _commit(tmp_path, "init")
    with pytest.raises(GitError):
        SubprocessGitClient(tmp_path).range_count("deadbeef" * 5, "HEAD")


# --------------------------------------------------------------------------- #
# Parser robustness                                                            #
# --------------------------------------------------------------------------- #


def test_parser_handles_commit_with_empty_body(tmp_path: Path) -> None:
    """Subject-only commit (no blank-line body) round-trips cleanly."""
    _init_repo(tmp_path)
    sha1 = _commit(tmp_path, "subject-only")
    [c] = SubprocessGitClient(tmp_path).recent_commits(1)
    assert c.sha == sha1
    assert c.subject == "subject-only"
    assert c.body == ""


def test_parser_handles_commit_with_multiline_body(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    multi_line = (
        "feat(foo): land slice\n"
        "\n"
        "Decision: A vs B.\n"
        "- bullet one\n"
        "- bullet two\n"
        "\n"
        "Closes #42."
    )
    sha = _commit(tmp_path, multi_line, file_name="x.txt")
    [c] = SubprocessGitClient(tmp_path).recent_commits(1)
    assert c.sha == sha
    assert c.subject == "feat(foo): land slice"
    assert "Decision: A vs B." in c.body
    assert "Closes #42." in c.body


# --------------------------------------------------------------------------- #
# GitError shape                                                               #
# --------------------------------------------------------------------------- #


def test_git_error_carries_command_returncode_stderr_tail() -> None:
    e = GitError(["git", "diff", "--quiet"], 2, "fatal: not a git repository")
    assert e.command == ("git", "diff", "--quiet")
    assert e.returncode == 2
    assert "fatal: not a git repository" in e.stderr_tail
    assert "git diff --quiet" in str(e)


# --------------------------------------------------------------------------- #
# current_branch / switch                                                     #
# --------------------------------------------------------------------------- #


def test_current_branch_returns_named_branch(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _commit(tmp_path, "init")
    assert SubprocessGitClient(tmp_path).current_branch() == "main"


def test_current_branch_returns_none_on_detached_head(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    sha = _commit(tmp_path, "init")
    subprocess.run(
        ["git", "-C", str(tmp_path), "checkout", "-q", sha],
        check=True,
        capture_output=True,
        text=True,
    )
    assert SubprocessGitClient(tmp_path).current_branch() is None


def test_switch_checks_out_existing_branch(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _commit(tmp_path, "init")
    subprocess.run(
        ["git", "-C", str(tmp_path), "branch", "feature/x"],
        check=True,
        capture_output=True,
        text=True,
    )
    SubprocessGitClient(tmp_path).switch("feature/x")
    assert SubprocessGitClient(tmp_path).current_branch() == "feature/x"
    # Round-trip back to base.
    SubprocessGitClient(tmp_path).switch("main")
    assert SubprocessGitClient(tmp_path).current_branch() == "main"


def test_switch_raises_for_missing_branch(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _commit(tmp_path, "init")
    with pytest.raises(GitError):
        SubprocessGitClient(tmp_path).switch("does-not-exist")


# --------------------------------------------------------------------------- #
# has_untracked / add_all / commit  (issue #32 — runner Checkpoint)           #
# --------------------------------------------------------------------------- #


def test_has_untracked_false_on_clean_tree(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _commit(tmp_path, "init")
    assert SubprocessGitClient(tmp_path).has_untracked() is False


def test_has_untracked_true_with_untracked_file(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _commit(tmp_path, "init")
    (tmp_path / "new.txt").write_text("nobody added me")
    assert SubprocessGitClient(tmp_path).has_untracked() is True


def test_has_untracked_false_for_modification_only(tmp_path: Path) -> None:
    """A modified *tracked* file is dirty, not untracked."""
    _init_repo(tmp_path)
    _commit(tmp_path, "init", file_name="a.txt", content="v1")
    (tmp_path / "a.txt").write_text("v2")
    assert SubprocessGitClient(tmp_path).has_untracked() is False
    assert SubprocessGitClient(tmp_path).is_dirty() is True


def test_has_untracked_honours_gitignore(tmp_path: Path) -> None:
    """An ignored untracked file does not count (``--exclude-standard``)."""
    _init_repo(tmp_path)
    (tmp_path / ".gitignore").write_text("ignored/\n*.log\n")
    _commit(tmp_path, "init", file_name=".gitignore", content="ignored/\n*.log\n")
    (tmp_path / "ignored").mkdir()
    (tmp_path / "ignored" / "x.txt").write_text("hidden")
    (tmp_path / "debug.log").write_text("noise")
    assert SubprocessGitClient(tmp_path).has_untracked() is False


def test_has_untracked_raises_outside_repo(tmp_path: Path) -> None:
    with pytest.raises(GitError):
        SubprocessGitClient(tmp_path).has_untracked()


def test_add_all_stages_modification_and_untracked(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _commit(tmp_path, "init", file_name="a.txt", content="v1")
    (tmp_path / "a.txt").write_text("v2")
    (tmp_path / "b.txt").write_text("brand new")

    SubprocessGitClient(tmp_path).add_all()

    # Everything is now staged: nothing untracked, and a staged diff exists.
    assert SubprocessGitClient(tmp_path).has_untracked() is False
    staged = subprocess.run(
        ["git", "-C", str(tmp_path), "diff", "--cached", "--name-only"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()
    assert sorted(staged) == ["a.txt", "b.txt"]


def test_add_all_honours_gitignore(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / ".gitignore").write_text("*.log\n")
    _commit(tmp_path, "init", file_name=".gitignore", content="*.log\n")
    (tmp_path / "keep.txt").write_text("tracked")
    (tmp_path / "debug.log").write_text("ignored")

    SubprocessGitClient(tmp_path).add_all()

    staged = subprocess.run(
        ["git", "-C", str(tmp_path), "diff", "--cached", "--name-only"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()
    assert staged == ["keep.txt"]


def test_commit_creates_commit_and_returns_head_sha(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _commit(tmp_path, "init")
    (tmp_path / "c.txt").write_text("payload")
    SubprocessGitClient(tmp_path).add_all()

    sha = SubprocessGitClient(tmp_path).commit("a runner checkpoint")

    assert sha == SubprocessGitClient(tmp_path).head_sha()
    assert len(sha) == 40 and all(ch in "0123456789abcdef" for ch in sha)
    recorded = SubprocessGitClient(tmp_path).recent_commits(1)[0]
    assert recorded.subject == "a runner checkpoint"


def test_commit_preserves_multi_paragraph_message(tmp_path: Path) -> None:
    """Subject + body + trailer paragraphs survive a single ``-m``."""
    _init_repo(tmp_path)
    _commit(tmp_path, "init")
    (tmp_path / "d.txt").write_text("payload")
    SubprocessGitClient(tmp_path).add_all()

    message = "Checkpoint subject\n\nA body paragraph.\n\nRalph-Checkpoint: 32"
    SubprocessGitClient(tmp_path).commit(message)

    recorded = SubprocessGitClient(tmp_path).recent_commits(1)[0]
    assert recorded.subject == "Checkpoint subject"
    assert "A body paragraph." in recorded.body
    assert "Ralph-Checkpoint: 32" in recorded.body


def test_commit_raises_when_nothing_staged(tmp_path: Path) -> None:
    """``git commit`` with an empty index exits non-zero -> GitError."""
    _init_repo(tmp_path)
    _commit(tmp_path, "init")
    with pytest.raises(GitError):
        SubprocessGitClient(tmp_path).commit("nothing to do")


# --------------------------------------------------------------------------- #
# push  (issue #35 — auto-push durability net)                                 #
# --------------------------------------------------------------------------- #


def _init_bare(path: Path) -> None:
    """Initialise a bare repo at ``path`` to serve as an upstream remote."""
    subprocess.run(
        ["git", "init", "--bare", "-q", "-b", "main", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )


def _wire_upstream(work: Path, remote: Path) -> None:
    """Add ``remote`` as ``origin`` and seed it as ``main``'s upstream."""
    subprocess.run(
        ["git", "-C", str(work), "remote", "add", "origin", str(remote)],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "-C", str(work), "push", "-u", "-q", "origin", "main"],
        check=True,
        capture_output=True,
        text=True,
    )


def test_push_sends_new_commits_to_upstream(tmp_path: Path) -> None:
    """A bare ``git push`` advances the configured upstream to local HEAD."""
    remote = tmp_path / "origin.git"
    work = tmp_path / "work"
    work.mkdir()
    _init_bare(remote)
    _init_repo(work)
    _commit(work, "init")
    _wire_upstream(work, remote)
    # A brand-new local commit the upstream has not seen yet.
    local_head = _commit(work, "second")

    SubprocessGitClient(work).push()

    # Verify via ls-remote (a transport op, like push itself) rather than
    # bare-repo path discovery, which a host `safe.bareRepository=explicit`
    # config refuses. This asserts what actually landed ON the remote.
    ls_remote = subprocess.run(
        ["git", "-C", str(work), "ls-remote", "origin", "refs/heads/main"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    remote_head = ls_remote.split()[0] if ls_remote else ""
    assert remote_head == local_head


def test_push_is_noop_when_upstream_already_current(tmp_path: Path) -> None:
    """Pushing with nothing new ('Everything up-to-date') exits 0, not an error."""
    remote = tmp_path / "origin.git"
    work = tmp_path / "work"
    work.mkdir()
    _init_bare(remote)
    _init_repo(work)
    _commit(work, "init")
    _wire_upstream(work, remote)

    # No new commit: a redundant push is a safe no-op, never a GitError.
    SubprocessGitClient(work).push()


def test_push_raises_without_upstream(tmp_path: Path) -> None:
    """No remote / no upstream → ``git push`` fails → GitError (non-fatal upstream)."""
    _init_repo(tmp_path)
    _commit(tmp_path, "init")
    with pytest.raises(GitError):
        SubprocessGitClient(tmp_path).push()


# --------------------------------------------------------------------------- #
# GitClient conformance + Checkpoint-exclusion (the seam contract)             #
# --------------------------------------------------------------------------- #


def test_subprocess_git_client_satisfies_gitclient_protocol(tmp_path: Path) -> None:
    """The adapter satisfies the ``@runtime_checkable`` ``GitClient`` structurally."""
    assert isinstance(SubprocessGitClient(tmp_path), GitClient)
    # A bare object missing the mechanics is rejected — the check has teeth.
    assert not isinstance(object(), GitClient)


def test_commits_between_excludes_a_runner_checkpoint(tmp_path: Path) -> None:
    """A Checkpoint authored *after* the post-iteration head read is excluded.

    Mirrors the loop's ordering: read the ``pre`` head, the agent commits, read
    the post-iteration ``head``, *then* the runner authors its Checkpoint.
    Because the Checkpoint lands after ``head`` was captured,
    ``commits_between(pre, head)`` returns only the agent's commit — the
    load-bearing Strike invariant (a Checkpoint is not progress).
    """
    _init_repo(tmp_path)
    pre = _commit(tmp_path, "base")
    git = SubprocessGitClient(tmp_path)

    # The agent authors real work; the loop reads the post-iteration head.
    agent_sha = _commit(
        tmp_path, "feat: agent work\n\nCloses #42", file_name="a.txt"
    )
    head = git.head_sha()
    assert head == agent_sha

    # The runner Checkpoints *after* ``head`` is read.
    (tmp_path / "leftover.txt").write_text("uncommitted scratch")
    git.add_all()
    checkpoint_sha = git.commit("chore(ralph): checkpoint")
    assert checkpoint_sha != head

    between = git.commits_between(pre, head)
    assert [c.sha for c in between] == [agent_sha]
    assert all(c.sha != checkpoint_sha for c in between)


# --------------------------------------------------------------------------- #
# lane_branch_name (Parallel-mode Lane branch naming, ADR-0005 / ADR-0008)     #
# --------------------------------------------------------------------------- #


def test_lane_branch_name_follows_copiloop_convention() -> None:
    """A Lane's branch is ``copiloop/<run_id>/issue-<N>`` (ADR-0005 prefix)."""
    assert lane_branch_name("01JAX7QZ8K9M", 7) == "copiloop/01JAX7QZ8K9M/issue-7"


def test_lane_branch_name_is_pure_string_policy() -> None:
    """Distinct run_ids / issue numbers produce distinct, well-formed branches."""
    assert lane_branch_name("RUNA", 1) == "copiloop/RUNA/issue-1"
    assert lane_branch_name("RUNB", 123) == "copiloop/RUNB/issue-123"
    # run_id and issue number are the only variables; the prefix is fixed.
    assert lane_branch_name("RUNA", 1) != lane_branch_name("RUNA", 2)
    assert lane_branch_name("RUNA", 1) != lane_branch_name("RUNB", 1)


# --------------------------------------------------------------------------- #
# Worktree lifecycle (Parallel-mode Lanes, ADR-0008)                          #
# --------------------------------------------------------------------------- #


def _sibling_worktree_repo(tmp_path: Path) -> tuple[Path, Path, SubprocessGitClient]:
    """Init a repo in ``tmp_path/repo`` with one commit; return (repo, sibling, client).

    ``sibling`` is a directory *outside* the repo (``tmp_path/worktrees``) where a
    Lane worktree lives — never nested inside the repo, per ADR-0008.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _commit(repo, "base", file_name="base.txt")
    return repo, tmp_path / "worktrees", SubprocessGitClient(repo)


def test_add_worktree_creates_branch_from_base_in_sibling_dir(tmp_path: Path) -> None:
    """``add_worktree`` cuts a new branch from base into a sibling dir outside the repo."""
    repo, siblings, client = _sibling_worktree_repo(tmp_path)
    base_head = client.head_sha()
    wt_path = siblings / "lane-7"
    branch = lane_branch_name("RUN123", 7)

    lane = client.add_worktree(wt_path, branch=branch, base="main")

    # On-disk effect: the worktree directory exists, outside the repo tree.
    assert wt_path.is_dir()
    assert repo not in wt_path.parents
    # The returned client is a GitClient bound to the worktree.
    assert isinstance(lane, GitClient)
    assert lane.root == wt_path
    # Branch-from-base: new branch, checked out in the worktree, at base's head.
    assert lane.current_branch() == branch
    assert lane.head_sha() == base_head
    # git now tracks two worktrees (main + the Lane).
    tracked = {Path(p).resolve() for p in _worktree_paths(repo)}
    assert repo.resolve() in tracked
    assert wt_path.resolve() in tracked


def test_worktree_commits_are_isolated_from_the_main_worktree(tmp_path: Path) -> None:
    """A commit in the Lane advances only its own branch; the main worktree is untouched."""
    repo, siblings, client = _sibling_worktree_repo(tmp_path)
    base_head = client.head_sha()
    lane = client.add_worktree(
        siblings / "lane-7", branch=lane_branch_name("RUN", 7), base="main"
    )

    # Work in the Lane's own worktree.
    (lane.root / "lane_only.txt").write_text("lane work")
    assert lane.has_untracked() is True
    # ...the main worktree stays clean (per-worktree probes are independent).
    assert client.has_untracked() is False

    lane.add_all()
    lane_sha = lane.commit("feat: lane work\n\nCloses #7")

    # The Lane branch advanced; the main worktree's HEAD did not move.
    assert lane.head_sha() == lane_sha
    assert lane_sha != base_head
    assert client.head_sha() == base_head
    # Per-Lane commit accounting: the Lane's own pre/post range holds its commit.
    between = lane.commits_between(base_head, lane_sha)
    assert [c.sha for c in between] == [lane_sha]
    assert lane.range_count(base_head, lane_sha) == 1


def test_remove_worktree_deletes_dir_but_keeps_branch(tmp_path: Path) -> None:
    """Teardown removes the worktree dir yet retains the branch as a breadcrumb (ADR-0008)."""
    repo, siblings, client = _sibling_worktree_repo(tmp_path)
    wt_path = siblings / "lane-7"
    branch = lane_branch_name("RUN", 7)
    lane = client.add_worktree(wt_path, branch=branch, base="main")
    lane.add_all()  # nothing staged; worktree is clean

    client.remove_worktree(wt_path)

    assert not wt_path.exists()
    # git no longer tracks the Lane worktree...
    tracked = {Path(p).resolve() for p in _worktree_paths(repo)}
    assert wt_path.resolve() not in tracked
    # ...but the branch survives so a failed Lane leaves a breadcrumb.
    assert _branch_exists(repo, branch) is True


def test_remove_worktree_force_removes_a_dirty_worktree(tmp_path: Path) -> None:
    """``force=True`` tears down a worktree with uncommitted changes; plain remove refuses."""
    repo, siblings, client = _sibling_worktree_repo(tmp_path)
    wt_path = siblings / "lane-7"
    lane = client.add_worktree(
        wt_path, branch=lane_branch_name("RUN", 7), base="main"
    )
    # Dirty the Lane worktree (a tracked-file modification).
    (lane.root / "base.txt").write_text("modified in lane")
    assert lane.is_dirty() is True

    # A plain remove refuses to discard uncommitted work.
    with pytest.raises(GitError):
        client.remove_worktree(wt_path)
    assert wt_path.exists()

    # force=True tears it down anyway.
    client.remove_worktree(wt_path, force=True)
    assert not wt_path.exists()


def test_add_worktree_duplicate_branch_raises(tmp_path: Path) -> None:
    """Re-using a branch name for a second live worktree is a git error, surfaced typed."""
    _repo, siblings, client = _sibling_worktree_repo(tmp_path)
    branch = lane_branch_name("RUN", 7)
    client.add_worktree(siblings / "lane-a", branch=branch, base="main")
    with pytest.raises(GitError):
        client.add_worktree(siblings / "lane-b", branch=branch, base="main")


# --------------------------------------------------------------------------- #
# Integration: merge + delete_branch (Parallel-mode Lanes, ADR-0009)          #
# --------------------------------------------------------------------------- #


def _committed_lane_branch(
    tmp_path: Path, *, issue: int = 7, file_name: str = "lane_only.txt"
) -> tuple[Path, str, SubprocessGitClient]:
    """Create a Lane worktree, commit ``Closes #<issue>`` in it, tear it down.

    Returns ``(repo, branch, client)`` with the Lane branch left as a breadcrumb
    (worktree removed, branch retained) — the state Integration merges from.
    """
    repo, siblings, client = _sibling_worktree_repo(tmp_path)
    branch = lane_branch_name("RUN", issue)
    lane = client.add_worktree(siblings / f"lane-{issue}", branch=branch, base="main")
    (lane.root / file_name).write_text(f"lane {issue} work")
    lane.add_all()
    lane.commit(f"feat: lane work\n\nCloses #{issue}")
    client.remove_worktree(siblings / f"lane-{issue}")
    return repo, branch, client


def test_merge_lands_a_lane_branch_on_base(tmp_path: Path) -> None:
    """``merge`` brings a Lane branch's commits onto the checked-out base branch."""
    repo, branch, client = _committed_lane_branch(tmp_path, issue=7)
    base_head = client.head_sha()

    client.merge(branch)

    # Base advanced and now contains the Lane's ``Closes #7`` commit.
    head = client.head_sha()
    assert head != base_head
    merged = client.commits_between(base_head, head)
    assert any("Closes #7" in c.message for c in merged)


def test_merge_raises_on_conflict(tmp_path: Path) -> None:
    """A conflicting merge surfaces a typed ``GitError`` (auto-resolution is #63)."""
    repo, branch, client = _committed_lane_branch(
        tmp_path, issue=7, file_name="base.txt"
    )
    # Diverge base on the same file the Lane edited, so the merge conflicts.
    _commit(repo, "base edit", file_name="base.txt", content="base version")

    with pytest.raises(GitError):
        client.merge(branch)


def test_delete_branch_removes_an_integrated_branch(tmp_path: Path) -> None:
    """``delete_branch`` drops a merged Lane branch after it lands (ADR-0008)."""
    repo, branch, client = _committed_lane_branch(tmp_path, issue=7)
    client.merge(branch)
    assert _branch_exists(repo, branch) is True

    client.delete_branch(branch)

    assert _branch_exists(repo, branch) is False


def test_delete_branch_raises_for_unknown_branch(tmp_path: Path) -> None:
    """Deleting a non-existent branch surfaces a typed ``GitError``."""
    _repo, _siblings, client = _sibling_worktree_repo(tmp_path)
    with pytest.raises(GitError):
        client.delete_branch(lane_branch_name("RUN", 999))

def _tree_sha(repo: Path, rev: str) -> str:
    """Return the tree SHA a revision points at (for a tree-equality assertion)."""
    completed = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", f"{rev}^{{tree}}"],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def test_revert_merge_undoes_a_landing_keeping_base_green(tmp_path: Path) -> None:
    """``revert_merge`` reverts the ``HEAD`` merge, restoring the pre-merge tree (#63)."""
    repo, branch, client = _committed_lane_branch(tmp_path, issue=7)
    base_head = client.head_sha()
    client.merge(branch)
    merged_head = client.head_sha()
    assert merged_head != base_head  # the --no-ff merge advanced base

    client.revert_merge()

    # A revert commit is appended (append-only: HEAD advances again, never a
    # destructive reset), but the Lane's change is undone — the tree matches the
    # pre-merge base, so the base branch is green again.
    reverted_head = client.head_sha()
    assert reverted_head != merged_head
    assert _tree_sha(repo, "HEAD") == _tree_sha(repo, base_head)


def test_abort_merge_undoes_an_in_progress_conflicted_merge(tmp_path: Path) -> None:
    """``abort_merge`` restores base after a conflicting merge left it mid-merge (#63)."""
    repo, branch, client = _committed_lane_branch(
        tmp_path, issue=7, file_name="base.txt"
    )
    # Diverge base on the same file the Lane edited, so the merge conflicts.
    _commit(repo, "base edit", file_name="base.txt", content="base version")
    base_head = client.head_sha()
    with pytest.raises(GitError):
        client.merge(branch)  # conflicts, leaves the repo mid-merge

    client.abort_merge()

    # Base is exactly where it was and no longer mid-merge.
    assert client.head_sha() == base_head
    assert not (repo / ".git" / "MERGE_HEAD").exists()


def test_integration_branch_name_uses_the_integrate_convention() -> None:
    """The auto-resolution branch is distinct from the retained Lane branch (#63)."""
    assert (
        integration_branch_name("RUN123", 42)
        == "copiloop/RUN123/integrate/issue-42"
    )
    # Distinct from the Lane breadcrumb branch for the same issue.
    assert integration_branch_name("RUN123", 42) != lane_branch_name("RUN123", 42)
