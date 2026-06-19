"""Tests for the CLI's model + ``reasoning_effort`` resolution.

Model id and reasoning effort are **separate axes** on the live Copilot
CLI: the model id must be a bare base id (``claude-opus-4.8``), and the
effort is sent alongside it. A suffixed id like ``claude-opus-4.7-xhigh``
is rejected ("not available"), so the CLI peels a recognised
``-<effort>`` suffix off ``MODEL`` into ``reasoning_effort``.

These tests pin the resolution behaviour:

* suffix derivation / stripping (``_split_model_suffix`` via
  ``_derive_reasoning_effort_from_model`` and the end-to-end config);
* the kit default (model ``claude-opus-4.8`` + effort ``max``);
* the per-model capability gate (a reasoning-incapable model is forced
  to ``None``; an unknown model warns and passes through);
* the ``REASONING_EFFORT`` env override + validation (an invalid override
  is a hard ``SystemExit``, not a mid-iteration crash).

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
        ("claude-opus-4.8-max", "max"),
        ("claude-sonnet-4.6", None),
        ("gpt-5.4", None),
        ("gpt-5-mini", None),
        # Wordy tails that merely look like a suffix must NOT be stripped.
        ("gpt-5.4-mini", None),
        ("gpt-5.3-codex", None),
        ("mai-code-1-flash-internal", None),
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
    """
    monkeypatch.setattr(cli_module, "resolve_repo_root", lambda: tmp_path)

    async def _fake_run(cfg: RunConfig) -> int:
        captured.append(cfg)
        return 0

    from ralph_afk import loop as loop_module

    monkeypatch.setattr(loop_module, "run", _fake_run)


def test_main_default_invocation_uses_base_model_and_default_effort(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A pure default invocation pins the kit default model + effort.

    Model id and reasoning effort are separate axes on the live CLI, so
    the composed config must carry a **bare base** model id
    (``claude-opus-4.8``) — not a ``-<effort>`` suffixed id, which the
    CLI rejects as "not available" — plus the kit's default effort.
    """
    captured: list[RunConfig] = []
    _install_fake_runner(monkeypatch, captured, tmp_path)

    exit_code = cli_module.main([])

    assert exit_code == 0
    assert len(captured) == 1
    cfg = captured[0]
    assert cfg.model == "claude-opus-4.8"
    assert cfg.reasoning_effort == "max"


def test_main_strips_effort_suffix_to_base_model_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A suffixed ``MODEL`` is split into a base id + reasoning effort.

    Regression for the live-CLI bug: ``MODEL=claude-opus-4.7-xhigh`` was
    sent verbatim and rejected ("Model 'claude-opus-4.7-xhigh' is not
    available."). The CLI must send the bare base id ``claude-opus-4.7``
    while honouring the ``xhigh`` effort.
    """
    monkeypatch.setenv("MODEL", "claude-opus-4.7-xhigh")
    captured: list[RunConfig] = []
    _install_fake_runner(monkeypatch, captured, tmp_path)

    exit_code = cli_module.main([])

    assert exit_code == 0
    assert captured[0].model == "claude-opus-4.7"
    assert captured[0].reasoning_effort == "xhigh"


def test_main_forces_none_effort_for_reasoning_incapable_model(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A model with no reasoning support resolves to ``reasoning_effort=None``.

    The live CLI hard-rejects ``session.create`` if any effort is sent
    for such a model, so the CLI layer must drop it — even when the
    operator explicitly requested one (with a warning).
    """
    monkeypatch.setenv("MODEL", "claude-haiku-4.5")
    monkeypatch.setenv("REASONING_EFFORT", "high")
    captured: list[RunConfig] = []
    _install_fake_runner(monkeypatch, captured, tmp_path)

    exit_code = cli_module.main([])

    assert exit_code == 0
    assert captured[0].model == "claude-haiku-4.5"
    assert captured[0].reasoning_effort is None


def test_main_accepts_max_effort(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``REASONING_EFFORT=max`` is accepted (live CLI takes it; SDK stub lags)."""
    monkeypatch.setenv("MODEL", "claude-opus-4.8")
    monkeypatch.setenv("REASONING_EFFORT", "max")
    captured: list[RunConfig] = []
    _install_fake_runner(monkeypatch, captured, tmp_path)

    exit_code = cli_module.main([])

    assert exit_code == 0
    assert captured[0].reasoning_effort == "max"


def test_main_unknown_model_passes_through_with_warning(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An unknown model id is passed through unchanged with a stderr warning.

    The kit chooses warn-and-pass-through (the Copilot CLI is the final
    authority on model validity) rather than a hard allowlist.
    """
    monkeypatch.setenv("MODEL", "some-future-model-9")
    captured: list[RunConfig] = []
    _install_fake_runner(monkeypatch, captured, tmp_path)

    exit_code = cli_module.main([])

    assert exit_code == 0
    assert captured[0].model == "some-future-model-9"
    err = capsys.readouterr().err
    assert "not in the kit's supported model set" in err


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
