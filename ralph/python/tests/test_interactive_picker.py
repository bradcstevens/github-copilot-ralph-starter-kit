"""Tests for ``ralph_afk.interactive.picker`` (issue #24 — picker orchestration).

:func:`~ralph_afk.interactive.picker.resolve_run_model` is the seam the CLI calls
on the interactive path before the loop starts: a live ``list_models()`` fetch
feeds the two-stage Textual picker, and **any** failure (offline / unauthed /
list_models error / empty list) falls back to the env/default model + effort
already resolved into the :class:`RunConfig` (the static-matrix path), so the run
always proceeds.

These are **ungated** unit tests: the module imports neither Textual nor the SDK
at top (the picker app + ``CopilotClient`` are imported lazily), and the
orchestrator takes injectable ``fetch`` / ``run_app`` seams, so the fallback —
the issue's "static fallback is unit-tested" criterion — is exercised without a
TTY. The real Textual picker app is covered by the gated Pilot test in
``test_interactive_picker_app.py``.
"""

from __future__ import annotations

from types import SimpleNamespace

from ralph_afk.config import RunConfig
from ralph_afk.interactive import picker as picker_module
from ralph_afk.interactive.models import Selection
from ralph_afk.interactive.picker import fetch_live_models, resolve_run_model


def _config(model: str | None = "claude-opus-4.8", effort: str | None = "max") -> RunConfig:
    return RunConfig(model=model, reasoning_effort=effort)


def _model(id: str, *, efforts: list[str] | None = None, policy_state: str = "enabled") -> SimpleNamespace:
    supports = SimpleNamespace(vision=False, reasoning_effort=bool(efforts))
    limits = SimpleNamespace(max_context_window_tokens=200_000)
    return SimpleNamespace(
        id=id,
        name=id.upper(),
        capabilities=SimpleNamespace(supports=supports, limits=limits),
        policy=SimpleNamespace(state=policy_state, terms=""),
        billing=SimpleNamespace(multiplier=1.0),
        supported_reasoning_efforts=efforts,
        default_reasoning_effort=(efforts[0] if efforts else None),
    )


class _Warn:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def __call__(self, message: str) -> None:
        self.messages.append(message)


async def _unreachable_app(choices: object, *, cursor: int) -> Selection:
    raise AssertionError("the picker app must not run when the fetch fails")


# ---------------------------------------------------------------------------
# Fallback (the "static fallback is unit-tested" criterion)
# ---------------------------------------------------------------------------


async def test_fetch_failure_falls_back_to_env_default_with_warning() -> None:
    warn = _Warn()

    async def boom() -> list:
        raise RuntimeError("offline / unauthed")

    model, effort = await resolve_run_model(
        _config("gpt-5.4", "high"), warn=warn, fetch=boom, run_app=_unreachable_app
    )

    assert (model, effort) == ("gpt-5.4", "high")
    assert len(warn.messages) == 1
    assert "gpt-5.4" in warn.messages[0]


async def test_empty_model_list_falls_back_with_warning() -> None:
    warn = _Warn()

    async def empty() -> list:
        return []

    model, effort = await resolve_run_model(
        _config(), warn=warn, fetch=empty, run_app=_unreachable_app
    )

    assert (model, effort) == ("claude-opus-4.8", "max")
    assert len(warn.messages) == 1


# ---------------------------------------------------------------------------
# Success + cancel
# ---------------------------------------------------------------------------


async def test_success_returns_the_picker_selection() -> None:
    warn = _Warn()
    captured: list[tuple] = []

    async def fetch() -> list:
        return [_model("a", efforts=["high"]), _model("gpt-5.4", efforts=["low", "high"])]

    async def run_app(choices, *, cursor) -> Selection:
        captured.append((choices, cursor))
        return Selection("gpt-5.4", "high")

    model, effort = await resolve_run_model(
        _config(), warn=warn, fetch=fetch, run_app=run_app
    )

    assert (model, effort) == ("gpt-5.4", "high")
    assert warn.messages == []
    # The orchestrator projected the live models into picker rows.
    choices, _cursor = captured[0]
    assert [c.id for c in choices] == ["a", "gpt-5.4"]


async def test_cursor_passthrough_prehighlights_env_default() -> None:
    captured: list[int] = []

    async def fetch() -> list:
        return [_model("a", efforts=["high"]), _model("claude-opus-4.8", efforts=["max"]), _model("c")]

    async def run_app(choices, *, cursor) -> Selection:
        captured.append(cursor)
        return Selection("claude-opus-4.8", "max")

    await resolve_run_model(
        _config("claude-opus-4.8", "max"), warn=_Warn(), fetch=fetch, run_app=run_app
    )

    assert captured == [1]


async def test_cancelled_picker_falls_back_silently() -> None:
    warn = _Warn()

    async def fetch() -> list:
        return [_model("a", efforts=["high"])]

    async def cancel(choices, *, cursor) -> None:
        return None

    model, effort = await resolve_run_model(
        _config("gpt-5.4", "high"), warn=warn, fetch=fetch, run_app=cancel
    )

    # User quit the picker without choosing -> keep env/default; not an error.
    assert (model, effort) == ("gpt-5.4", "high")
    assert warn.messages == []


async def test_selection_with_no_effort_is_preserved() -> None:
    async def fetch() -> list:
        return [_model("claude-opus-4.5")]  # reasoning-incapable

    async def run_app(choices, *, cursor) -> Selection:
        return Selection("claude-opus-4.5", None)

    model, effort = await resolve_run_model(
        _config(), warn=_Warn(), fetch=fetch, run_app=run_app
    )

    assert model == "claude-opus-4.5"
    assert effort is None


# ---------------------------------------------------------------------------
# fetch_live_models: throwaway-client lifecycle
# ---------------------------------------------------------------------------


async def test_fetch_live_models_uses_throwaway_client_context() -> None:
    class _FakeClient:
        def __init__(self) -> None:
            self.entered = False
            self.exited = False

        async def __aenter__(self) -> "_FakeClient":
            self.entered = True
            return self

        async def __aexit__(self, *exc: object) -> bool:
            self.exited = True
            return False

        async def list_models(self) -> list[str]:
            return ["MODEL-A", "MODEL-B"]

    client = _FakeClient()
    models = await fetch_live_models(client_factory=lambda: client)

    assert models == ["MODEL-A", "MODEL-B"]
    assert client.entered is True
    assert client.exited is True


# ---------------------------------------------------------------------------
# Import guard: no Textual at module import time
# ---------------------------------------------------------------------------


def test_picker_module_does_not_import_textual_at_top() -> None:
    """picker.py stays importable (and fallback-testable) without the [tui] extra.

    Textual is imported lazily inside the default ``run_app`` only on the success
    path that actually shows the picker, so this module — and its fallback path —
    import cleanly in the base install.
    """
    import ast
    from pathlib import Path

    source = Path(picker_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    top_level_imports: set[str] = set()
    for node in tree.body:  # module body only -> top-level imports
        if isinstance(node, ast.Import):
            for alias in node.names:
                top_level_imports.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            top_level_imports.add(node.module.split(".")[0])
    assert "textual" not in top_level_imports
    assert "copilot" not in top_level_imports
