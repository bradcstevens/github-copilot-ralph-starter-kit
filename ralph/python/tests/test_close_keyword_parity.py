"""Cross-runner regex parity test.

This is the single most load-bearing test in the suite. The bash runner
at ``ralph/afk.sh:192-196`` and the Python runner share one contract:
the set of issue numbers extracted from a corpus of commit messages MUST
be identical. If this test ever fails, **the failure is the spec** — bash
or Python has drifted and the failing corpus case tells you which.

Mechanics:

* The bash side invokes the exact pipeline ``afk.sh`` uses:

  .. code-block:: bash

     grep -iEo '(close[sd]?|fix(es|ed)?|resolve[sd]?)[[:space:]]+#[0-9]+' \\
       | grep -oE '#[0-9]+' \\
       | tr -d '#' \\
       | sort -un

  ``LC_ALL=C`` is forced on every subprocess so ``[[:space:]]`` and
  ``[0-9]`` behave deterministically across BSD (macOS) and GNU (Linux)
  grep.

* The Python side calls :func:`ralph_afk.wrapper.extract_close_refs`
  directly.

* Both outputs are normalised with ``sorted(set(...))`` because bash
  ends in ``sort -un`` while the Python function preserves first-encounter
  order (the PRD-specified ordering for the Python side). The parity
  property under test is *set equality*, not list ordering.

Out-of-scope (documented divergences):

* Cross-line matching — Python's ``\\s+`` would otherwise span ``\\n``;
  the function processes line-by-line precisely to preserve parity, so
  this test never feeds cross-line corpus to either side.
* Unicode digits — ``grep [0-9]+`` is ASCII; Python ``\\d`` is Unicode.
  Real commit messages never contain Unicode digits in issue references,
  so no corpus case exercises this. A real-world drift would surface
  before the parity test does.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from ralph_afk.wrapper import extract_close_refs

# The bash regex byte-for-byte from ralph/afk.sh:193.
_BASH_KEYWORD_RE = (
    "(close[sd]?|fix(es|ed)?|resolve[sd]?)[[:space:]]+#[0-9]+"
)


def _bash_extract_close_refs(corpus: str) -> list[int]:
    """Invoke the exact ``afk.sh`` pipeline against ``corpus`` and return the
    sorted-unique list of issue numbers.

    Forces ``LC_ALL=C`` on every subprocess to lock down ``[[:space:]]``
    and ``[0-9]`` semantics. Inherits ``PATH`` so grep/tr/sort are
    discoverable from the test environment.

    Asserts subprocess exit codes — ``grep`` may legitimately return 1
    (no matches), but anything else (file errors, regex errors) indicates
    an environment problem the parity test should surface rather than
    silently treat as an empty match set.
    """
    env = {"LC_ALL": "C", "PATH": os.environ.get("PATH", "")}
    p1 = subprocess.run(
        ["grep", "-iEo", _BASH_KEYWORD_RE],
        input=corpus,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert p1.returncode in (0, 1), (
        f"grep -iEo failed unexpectedly (rc={p1.returncode}); "
        f"stderr={p1.stderr!r}"
    )
    if not p1.stdout:
        return []
    p2 = subprocess.run(
        ["grep", "-oE", "#[0-9]+"],
        input=p1.stdout,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert p2.returncode in (0, 1), (
        f"grep -oE failed unexpectedly (rc={p2.returncode}); "
        f"stderr={p2.stderr!r}"
    )
    p3 = subprocess.run(
        ["tr", "-d", "#"],
        input=p2.stdout,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert p3.returncode == 0, (
        f"tr failed unexpectedly (rc={p3.returncode}); stderr={p3.stderr!r}"
    )
    p4 = subprocess.run(
        ["sort", "-un"],
        input=p3.stdout,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert p4.returncode == 0, (
        f"sort -un failed unexpectedly (rc={p4.returncode}); "
        f"stderr={p4.stderr!r}"
    )
    return [int(line) for line in p4.stdout.splitlines() if line.strip()]


def _require_bash_toolchain() -> None:
    """Skip the parity tests cleanly if any required CLI is missing.

    The parity contract is meaningful only when the bash pipeline can
    actually run; absence of ``grep``/``tr``/``sort`` on PATH is an
    environment issue, not a contract regression.
    """
    for cmd in ("grep", "tr", "sort"):
        if shutil.which(cmd) is None:
            pytest.skip(f"{cmd!r} not on PATH — bash parity test cannot run")


# --------------------------------------------------------------------------- #
# Corpus                                                                       #
# --------------------------------------------------------------------------- #
#
# Each entry is (label, corpus). The label appears in pytest output so a
# failure's offending case is obvious. Every entry must produce identical
# set-of-numbers from bash and Python.

PARITY_CORPUS: list[tuple[str, str]] = [
    ("empty", ""),
    ("no_keyword", "Just a normal commit body — no closing keywords."),
    ("single_closes", "Closes #42"),
    (
        "all_nine_keyword_forms",
        "close #1 closes #2 closed #3 "
        "fix #4 fixes #5 fixed #6 "
        "resolve #7 resolves #8 resolved #9",
    ),
    ("case_variation", "Closes #10 CLOSES #11 cLoSeS #12 fIxEs #13 RESOLVED #14"),
    ("multiple_per_line", "Closes #100 and fixes #200"),
    ("tab_separator", "Closes\t#42"),
    ("multi_space_separator", "Closes   #42"),
    (
        "multi_commit_with_boundary",
        "Closes #20\n"
        "---COMMIT-BOUNDARY---\n"
        "Fixes #21\n"
        "---COMMIT-BOUNDARY---\n"
        "Resolves #22",
    ),
    (
        "realistic_commit_body",
        "feat(wrapper): port close-keyword logic\n\n"
        "Implements the deep wrapper module.\n\n"
        "Closes #3.\n",
    ),
    # ----- realistic punctuation variants -----
    ("leading_whitespace", "    Closes #42"),
    ("parenthesised", "(Closes #42)"),
    ("trailing_period", "Closes #42."),
    ("trailing_spaces", "Closes #42   "),
    ("comma_separated_refs", "Closes #1, fixes #2"),
    ("crlf_line_ending", "Closes #42\r\n"),
    ("leading_zeros", "Closes #00042"),
    # ----- negative cases — all of these must produce the empty set. -----
    ("negative_cross_repo_qualified", "Closes org/other-repo#42"),
    ("negative_keyword_glued_to_hash", "Closes#42"),
    ("negative_keyword_in_compound_word", "Close-related #42"),
    ("negative_hash_without_keyword", "Mentions #42 in passing"),
    # ----- dedup ----- bash sorts/uniqs, Python first-encounter dedups;
    # set equality holds either way.
    ("dedup_same_number_twice", "Closes #42 Closes #42"),
    (
        "dedup_across_commits",
        "Closes #42\n---COMMIT-BOUNDARY---\nFixes #42",
    ),
    # ----- ordering — bash sorts numerically (-un), Python preserves
    # first-encounter; both normalise to the same set.
    ("descending_to_test_sort_normalisation", "Closes #9 fixes #1"),
]


@pytest.mark.parametrize(
    "label,corpus",
    PARITY_CORPUS,
    ids=[label for label, _ in PARITY_CORPUS],
)
def test_python_matches_bash_for_corpus(label: str, corpus: str) -> None:
    """Set-of-issue-numbers extracted from ``corpus`` is identical between
    the bash grep pipeline and ``extract_close_refs``.
    """
    _require_bash_toolchain()
    bash_set = set(_bash_extract_close_refs(corpus))
    py_set = set(extract_close_refs(corpus))
    assert py_set == bash_set, (
        f"parity break for {label!r}\n"
        f"  corpus = {corpus!r}\n"
        f"  bash   = {sorted(bash_set)}\n"
        f"  python = {sorted(py_set)}\n"
        f"This failure IS the spec — bash or Python has drifted from the "
        f"cross-runner contract."
    )
