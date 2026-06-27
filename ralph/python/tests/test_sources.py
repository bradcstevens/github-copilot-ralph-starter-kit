"""Tests for :mod:`ralph_afk.sources` — IssueSource Protocol + impls."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from ralph_afk import gh as gh_module
from ralph_afk import sources as sources_module
from ralph_afk.sources import (
    AfkReadyItem,
    Completion,
    GitHubIssueSource,
    IssueSource,
    PrdsIssueSource,
    is_afk_ready,
    is_pr_afk_ready,
)


# --------------------------------------------------------------------------- #
# Shared helpers                                                              #
# --------------------------------------------------------------------------- #


def _silent_logger() -> logging.Logger:
    """A test logger with a NullHandler — silent but still log-API-shaped."""
    logger = logging.getLogger(f"ralph_afk.tests.{id(object())}")
    logger.handlers.clear()
    logger.addHandler(logging.NullHandler())
    logger.propagate = False
    return logger


@dataclass(frozen=True)
class _FakeCommit:
    """Minimal stand-in for :class:`ralph_afk.git.Commit`."""

    sha: str
    subject: str
    body: str = ""
    date: str = "2026-05-16"

    @property
    def message(self) -> str:
        return f"{self.subject}\n{self.body}" if self.body else self.subject


def _make_issue(
    number: int,
    *,
    body: str = "## Parent\n#1\n\n## What to build\nthing\n\n## Acceptance criteria\n- done",
    state: str = "OPEN",
    labels: list[str] | None = None,
    title: str | None = None,
    comments: tuple[gh_module.Comment, ...] = (),
) -> gh_module.Issue:
    return gh_module.Issue(
        number=number,
        title=title or f"Test issue {number}",
        body=body,
        labels=labels if labels is not None else ["ready-for-agent"],
        state=state,
        url=f"https://github.com/x/y/issues/{number}",
        comments=comments,
    )


# --------------------------------------------------------------------------- #
# is_afk_ready                                                                #
# --------------------------------------------------------------------------- #


class TestIsAfkReady:
    def test_returns_true_for_body_with_both_sections(self) -> None:
        body = "Intro\n\n## What to build\nthing\n\n## Acceptance criteria\n- foo"
        assert is_afk_ready(body) is True

    def test_returns_true_when_parent_omitted(self) -> None:
        # ``## Parent`` is OPTIONAL per the to-issues template; a slice that
        # omits it is still AFK-ready as long as it carries the two required
        # sections. Regression guard for parent-less slices being dropped.
        body = "## What to build\nthing\n\n## Acceptance criteria\n- foo"
        assert is_afk_ready(body) is True

    def test_returns_true_when_parent_present(self) -> None:
        # ``## Parent`` is allowed (and usual) — it just isn't required.
        body = "## Parent\n#1\n\n## What to build\nx\n\n## Acceptance criteria\n- foo"
        assert is_afk_ready(body) is True

    def test_returns_false_when_missing_what_to_build(self) -> None:
        body = "## Parent\n#1\n\n## Acceptance criteria\n- foo"
        assert is_afk_ready(body) is False

    def test_returns_false_when_missing_acceptance_criteria(self) -> None:
        body = "## What to build\nthing"
        assert is_afk_ready(body) is False

    def test_returns_false_for_empty_body(self) -> None:
        assert is_afk_ready("") is False

    def test_returns_false_when_sections_are_not_line_anchored(self) -> None:
        body = "blah ## What to build x ## Acceptance criteria done"
        assert is_afk_ready(body) is False

    def test_returns_true_when_sections_anchored_at_start_of_string(self) -> None:
        body = "## What to build\nthing\n## Acceptance criteria\n- bar"
        assert is_afk_ready(body) is True

    def test_returns_true_when_extra_text_follows_section_heading(self) -> None:
        # "## What to build" must be at the start of a line; extra text on
        # the same line after the heading is allowed because the regex isn't
        # end-anchored.
        body = "## What to build  (slice)\n\n## Acceptance criteria\n- bar"
        assert is_afk_ready(body) is True


# --------------------------------------------------------------------------- #
# AfkReadyItem + Completion dataclass shape                                   #
# --------------------------------------------------------------------------- #


class TestDataclassShapes:
    def test_afk_ready_item_carries_int_ref_for_github(self) -> None:
        item = AfkReadyItem(ref=42, title="t", rendered_block="x")
        assert item.ref == 42
        assert isinstance(item.ref, int)

    def test_afk_ready_item_carries_str_ref_for_prds(self) -> None:
        item = AfkReadyItem(
            ref="prds/feat/001-x.md", title="t", rendered_block="x"
        )
        assert item.ref == "prds/feat/001-x.md"
        assert isinstance(item.ref, str)

    def test_afk_ready_item_is_frozen(self) -> None:
        item = AfkReadyItem(ref=1, title="t", rendered_block="x")
        with pytest.raises(Exception):
            item.ref = 99  # type: ignore[misc]

    def test_completion_defaults_shas_to_empty_tuple(self) -> None:
        c = Completion(ref=1, sha="deadbeef")
        assert c.shas == ()

    def test_completion_is_frozen(self) -> None:
        c = Completion(ref=1, sha="deadbeef")
        with pytest.raises(Exception):
            c.sha = "nope"  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# IssueSource Protocol structural conformance                                 #
# --------------------------------------------------------------------------- #


class TestProtocolConformance:
    def test_github_source_satisfies_protocol_isinstance(self) -> None:
        impl = GitHubIssueSource(_silent_logger())
        assert isinstance(impl, IssueSource)

    def test_prds_source_satisfies_protocol_isinstance(self, tmp_path: Path) -> None:
        impl = PrdsIssueSource(tmp_path, _silent_logger())
        assert isinstance(impl, IssueSource)

    def test_runtime_checkable_rejects_arbitrary_object(self) -> None:
        class NotASource:
            pass

        assert not isinstance(NotASource(), IssueSource)


# --------------------------------------------------------------------------- #
# GitHubIssueSource.preflight                                                 #
# --------------------------------------------------------------------------- #


class TestGitHubPreflight:
    def test_returns_none_when_gh_ok(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(gh_module, "auth_status", lambda: True)
        monkeypatch.setattr(
            gh_module,
            "repo_view",
            lambda: gh_module.Repo(owner="x", name="y", default_branch="main"),
        )
        impl = GitHubIssueSource(_silent_logger())
        assert impl.preflight() is None

    def test_returns_one_when_gh_not_authed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(gh_module, "auth_status", lambda: False)
        impl = GitHubIssueSource(_silent_logger())
        assert impl.preflight() == 1

    def test_returns_one_when_auth_status_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _raise() -> bool:
            raise gh_module.GhError(["gh", "auth", "status"], 127, "missing")

        monkeypatch.setattr(gh_module, "auth_status", _raise)
        impl = GitHubIssueSource(_silent_logger())
        assert impl.preflight() == 1

    def test_returns_one_when_repo_view_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(gh_module, "auth_status", lambda: True)

        def _raise() -> gh_module.Repo:
            raise gh_module.GhError(["gh", "repo", "view"], 1, "not a repo")

        monkeypatch.setattr(gh_module, "repo_view", _raise)
        impl = GitHubIssueSource(_silent_logger())
        assert impl.preflight() == 1


# --------------------------------------------------------------------------- #
# GitHubIssueSource.collect_afk_ready                                          #
# --------------------------------------------------------------------------- #


class TestGitHubCollectAfkReady:
    def test_returns_empty_when_list_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _raise(*_a: Any, **_kw: Any) -> list[gh_module.Issue]:
            raise gh_module.GhError(["gh", "issue", "list"], 1, "boom")

        monkeypatch.setattr(gh_module, "issue_list", _raise)
        impl = GitHubIssueSource(_silent_logger())
        assert impl.collect_afk_ready() == []

    def test_filters_out_issues_lacking_discriminator(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        good = _make_issue(42)
        bad = _make_issue(43, body="just words, no sections")
        view_calls: list[int] = []

        def fake_view(num: int) -> gh_module.Issue:
            view_calls.append(num)
            return good if num == 42 else bad

        monkeypatch.setattr(
            gh_module, "issue_list", lambda label, state="open": [good, bad]
        )
        monkeypatch.setattr(gh_module, "issue_view", fake_view)

        impl = GitHubIssueSource(_silent_logger())
        items = impl.collect_afk_ready()

        assert [i.ref for i in items] == [42]
        # Discriminator filter runs BEFORE the per-issue view to save
        # the N+1 round-trip on non-AFK-ready candidates.
        assert view_calls == [42], (
            f"expected only #42 to be view-fetched; got {view_calls}"
        )

    def test_renders_block_with_header_body_and_no_comments(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        issue = _make_issue(
            42,
            title="Do the thing",
            labels=["ready-for-agent", "bug"],
            body="## Parent\n#1\n\n## What to build\nthing\n\n## Acceptance criteria\n- ok",
        )
        monkeypatch.setattr(
            gh_module, "issue_list", lambda label, state="open": [issue]
        )
        monkeypatch.setattr(gh_module, "issue_view", lambda n: issue)

        impl = GitHubIssueSource(_silent_logger())
        items = impl.collect_afk_ready()
        assert len(items) == 1
        block = items[0].rendered_block
        assert block.startswith(
            "=== Issue #42: Do the thing [labels: ready-for-agent, bug] ==="
        )
        assert "## What to build" in block
        assert "## Acceptance criteria" in block

    def test_renders_block_with_recent_comments_newest_first(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        comments = (
            gh_module.Comment(
                author="alice", body="old comment", created_at="2026-05-10T00:00:00Z"
            ),
            gh_module.Comment(
                author="bob", body="newer comment", created_at="2026-05-15T00:00:00Z"
            ),
        )
        issue = _make_issue(42, comments=comments)
        monkeypatch.setattr(
            gh_module, "issue_list", lambda label, state="open": [issue]
        )
        monkeypatch.setattr(gh_module, "issue_view", lambda n: issue)

        impl = GitHubIssueSource(_silent_logger())
        items = impl.collect_afk_ready()
        block = items[0].rendered_block

        # Newest comment should appear first in the block (after the
        # "--- Recent comments" separator).
        assert "--- Recent comments (newest first, up to 5) ---" in block
        comments_section = block.split(
            "--- Recent comments (newest first, up to 5) ---"
        )[1]
        bob_pos = comments_section.index("@bob")
        alice_pos = comments_section.index("@alice")
        assert bob_pos < alice_pos, (
            "newest comment (bob) should appear before older (alice)"
        )

    def test_skips_issue_view_failure_continues_others(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ok = _make_issue(42)
        broken = _make_issue(99)

        def fake_view(num: int) -> gh_module.Issue:
            if num == 99:
                raise gh_module.GhError(["gh"], 1, "broken")
            return ok

        monkeypatch.setattr(
            gh_module, "issue_list", lambda label, state="open": [ok, broken]
        )
        monkeypatch.setattr(gh_module, "issue_view", fake_view)

        impl = GitHubIssueSource(_silent_logger())
        items = impl.collect_afk_ready()
        assert [i.ref for i in items] == [42]

    def test_re_verifies_discriminator_on_full_body(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If issue_view returns a different body lacking the discriminator, drop it."""
        stub_list_body = _make_issue(42)
        stub_full = _make_issue(42, body="No discriminator anymore")

        monkeypatch.setattr(
            gh_module, "issue_list", lambda label, state="open": [stub_list_body]
        )
        monkeypatch.setattr(gh_module, "issue_view", lambda n: stub_full)

        impl = GitHubIssueSource(_silent_logger())
        assert impl.collect_afk_ready() == []


