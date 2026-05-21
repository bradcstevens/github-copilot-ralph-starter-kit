"""Tests for the CLI's ``reasoning_effort`` resolution.

The Copilot Python SDK only sends a ``reasoningEffort`` payload field
when ``client.create_session(reasoning_effort=...)`` receives a non-
falsy value. Some Copilot model variants pin the supported effort to a
single value and reject the backend's service-side default with a
CAPI 400 — e.g. ``claude-opus-4.7-xhigh`` only accepts ``"xhigh"``.

The CLI therefore auto-derives a safe default from the resolved model
id's suffix, with the ``REASONING_EFFORT`` env var as an explicit
override. These tests pin both behaviours:

* the suffix-based auto-derivation (load-bearing for the kit's default
  model);
* the env-var override (so operators can experiment without editing
  the runner);
* the env-var validation (an invalid override is a hard ``SystemExit``,
  not a mid-iteration crash).

The CLI's ``_build_config`` is exercised end-to-end via :func:`main`
with monkeypatched env + a faked loop runner, so the test covers the
real env-var precedence and not just an isolated helper.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ralph_afk import cli as cli_module
from ralph_afk.cli import _derive_reasoning_effort_from_model
from ralph_afk.config import RunConfig


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear every env var the CLI consults so tests start from a clean slate."""
    for name in (
        "MODEL",
        "REASONING_EFFORT",
        "ISSUE_SOURCE",
        "MAX_NMT_STRIKES",
        "RALPH_DENY_TOOLS",
        "RALPH_DENY_SKILLS",
        "RALPH_PRICING_FILE",
        "RALPH_OTEL_ENABLED",
        "OTEL_EXPORTER_OTLP_ENDPOINT",
    ):
        monkeypatch.delenv(name, raising=False)


# ---------------------------------------------------------------------------
# Pure helper — _derive_reasoning_effort_from_model
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "model,expected",
    [
        ("claude-opus-4.7-xhigh", "xhigh"),
        ("claude-opus-4.7-high", "high"),
        ("claude-opus-4.7-medium", "medium"),
        ("claude-opus-4.7-low", "low"),
        ("claude-sonnet-4.6", None),
        ("gpt-5.4", None),
        ("gpt-5-mini", None),
        ("", None),
        (None, None),
    ],
)
def test_derive_reasoning_effort_from_model(
    model: str | None, expected: str | None
) -> None:
    """The helper matches the trailing ``-<effort>`` segment exactly."""
    assert _derive_reasoning_effort_from_model(model) == expected


# ---------------------------------------------------------------------------
# End-to-end — main() composes a RunConfig with the right reasoning_effort
# ---------------------------------------------------------------------------


def _install_fake_runner(
    monkeypatch: pytest.MonkeyPatch,
    captured: list[RunConfig],
    tmp_path: Path,
) -> None:
    """Replace ``cli.resolve_repo_root`` + ``loop.run`` so ``main`` doesn't actually run.

    We want the env-var and CLI parse path to run for real (so we test
    the precedence the operator will actually hit) but stop short of
    creating an SDK client. Capturing the composed :class:`RunConfig`
    is enough for the assertions below.

    Also writes a stub ``docs/agents/issue-tracker.md`` under ``tmp_path``
    so the CLI's agent-skills preflight passes — these tests pin the
    reasoning-effort precedence contract, not the agent-skills
    configuration preflight (which has dedicated coverage in
    ``tests/test_smoke.py``).
    """
    monkeypatch.setattr(cli_module, "resolve_repo_root", lambda: tmp_path)

    issue_tracker_doc = tmp_path / "docs" / "agents" / "issue-tracker.md"
    issue_tracker_doc.parent.mkdir(parents=True, exist_ok=True)
    issue_tracker_doc.write_text(
        "Stub: agent-skills preflight satisfied for reasoning-effort tests.\n",
        encoding="utf-8",
    )

    async def _fake_run(cfg: RunConfig) -> int:
        captured.append(cfg)
        return 0

    from ralph_afk import loop as loop_module

    monkeypatch.setattr(loop_module, "run", _fake_run)


def test_main_auto_derives_xhigh_for_default_model(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Default invocation pins ``reasoning_effort="xhigh"`` for the default model.

    This is the regression case from the user's report:
    ``claude-opus-4.7-xhigh`` rejected the service-default ``medium``
    with a CAPI 400. The CLI must compose a config that pins the
    effort to ``xhigh`` so the very first SDK call is accepted.
    """
    captured: list[RunConfig] = []
    _install_fake_runner(monkeypatch, captured, tmp_path)

    exit_code = cli_module.main([])

    assert exit_code == 0
    assert len(captured) == 1
    cfg = captured[0]
    assert cfg.model == "claude-opus-4.7-xhigh"
    assert cfg.reasoning_effort == "xhigh"


def test_main_leaves_reasoning_effort_unset_for_non_pinned_model(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A non-pinned model resolves to ``reasoning_effort=None``.

    Models without a recognised ``-<effort>`` suffix preserve today's
    behaviour: the SDK omits the ``reasoningEffort`` payload field and
    the backend applies its own default.
    """
    monkeypatch.setenv("MODEL", "claude-sonnet-4.6")
    captured: list[RunConfig] = []
    _install_fake_runner(monkeypatch, captured, tmp_path)

    exit_code = cli_module.main([])

    assert exit_code == 0
    assert captured[0].model == "claude-sonnet-4.6"
    assert captured[0].reasoning_effort is None


def test_main_env_override_wins_over_model_suffix(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``REASONING_EFFORT`` env var overrides the model-suffix default.

    Lets an operator force a specific reasoning level on a non-pinned
    model without having to rename the model id.
    """
    monkeypatch.setenv("MODEL", "claude-sonnet-4.6")
    monkeypatch.setenv("REASONING_EFFORT", "high")
    captured: list[RunConfig] = []
    _install_fake_runner(monkeypatch, captured, tmp_path)

    exit_code = cli_module.main([])

    assert exit_code == 0
    assert captured[0].reasoning_effort == "high"


def test_main_env_override_is_case_insensitive(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``REASONING_EFFORT`` is normalised to lower-case before forwarding.

    Mirrors the leniency the kit applies to other env vars
    (``RALPH_OTEL_ENABLED`` accepts ``"1"`` / ``"true"`` / ``"yes"``).
    The SDK's ``ReasoningEffort`` literal is lowercase-only, so we
    canonicalise before constructing the :class:`RunConfig`.
    """
    monkeypatch.setenv("REASONING_EFFORT", "XHigh")
    captured: list[RunConfig] = []
    _install_fake_runner(monkeypatch, captured, tmp_path)

    exit_code = cli_module.main([])

    assert exit_code == 0
    assert captured[0].reasoning_effort == "xhigh"


def test_main_rejects_invalid_reasoning_effort_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An invalid ``REASONING_EFFORT`` is rejected eagerly with ``SystemExit``.

    The alternative — letting it through to the SDK and crashing
    mid-iteration — would leave the operator without an actionable
    stderr message and would burn a strike.
    """
    monkeypatch.setenv("REASONING_EFFORT", "ultra")
    captured: list[RunConfig] = []
    _install_fake_runner(monkeypatch, captured, tmp_path)

    with pytest.raises(SystemExit):
        cli_module.main([])

    assert captured == [], "loop must not be invoked when env validation fails"
