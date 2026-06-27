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

from ralph_afk import git
from ralph_afk.git import (
    Commit,
    GitError,
    add_all,
    commit,
    commits_between,
    current_branch,
    has_untracked,
    head_sha,
    is_dirty,
    range_count,
    recent_commits,
    repo_root,
    switch,
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
    assert repo_root(start=tmp_path) == expected


def test_repo_root_works_from_nested_subdirectory(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    sub = tmp_path / "a" / "b" / "c"
    sub.mkdir(parents=True)
    assert repo_root(start=sub) == tmp_path.resolve()


def test_repo_root_raises_outside_repo(tmp_path: Path) -> None:
    """Non-repo directory → GitError, not a generic CalledProcessError."""
    with pytest.raises(GitError) as exc_info:
        repo_root(start=tmp_path)
    assert exc_info.value.returncode != 0


# --------------------------------------------------------------------------- #
# head_sha                                                                     #
# --------------------------------------------------------------------------- #


def test_head_sha_returns_full_40_hex(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    sha = _commit(tmp_path, "init")
    assert head_sha(start=tmp_path) == sha
    assert len(sha) == 40
    assert all(ch in "0123456789abcdef" for ch in sha)


def test_head_sha_raises_in_empty_repo(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    # No commits yet → `git rev-parse HEAD` returns non-zero.
    with pytest.raises(GitError):
        head_sha(start=tmp_path)


# --------------------------------------------------------------------------- #
# is_dirty                                                                     #
# --------------------------------------------------------------------------- #


def test_is_dirty_false_on_clean_tree(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _commit(tmp_path, "init")
    assert is_dirty(start=tmp_path) is False


def test_is_dirty_true_with_unstaged_modification(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _commit(tmp_path, "init", file_name="a.txt", content="v1")
    (tmp_path / "a.txt").write_text("v2")
    assert is_dirty(start=tmp_path) is True


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
    assert is_dirty(start=tmp_path) is True


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
    assert is_dirty(start=tmp_path) is False


def test_is_dirty_raises_when_git_returns_error_exit_code(
    tmp_path: Path,
) -> None:
    """``git diff`` outside a repo returns rc > 1 — we raise GitError."""
    with pytest.raises(GitError):
        is_dirty(start=tmp_path)


# --------------------------------------------------------------------------- #
# commits_between                                                              #
# --------------------------------------------------------------------------- #


def test_commits_between_returns_only_new_commits(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    sha1 = _commit(tmp_path, "first")
    sha2 = _commit(tmp_path, "second")
    sha3 = _commit(tmp_path, "third")
    commits = commits_between(sha1, sha3, start=tmp_path)
    assert len(commits) == 2
    # Newest first, mirroring `git log` default.
    assert commits[0].sha == sha3
    assert commits[0].subject == "third"
    assert commits[1].sha == sha2


def test_commits_between_returns_empty_when_pre_equals_head(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    sha = _commit(tmp_path, "init")
    assert commits_between(sha, sha, start=tmp_path) == []


def test_commits_between_preserves_subject_and_body_separately(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    sha1 = _commit(tmp_path, "first")
    sha2 = _commit(
        tmp_path,
        # Multi-line message: subject + blank line + body.
        "Closes #42\n\nLine 1 of body.\nLine 2 of body.",
        file_name="b.txt",
    )
    commits = commits_between(sha1, sha2, start=tmp_path)
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
    commits = commits_between(sha1, sha2, start=tmp_path)
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
    commits = commits_between(sha1, sha2, start=tmp_path)
    assert "Resolves #6" in commits[0].message


def test_commits_between_raises_on_invalid_sha(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _commit(tmp_path, "init")
    with pytest.raises(GitError):
        commits_between("deadbeef" * 5, "HEAD", start=tmp_path)


# --------------------------------------------------------------------------- #
# recent_commits                                                               #
# --------------------------------------------------------------------------- #


def test_recent_commits_returns_newest_first(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    sha1 = _commit(tmp_path, "first")
    sha2 = _commit(tmp_path, "second")
    sha3 = _commit(tmp_path, "third")
    commits = recent_commits(2, start=tmp_path)
    assert [c.sha for c in commits] == [sha3, sha2]


def test_recent_commits_clamps_to_repo_size(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _commit(tmp_path, "only")
    commits = recent_commits(10, start=tmp_path)
    assert len(commits) == 1


def test_recent_commits_zero_or_negative_n_returns_empty(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _commit(tmp_path, "init")
    assert recent_commits(0, start=tmp_path) == []
    assert recent_commits(-1, start=tmp_path) == []


def test_recent_commits_populates_date_field(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _commit(tmp_path, "init")
    [c] = recent_commits(1, start=tmp_path)
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
    head = head_sha(start=tmp_path)
    assert range_count(sha1, head, start=tmp_path) == 2


def test_range_count_returns_zero_when_pre_equals_head(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    sha = _commit(tmp_path, "init")
    assert range_count(sha, sha, start=tmp_path) == 0


def test_range_count_raises_on_invalid_sha(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _commit(tmp_path, "init")
    with pytest.raises(GitError):
        range_count("deadbeef" * 5, "HEAD", start=tmp_path)


# --------------------------------------------------------------------------- #
# Parser robustness                                                            #
# --------------------------------------------------------------------------- #


def test_parser_handles_commit_with_empty_body(tmp_path: Path) -> None:
    """Subject-only commit (no blank-line body) round-trips cleanly."""
    _init_repo(tmp_path)
    sha1 = _commit(tmp_path, "subject-only")
    [c] = recent_commits(1, start=tmp_path)
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
    [c] = recent_commits(1, start=tmp_path)
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
    assert current_branch(tmp_path) == "main"


def test_current_branch_returns_none_on_detached_head(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    sha = _commit(tmp_path, "init")
    subprocess.run(
        ["git", "-C", str(tmp_path), "checkout", "-q", sha],
        check=True,
        capture_output=True,
        text=True,
    )
    assert current_branch(tmp_path) is None


def test_switch_checks_out_existing_branch(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _commit(tmp_path, "init")
    subprocess.run(
        ["git", "-C", str(tmp_path), "branch", "feature/x"],
        check=True,
        capture_output=True,
        text=True,
    )
    switch("feature/x", tmp_path)
    assert current_branch(tmp_path) == "feature/x"
    # Round-trip back to base.
    switch("main", tmp_path)
    assert current_branch(tmp_path) == "main"


def test_switch_raises_for_missing_branch(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _commit(tmp_path, "init")
    with pytest.raises(GitError):
        switch("does-not-exist", tmp_path)


# --------------------------------------------------------------------------- #
# has_untracked / add_all / commit  (issue #32 — runner Checkpoint)           #
# --------------------------------------------------------------------------- #


def test_has_untracked_false_on_clean_tree(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _commit(tmp_path, "init")
    assert has_untracked(start=tmp_path) is False


def test_has_untracked_true_with_untracked_file(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _commit(tmp_path, "init")
    (tmp_path / "new.txt").write_text("nobody added me")
    assert has_untracked(start=tmp_path) is True


def test_has_untracked_false_for_modification_only(tmp_path: Path) -> None:
    """A modified *tracked* file is dirty, not untracked."""
    _init_repo(tmp_path)
    _commit(tmp_path, "init", file_name="a.txt", content="v1")
    (tmp_path / "a.txt").write_text("v2")
    assert has_untracked(start=tmp_path) is False
    assert is_dirty(start=tmp_path) is True


def test_has_untracked_honours_gitignore(tmp_path: Path) -> None:
    """An ignored untracked file does not count (``--exclude-standard``)."""
    _init_repo(tmp_path)
    (tmp_path / ".gitignore").write_text("ignored/\n*.log\n")
    _commit(tmp_path, "init", file_name=".gitignore", content="ignored/\n*.log\n")
    (tmp_path / "ignored").mkdir()
    (tmp_path / "ignored" / "x.txt").write_text("hidden")
    (tmp_path / "debug.log").write_text("noise")
    assert has_untracked(start=tmp_path) is False


def test_has_untracked_raises_outside_repo(tmp_path: Path) -> None:
    with pytest.raises(GitError):
        has_untracked(start=tmp_path)


def test_add_all_stages_modification_and_untracked(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _commit(tmp_path, "init", file_name="a.txt", content="v1")
    (tmp_path / "a.txt").write_text("v2")
    (tmp_path / "b.txt").write_text("brand new")

    add_all(start=tmp_path)

    # Everything is now staged: nothing untracked, and a staged diff exists.
    assert has_untracked(start=tmp_path) is False
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

    add_all(start=tmp_path)

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
    add_all(start=tmp_path)

    sha = commit("a runner checkpoint", start=tmp_path)

    assert sha == head_sha(start=tmp_path)
    assert len(sha) == 40 and all(ch in "0123456789abcdef" for ch in sha)
    recorded = recent_commits(1, start=tmp_path)[0]
    assert recorded.subject == "a runner checkpoint"


def test_commit_preserves_multi_paragraph_message(tmp_path: Path) -> None:
    """Subject + body + trailer paragraphs survive a single ``-m``."""
    _init_repo(tmp_path)
    _commit(tmp_path, "init")
    (tmp_path / "d.txt").write_text("payload")
    add_all(start=tmp_path)

    message = "Checkpoint subject\n\nA body paragraph.\n\nRalph-Checkpoint: 32"
    commit(message, start=tmp_path)

    recorded = recent_commits(1, start=tmp_path)[0]
    assert recorded.subject == "Checkpoint subject"
    assert "A body paragraph." in recorded.body
    assert "Ralph-Checkpoint: 32" in recorded.body


def test_commit_raises_when_nothing_staged(tmp_path: Path) -> None:
    """``git commit`` with an empty index exits non-zero -> GitError."""
    _init_repo(tmp_path)
    _commit(tmp_path, "init")
    with pytest.raises(GitError):
        commit("nothing to do", start=tmp_path)