# --------------------------------------------------------------------------- #
# GitHubIssueSource.handle_completions                                        #
# --------------------------------------------------------------------------- #


class TestGitHubHandleCompletions:
    def test_returns_empty_when_no_new_commits(self) -> None:
        impl = GitHubIssueSource(_silent_logger())
        completions = impl.handle_completions(
            pool=[AfkReadyItem(ref=42, title="t", rendered_block="x")],
            new_commits=[],
        )
        assert completions == []

    def test_returns_empty_when_pool_is_empty(self) -> None:
        impl = GitHubIssueSource(_silent_logger())
        completions = impl.handle_completions(
            pool=[],
            new_commits=[_FakeCommit("sha1", "Closes #42")],
        )
        assert completions == []

    def test_closes_issue_when_commit_references_pool_member(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        close_calls: list[tuple[int, str]] = []

        def fake_close(num: int, body: str) -> None:
            close_calls.append((num, body))

        monkeypatch.setattr(gh_module, "issue_view", lambda n: _make_issue(n))
        monkeypatch.setattr(gh_module, "issue_close", fake_close)

        impl = GitHubIssueSource(_silent_logger())
        completions = impl.handle_completions(
            pool=[AfkReadyItem(ref=42, title="t", rendered_block="x")],
            new_commits=[
                _FakeCommit("sha_abc", "feat: impl", body="Closes #42")
            ],
        )

        assert len(completions) == 1
        assert completions[0].ref == 42
        assert completions[0].sha == "sha_abc"
        assert completions[0].shas == ("sha_abc",)
        assert len(close_calls) == 1
        assert close_calls[0][0] == 42

    def test_skips_close_when_ref_not_in_pool(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        close_calls: list[tuple[int, str]] = []

        def fake_close(num: int, body: str) -> None:
            close_calls.append((num, body))

        monkeypatch.setattr(gh_module, "issue_view", lambda n: _make_issue(n))
        monkeypatch.setattr(gh_module, "issue_close", fake_close)

        impl = GitHubIssueSource(_silent_logger())
        completions = impl.handle_completions(
            pool=[AfkReadyItem(ref=42, title="t", rendered_block="x")],
            new_commits=[_FakeCommit("sha", "Closes #99")],
        )
        assert completions == []
        assert close_calls == []

    def test_skips_close_when_issue_already_closed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            gh_module,
            "issue_view",
            lambda n: _make_issue(n, state="CLOSED"),
        )
        close_calls: list[Any] = []
        monkeypatch.setattr(
            gh_module,
            "issue_close",
            lambda *a, **kw: close_calls.append((a, kw)),
        )

        impl = GitHubIssueSource(_silent_logger())
        completions = impl.handle_completions(
            pool=[AfkReadyItem(ref=42, title="t", rendered_block="x")],
            new_commits=[_FakeCommit("sha", "Closes #42")],
        )
        assert completions == []
        assert close_calls == []

    def test_close_failure_is_non_fatal_to_other_completions(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        close_calls: list[int] = []

        def fake_close(num: int, body: str) -> None:
            close_calls.append(num)
            if num == 42:
                raise gh_module.GhError(["gh"], 1, "boom")

        monkeypatch.setattr(gh_module, "issue_view", lambda n: _make_issue(n))
        monkeypatch.setattr(gh_module, "issue_close", fake_close)

        impl = GitHubIssueSource(_silent_logger())
        completions = impl.handle_completions(
            pool=[
                AfkReadyItem(ref=42, title="t", rendered_block="x"),
                AfkReadyItem(ref=43, title="t", rendered_block="x"),
            ],
            new_commits=[
                _FakeCommit("sha1", "Closes #42"),
                _FakeCommit("sha2", "Fixes #43"),
            ],
        )
        # #42 close raised → no completion; #43 still proceeds.
        assert [c.ref for c in completions] == [43]
        assert close_calls == [42, 43]

    def test_attributes_multiple_shas_when_multiple_commits_reference_issue(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        close_calls: list[str] = []

        def fake_close(num: int, body: str) -> None:
            close_calls.append(body)

        monkeypatch.setattr(gh_module, "issue_view", lambda n: _make_issue(n))
        monkeypatch.setattr(gh_module, "issue_close", fake_close)

        impl = GitHubIssueSource(_silent_logger())
        completions = impl.handle_completions(
            pool=[AfkReadyItem(ref=42, title="t", rendered_block="x")],
            new_commits=[
                _FakeCommit("sha_a", "first half", body="Refs #42"),
                _FakeCommit("sha_b", "complete", body="Closes #42"),
                _FakeCommit("sha_c", "follow-up", body="Fixes #42"),
            ],
        )
        # Only sha_b and sha_c contain CLOSING keywords (Refs isn't one);
        # extract_close_refs returns 42 (deduped). Both closing commits
        # should be attributed.
        assert len(completions) == 1
        assert completions[0].ref == 42
        assert set(completions[0].shas) == {"sha_b", "sha_c"}


# --------------------------------------------------------------------------- #
# PrdsIssueSource.preflight                                                   #
# --------------------------------------------------------------------------- #


class TestPrdsPreflight:
    def test_returns_none_even_when_prds_dir_missing(
        self, tmp_path: Path
    ) -> None:
        impl = PrdsIssueSource(tmp_path, _silent_logger())
        assert impl.preflight() is None

    def test_returns_none_when_prds_dir_exists(self, tmp_path: Path) -> None:
        (tmp_path / "prds").mkdir()
        impl = PrdsIssueSource(tmp_path, _silent_logger())
        assert impl.preflight() is None


# --------------------------------------------------------------------------- #
# PrdsIssueSource.collect_afk_ready                                           #
# --------------------------------------------------------------------------- #


_AFK_BODY = "## Parent\n#1\n\n## What to build\nthing\n\n## Acceptance criteria\n- a"
_NON_AFK_BODY = "Just a regular body without sections."


def _write_md(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


class TestPrdsCollectAfkReady:
    def test_returns_empty_when_no_prds_dir(self, tmp_path: Path) -> None:
        impl = PrdsIssueSource(tmp_path, _silent_logger())
        assert impl.collect_afk_ready() == []

    def test_returns_empty_when_prds_dir_empty(self, tmp_path: Path) -> None:
        (tmp_path / "prds").mkdir()
        impl = PrdsIssueSource(tmp_path, _silent_logger())
        assert impl.collect_afk_ready() == []

    def test_discovers_single_nnn_file_with_discriminator(
        self, tmp_path: Path
    ) -> None:
        _write_md(tmp_path / "prds" / "featA" / "001-foo.md", _AFK_BODY)
        impl = PrdsIssueSource(tmp_path, _silent_logger())
        items = impl.collect_afk_ready()
        assert len(items) == 1
        assert items[0].ref == "prds/featA/001-foo.md"
        assert items[0].title == "prds/featA/001-foo.md"
        assert items[0].rendered_block.startswith("=== prds/featA/001-foo.md ===\n")
        assert "## Parent" in items[0].rendered_block

    def test_skips_prd_md(self, tmp_path: Path) -> None:
        _write_md(tmp_path / "prds" / "featA" / "prd.md", _AFK_BODY)
        impl = PrdsIssueSource(tmp_path, _silent_logger())
        assert impl.collect_afk_ready() == []

    def test_skips_files_without_nnn_prefix(self, tmp_path: Path) -> None:
        _write_md(tmp_path / "prds" / "featA" / "notes.md", _AFK_BODY)
        _write_md(tmp_path / "prds" / "featA" / "001-real.md", _AFK_BODY)
        impl = PrdsIssueSource(tmp_path, _silent_logger())
        items = impl.collect_afk_ready()
        assert [i.ref for i in items] == ["prds/featA/001-real.md"]

    def test_skips_files_lacking_afk_discriminator(self, tmp_path: Path) -> None:
        _write_md(tmp_path / "prds" / "featA" / "001-incomplete.md", _NON_AFK_BODY)
        _write_md(tmp_path / "prds" / "featA" / "002-ready.md", _AFK_BODY)
        impl = PrdsIssueSource(tmp_path, _silent_logger())
        items = impl.collect_afk_ready()
        assert [i.ref for i in items] == ["prds/featA/002-ready.md"]

    def test_skips_done_subdirectory_files(self, tmp_path: Path) -> None:
        _write_md(tmp_path / "prds" / "featA" / "done" / "001-archived.md", _AFK_BODY)
        _write_md(tmp_path / "prds" / "featA" / "002-active.md", _AFK_BODY)
        impl = PrdsIssueSource(tmp_path, _silent_logger())
        items = impl.collect_afk_ready()
        assert [i.ref for i in items] == ["prds/featA/002-active.md"]

    def test_orders_within_feature_numerically_by_nnn(
        self, tmp_path: Path
    ) -> None:
        # Zero-padded NNN sorts lex-equivalent-to-numerical, matching
        # POSIX `find ... | sort` ordering.
        _write_md(tmp_path / "prds" / "featA" / "003-c.md", _AFK_BODY)
        _write_md(tmp_path / "prds" / "featA" / "001-a.md", _AFK_BODY)
        _write_md(tmp_path / "prds" / "featA" / "002-b.md", _AFK_BODY)
        impl = PrdsIssueSource(tmp_path, _silent_logger())
        items = impl.collect_afk_ready()
        assert [i.ref for i in items] == [
            "prds/featA/001-a.md",
            "prds/featA/002-b.md",
            "prds/featA/003-c.md",
        ]

    def test_orders_across_features_lexicographically(
        self, tmp_path: Path
    ) -> None:
        _write_md(tmp_path / "prds" / "featB" / "001-b.md", _AFK_BODY)
        _write_md(tmp_path / "prds" / "featA" / "001-a.md", _AFK_BODY)
        impl = PrdsIssueSource(tmp_path, _silent_logger())
        items = impl.collect_afk_ready()
        assert [i.ref for i in items] == [
            "prds/featA/001-a.md",
            "prds/featB/001-b.md",
        ]

    def test_multi_feature_multi_file_full_ordering(
        self, tmp_path: Path
    ) -> None:
        # 5 files across 2 features; verify full deterministic order.
        _write_md(tmp_path / "prds" / "featA" / "001-a.md", _AFK_BODY)
        _write_md(tmp_path / "prds" / "featA" / "010-aa.md", _AFK_BODY)
        _write_md(tmp_path / "prds" / "featA" / "002-b.md", _AFK_BODY)
        _write_md(tmp_path / "prds" / "featB" / "005-b5.md", _AFK_BODY)
        _write_md(tmp_path / "prds" / "featB" / "001-b1.md", _AFK_BODY)
        impl = PrdsIssueSource(tmp_path, _silent_logger())
        items = impl.collect_afk_ready()
        assert [i.ref for i in items] == [
            "prds/featA/001-a.md",
            "prds/featA/002-b.md",
            "prds/featA/010-aa.md",
            "prds/featB/001-b1.md",
            "prds/featB/005-b5.md",
        ]

    def test_skips_top_level_done_directory(self, tmp_path: Path) -> None:
        # Defensive: a top-level `prds/done/` shouldn't be a feature dir
        # but if it exists we don't iterate it.
        _write_md(tmp_path / "prds" / "done" / "001-archived.md", _AFK_BODY)
        _write_md(tmp_path / "prds" / "featA" / "001-active.md", _AFK_BODY)
        impl = PrdsIssueSource(tmp_path, _silent_logger())
        items = impl.collect_afk_ready()
        assert [i.ref for i in items] == ["prds/featA/001-active.md"]

    def test_skips_loose_md_files_at_top_level(self, tmp_path: Path) -> None:
        # `prds/README.md` isn't inside a feature dir; should be ignored.
        _write_md(tmp_path / "prds" / "README.md", _AFK_BODY)
        _write_md(tmp_path / "prds" / "featA" / "001-a.md", _AFK_BODY)
        impl = PrdsIssueSource(tmp_path, _silent_logger())
        items = impl.collect_afk_ready()
        assert [i.ref for i in items] == ["prds/featA/001-a.md"]

    def test_rendered_block_format_matches_bash_collector(
        self, tmp_path: Path
    ) -> None:
        # The block is "=== <path> ===\n<file contents>".
        body = "## Parent\n#1\n\n## What to build\nthing\n\n## Acceptance criteria\n- ok\n"
        _write_md(tmp_path / "prds" / "featA" / "001-a.md", body)
        impl = PrdsIssueSource(tmp_path, _silent_logger())
        items = impl.collect_afk_ready()
        assert items[0].rendered_block == f"=== prds/featA/001-a.md ===\n{body}"

    def test_unreadable_file_is_skipped_not_raised(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Simulate a read failure on one file but success on another.
        _write_md(tmp_path / "prds" / "featA" / "001-good.md", _AFK_BODY)
        _write_md(tmp_path / "prds" / "featA" / "002-bad.md", _AFK_BODY)

        real_read = Path.read_text

        def fake_read(self: Path, *args: Any, **kwargs: Any) -> str:
            if self.name == "002-bad.md":
                raise OSError("simulated read failure")
            return real_read(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", fake_read)
        impl = PrdsIssueSource(tmp_path, _silent_logger())
        items = impl.collect_afk_ready()
        assert [i.ref for i in items] == ["prds/featA/001-good.md"]


# --------------------------------------------------------------------------- #
# PrdsIssueSource.handle_completions                                          #
# --------------------------------------------------------------------------- #


class TestPrdsHandleCompletions:
    def test_returns_empty_with_no_pool(self, tmp_path: Path) -> None:
        impl = PrdsIssueSource(tmp_path, _silent_logger())
        assert impl.handle_completions(pool=[], new_commits=[]) == []

    def test_returns_empty_even_when_commit_references_file(
        self, tmp_path: Path
    ) -> None:
        """The wrapper does NOT auto-move files.

        Even if a new commit's message literally contains the pool file
        path, ``handle_completions`` returns an empty list. The agent
        owns the ``git mv`` step; the wrapper only discovers the
        resulting state on the next iteration.
        """
        _write_md(tmp_path / "prds" / "featA" / "001-a.md", _AFK_BODY)
        pool = [
            AfkReadyItem(
                ref="prds/featA/001-a.md", title="x", rendered_block="x"
            )
        ]
        commits = [_FakeCommit("sha", "git mv prds/featA/001-a.md prds/featA/done/")]
        impl = PrdsIssueSource(tmp_path, _silent_logger())
        assert impl.handle_completions(pool=pool, new_commits=commits) == []

    def test_does_not_mutate_filesystem(self, tmp_path: Path) -> None:
        """Critical invariant: handle_completions must leave the worktree clean.

        A wrapper-side move would dirty the tree; under ADR-0004 the runner
        Checkpoint would now capture that rather than abort, but detection-only
        keeps the PRDs closure attributable to the agent's own ``git mv``
        commit instead of an anonymous Checkpoint.
        """
        original = tmp_path / "prds" / "featA" / "001-a.md"
        _write_md(original, _AFK_BODY)
        before_files = {
            p for p in (tmp_path / "prds").rglob("*") if p.is_file()
        }
        pool = [
            AfkReadyItem(
                ref="prds/featA/001-a.md", title="x", rendered_block="x"
            )
        ]
        commits = [_FakeCommit("sha", "Closes prds/featA/001-a.md")]

        impl = PrdsIssueSource(tmp_path, _silent_logger())
        impl.handle_completions(pool=pool, new_commits=commits)

        after_files = {
            p for p in (tmp_path / "prds").rglob("*") if p.is_file()
        }
        assert before_files == after_files, (
            "PrdsIssueSource.handle_completions must NOT mutate the filesystem"
        )
        assert original.exists(), "the pool file must still exist after handle_completions"


# --------------------------------------------------------------------------- #
# Module-level structure / imports                                            #
# --------------------------------------------------------------------------- #


class TestModuleStructure:
    def test_exports_documented_public_surface(self) -> None:
        expected = {
            "AfkReadyItem",
            "Completion",
            "IssueSource",
            "GitHubIssueSource",
            "PrdsIssueSource",
            "is_afk_ready",
            "is_pr_afk_ready",
        }
        assert set(sources_module.__all__) == expected
        for name in expected:
            assert hasattr(sources_module, name)

    def test_protocol_is_runtime_checkable(self) -> None:
        # The Protocol must remain @runtime_checkable so the loop and
        # tests can confirm structural conformance via isinstance.
        from typing import _ProtocolMeta  # type: ignore[attr-defined]

        assert isinstance(IssueSource, _ProtocolMeta)

    def test_imports_are_constrained(self) -> None:
        """sources.py may only import stdlib + ralph_afk.{gh,git,wrapper}.

        Forbidden: copilot SDK, rich, ralph_afk.{loop,cli,config,session,
        ui,persist,events,pricing,telemetry} — keeps the Protocol seam
        light and the unit-test surface fast.
        """
        import ast

        source_path = Path(sources_module.__file__)
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        allowed_third_party_prefixes: set[str] = set()  # no third-party allowed
        allowed_ralph_afk_submodules = {"gh", "git", "wrapper"}

        offenders: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top = alias.name.split(".")[0]
                    if top == "ralph_afk":
                        sub = alias.name.split(".", 2)[1] if "." in alias.name else None
                        if sub and sub not in allowed_ralph_afk_submodules:
                            offenders.append(
                                f"line {node.lineno}: import {alias.name}"
                            )
                    elif top in {"copilot", "rich"}:
                        offenders.append(f"line {node.lineno}: import {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                if node.level > 0:
                    continue
                if node.module is None:
                    continue
                top = node.module.split(".")[0]
                if top == "ralph_afk":
                    parts = node.module.split(".")
                    if len(parts) >= 2 and parts[1] not in allowed_ralph_afk_submodules:
                        offenders.append(
                            f"line {node.lineno}: from {node.module} import ..."
                        )
                elif top in {"copilot", "rich"}:
                    offenders.append(
                        f"line {node.lineno}: from {node.module} import ..."
                    )
                elif top not in allowed_third_party_prefixes:
                    # stdlib-only is allowed via the default branch
                    # but check for known third-party leaks.
                    if top in {"httpx", "requests", "github", "pygit2"}:
                        offenders.append(
                            f"line {node.lineno}: from {node.module} import ..."
                        )

        assert not offenders, (
            "ralph_afk/sources.py has forbidden imports:\n  "
            + "\n  ".join(offenders)
        )


# Silence pytest's unused-import warning if MagicMock ends up unused.
_ = MagicMock


# --------------------------------------------------------------------------- #
# PR support helpers                                                          #
# --------------------------------------------------------------------------- #


def _make_pr(
    number: int,
    *,
    body: str = "",
    state: str = "OPEN",
    head_sha: str = "a" * 40,
    head_branch: str = "feature/x",
    labels: list[str] | None = None,
    comments: tuple[gh_module.Comment, ...] = (),
) -> gh_module.PullRequest:
    return gh_module.PullRequest(
        number=number,
        title=f"Test PR {number}",
        body=body,
        labels=labels if labels is not None else ["ready-for-agent"],
        state=state,
        url=f"https://github.com/x/y/pull/{number}",
        head_sha=head_sha,
        head_branch=head_branch,
        comments=comments,
    )


def _brief_comment(body: str = "## Agent Brief\nDo the thing.") -> gh_module.Comment:
    return gh_module.Comment(author="triage-bot", body=body, created_at="2026-05-16")


# --------------------------------------------------------------------------- #
# is_pr_afk_ready                                                             #
# --------------------------------------------------------------------------- #


class TestIsPrAfkReady:
    def test_true_when_brief_in_body(self) -> None:
        assert is_pr_afk_ready(_make_pr(7, body="## Agent Brief\nDo X")) is True

    def test_true_when_brief_in_comment(self) -> None:
        pr = _make_pr(7, body="normal description", comments=(_brief_comment(),))
        assert is_pr_afk_ready(pr) is True

    def test_false_when_no_brief_anywhere(self) -> None:
        pr = _make_pr(
            7,
            body="normal",
            comments=(gh_module.Comment("u", "lgtm", "2026-05-16"),),
        )
        assert is_pr_afk_ready(pr) is False

    def test_false_when_brief_not_line_anchored(self) -> None:
        assert is_pr_afk_ready(_make_pr(7, body="see ## Agent Brief inline")) is False

    def test_false_for_empty_pr(self) -> None:
        assert is_pr_afk_ready(_make_pr(7, body="")) is False


# --------------------------------------------------------------------------- #
# PR-aware AfkReadyItem / Completion shape                                    #
# --------------------------------------------------------------------------- #


class TestPrDataclassShapes:
    def test_afk_ready_item_pr_kind_and_head_sha(self) -> None:
        item = AfkReadyItem(
            ref=7, title="t", rendered_block="x", kind="pr", head_sha="abc"
        )
        assert item.kind == "pr"
        assert item.head_sha == "abc"

    def test_afk_ready_item_defaults_to_issue_kind(self) -> None:
        item = AfkReadyItem(ref=1, title="t", rendered_block="x")
        assert item.kind == "issue"
        assert item.head_sha == ""

    def test_completion_defaults_to_issue_kind(self) -> None:
        assert Completion(ref=1, sha="x").kind == "issue"

    def test_completion_pr_kind(self) -> None:
        assert Completion(ref=7, sha="newsha", kind="pr").kind == "pr"


# --------------------------------------------------------------------------- #
# GitHubIssueSource PR collection                                            #
# --------------------------------------------------------------------------- #


class TestGitHubCollectAfkReadyPrs:
    def test_does_not_list_prs_when_include_prs_false(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        called = {"pr_list": 0}
        monkeypatch.setattr(gh_module, "issue_list", lambda label, state="open": [])

        def _pr_list(*_a: Any, **_kw: Any) -> list[gh_module.PullRequest]:
            called["pr_list"] += 1
            return []

        monkeypatch.setattr(gh_module, "pr_list", _pr_list)
        impl = GitHubIssueSource(_silent_logger())  # include_prs defaults False
        assert impl.collect_afk_ready() == []
        assert called["pr_list"] == 0

    def test_collects_only_prs_with_brief_when_enabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(gh_module, "issue_list", lambda label, state="open": [])
        monkeypatch.setattr(
            gh_module,
            "pr_list",
            lambda label, state="open": [_make_pr(7), _make_pr(8)],
        )

        def fake_pr_view(n: int) -> gh_module.PullRequest:
            if n == 7:
                return _make_pr(7, comments=(_brief_comment(),), head_sha="oldsha")
            return _make_pr(8, body="no brief here")  # filtered out

        monkeypatch.setattr(gh_module, "pr_view", fake_pr_view)
        impl = GitHubIssueSource(_silent_logger(), include_prs=True)
        items = impl.collect_afk_ready()

        assert [i.ref for i in items] == [7]
        assert items[0].kind == "pr"
        assert items[0].head_sha == "oldsha"
        assert items[0].rendered_block.startswith("=== PR #7:")
        assert "(branch: feature/x)" in items[0].rendered_block

    def test_pr_list_failure_is_non_fatal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(gh_module, "issue_list", lambda label, state="open": [])

        def _raise(*_a: Any, **_kw: Any) -> list[gh_module.PullRequest]:
            raise gh_module.GhError(["gh", "pr", "list"], 1, "boom")

        monkeypatch.setattr(gh_module, "pr_list", _raise)
        impl = GitHubIssueSource(_silent_logger(), include_prs=True)
        assert impl.collect_afk_ready() == []


# --------------------------------------------------------------------------- #
# GitHubIssueSource PR-advance detection + mixed-pool backstop               #
# --------------------------------------------------------------------------- #


def _pool_pr(number: int = 7, head_sha: str = "oldsha") -> AfkReadyItem:
    return AfkReadyItem(
        ref=number, title="t", rendered_block="x", kind="pr", head_sha=head_sha
    )


class TestGitHubDetectPrAdvances:
    def test_records_advance_when_head_sha_changed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            gh_module, "pr_view", lambda n: _make_pr(n, head_sha="newsha")
        )
        impl = GitHubIssueSource(_silent_logger(), include_prs=True)
        completions = impl.handle_completions(pool=[_pool_pr()], new_commits=[])
        assert len(completions) == 1
        assert completions[0].ref == 7
        assert completions[0].kind == "pr"
        assert completions[0].sha == "newsha"

    def test_no_advance_when_head_sha_unchanged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            gh_module, "pr_view", lambda n: _make_pr(n, head_sha="oldsha")
        )
        impl = GitHubIssueSource(_silent_logger(), include_prs=True)
        assert (
            impl.handle_completions(pool=[_pool_pr(head_sha="oldsha")], new_commits=[])
            == []
        )

    def test_no_advance_when_pr_merged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            gh_module,
            "pr_view",
            lambda n: _make_pr(n, head_sha="newsha", state="MERGED"),
        )
        impl = GitHubIssueSource(_silent_logger(), include_prs=True)
        assert impl.handle_completions(pool=[_pool_pr()], new_commits=[]) == []

    def test_detection_skipped_when_include_prs_false(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        called = {"pr_view": 0}

        def _pr_view(n: int) -> gh_module.PullRequest:
            called["pr_view"] += 1
            return _make_pr(n, head_sha="newsha")

        monkeypatch.setattr(gh_module, "pr_view", _pr_view)
        impl = GitHubIssueSource(_silent_logger())  # include_prs False
        assert impl.handle_completions(pool=[_pool_pr()], new_commits=[]) == []
        assert called["pr_view"] == 0

    def test_pr_view_failure_during_detect_is_non_fatal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _raise(n: int) -> gh_module.PullRequest:
            raise gh_module.GhError(["gh", "pr", "view", str(n)], 1, "boom")

        monkeypatch.setattr(gh_module, "pr_view", _raise)
        impl = GitHubIssueSource(_silent_logger(), include_prs=True)
        assert impl.handle_completions(pool=[_pool_pr()], new_commits=[]) == []


class TestGitHubMixedPoolBackstop:
    def test_closes_keyword_never_closes_pr_sharing_number(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A ``Closes #7`` must not ``gh issue close`` #7 when #7 is a PR."""
        close_calls: list[int] = []
        monkeypatch.setattr(gh_module, "issue_view", lambda n: _make_issue(n))
        monkeypatch.setattr(
            gh_module, "issue_close", lambda n, c: close_calls.append(n)
        )
        # PR-advance check: head unchanged → no PR completion either.
        monkeypatch.setattr(
            gh_module, "pr_view", lambda n: _make_pr(n, head_sha="oldsha")
        )
        impl = GitHubIssueSource(_silent_logger(), include_prs=True)
        completions = impl.handle_completions(
            pool=[_pool_pr(number=7, head_sha="oldsha")],
            new_commits=[_FakeCommit("sha", "Closes #7")],
        )
        assert close_calls == []
        assert completions == []

    def test_issue_closure_still_works_with_pr_in_pool(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        close_calls: list[int] = []
        monkeypatch.setattr(gh_module, "issue_view", lambda n: _make_issue(n))
        monkeypatch.setattr(
            gh_module, "issue_close", lambda n, c: close_calls.append(n)
        )
        monkeypatch.setattr(
            gh_module, "pr_view", lambda n: _make_pr(n, head_sha="oldsha")
        )
        impl = GitHubIssueSource(_silent_logger(), include_prs=True)
        completions = impl.handle_completions(
            pool=[
                _pool_pr(number=7, head_sha="oldsha"),
                AfkReadyItem(ref=42, title="t", rendered_block="x"),  # issue
            ],
            new_commits=[_FakeCommit("sha", "Closes #42")],
        )
        assert close_calls == [42]
        assert [c.ref for c in completions] == [42]
