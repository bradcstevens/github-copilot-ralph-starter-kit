"""``ralph_afk.config`` — frozen per-invocation configuration.

The :class:`RunConfig` dataclass is the single load-bearing config seam
between :mod:`ralph_afk.cli` (which composes it from CLI flags + env
vars + defaults) and :mod:`ralph_afk.loop` (which consumes it).

It also satisfies — structurally, via Python's :pep:`544` Protocol
machinery — the :class:`ralph_afk.session.SessionConfig` Protocol, so
the loop can pass the same object to :class:`~ralph_afk.session.IterationSession`
without an explicit conversion. The Protocol-conformance contract is:

- ``deny_tools: frozenset[str]``
- ``deny_skills: frozenset[str]``
- ``verbosity: int``
- ``render_reasoning: bool``

Design notes:

* **Frozen.** The loop reuses the same config across every iteration;
  freezing makes accidental mid-run mutation impossible.
* **No I/O at construction time.** ``pricing_file`` is a :class:`Path`
  reference — actually opening it is :func:`ralph_afk.pricing.load_pricing`'s
  job and only happens inside :func:`ralph_afk.loop.run`.
* **``otel_enabled`` is plumbed but inert in this slice.** Issue #12
  wires it; this slice just makes sure the flag survives the CLI →
  RunConfig → loop pipe so #12 doesn't have to re-touch the dataclass.
* **stdlib only.** Enforced by ``tests/test_config.py``'s import-guard
  test.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

__all__ = [
    "RunConfig",
    "REASONING_EFFORTS",
    "MODEL_REASONING_EFFORTS",
    "SUPPORTED_MODELS",
]

#: Per-model reasoning-effort capability matrix for the models the kit
#: officially supports. Maps each Copilot model id to the set of
#: reasoning-effort values that model accepts. An **empty set** means the
#: model does not support reasoning-effort configuration at all — the
#: live Copilot CLI hard-rejects ``session.create`` with
#: "does not support reasoning effort configuration" if a non-null
#: ``reasoningEffort`` is sent for such a model, so :mod:`ralph_afk.cli`
#: forces ``reasoning_effort=None`` for them. Models absent from this
#: table are treated as "unknown": the CLI warns and passes them through
#: unchanged (the Copilot CLI is the final authority on model validity).
#:
#: Keep this in lockstep with the Copilot CLI's ``models.list`` output.
MODEL_REASONING_EFFORTS: dict[str, frozenset[str]] = {
    "claude-opus-4.8": frozenset({"low", "medium", "high", "xhigh", "max"}),
    "claude-opus-4.7": frozenset({"low", "medium", "high", "xhigh", "max"}),
    "claude-opus-4.6": frozenset({"low", "medium", "high", "max"}),
    "claude-opus-4.5": frozenset(),
    "claude-sonnet-4.6": frozenset({"low", "medium", "high", "max"}),
    "claude-sonnet-4.5": frozenset(),
    "claude-haiku-4.5": frozenset(),
    "gpt-5.5": frozenset({"low", "medium", "high", "xhigh"}),
    "gpt-5.4": frozenset({"low", "medium", "high", "xhigh"}),
    "gpt-5.3-codex": frozenset({"low", "medium", "high", "xhigh"}),
    "gpt-5.4-mini": frozenset({"low", "medium", "high", "xhigh"}),
    "gpt-5-mini": frozenset({"low", "medium", "high"}),
    "gemini-3.1-pro-preview": frozenset({"low", "medium", "high"}),
    "gemini-3.5-flash": frozenset({"low", "medium", "high"}),
    "mai-code-1-flash-internal": frozenset({"low", "medium", "high"}),
}

#: The model ids the kit officially supports (the keys of
#: :data:`MODEL_REASONING_EFFORTS`). :mod:`ralph_afk.cli` uses this to
#: decide whether a requested model is "known" (full per-model effort
#: gating) or "unknown" (warn-and-pass-through).
SUPPORTED_MODELS: frozenset[str] = frozenset(MODEL_REASONING_EFFORTS)

#: Reasoning-effort values the kit accepts for the ``reasoning_effort``
#: knob, used by :mod:`ralph_afk.cli`'s suffix-derivation helper and
#: ``__post_init__`` validation as one shared source of truth for basic
#: (syntactic) validation — e.g. rejecting ``"ultra"``.
#:
#: This is a **superset** of the SDK's ``copilot.session.ReasoningEffort``
#: ``Literal`` (currently ``low``/``medium``/``high``/``xhigh``): the live
#: Copilot CLI/backend also accepts ``max`` for several models (verified
#: against ``session.create``), but the SDK's type stub has not caught up.
#: ``reasoning_effort`` is forwarded to the SDK as a plain ``str``, so the
#: value reaches the CLI verbatim regardless of the stub. The per-model
#: subset each model actually accepts lives in
#: :data:`MODEL_REASONING_EFFORTS`; this flat union is the syntactic gate.
REASONING_EFFORTS: frozenset[str] = frozenset(
    {"low", "medium", "high", "xhigh", "max"}
)


@dataclass(frozen=True)
class RunConfig:
    """Frozen per-invocation configuration for the ``ralph-afk`` runner.

    Attributes:
        model: Optional Copilot model id override. ``None`` lets the SDK
            pick its default (which respects ``~/.copilot`` config).
        reasoning_effort: Optional reasoning-effort override forwarded to
            ``copilot.CopilotClient.create_session``. One of ``"low"`` /
            ``"medium"`` / ``"high"`` / ``"xhigh"`` / ``"max"`` or ``None``
            (let the SDK / service pick). Model id and reasoning effort
            are **separate axes**: the value here is sent verbatim while
            :attr:`model` carries a bare base id. Not every model accepts
            every effort — some accept none at all — so :mod:`ralph_afk.cli`
            gates this against :data:`MODEL_REASONING_EFFORTS` before
            composing the config. ``REASONING_EFFORT`` env overrides the
            value derived from a ``MODEL`` suffix or the kit default.
        issue_source: ``"github"`` (default) for the GitHub-issue-backed
            collector or ``"prds"`` for the legacy local-markdown layout.
            This slice (#10) only implements ``"github"``; ``"prds"``
            lands in #11 and the loop raises :class:`NotImplementedError`
            for it.
        include_prs: Whether ``ready-for-agent`` pull requests (with an
            agent brief) join the AFK-ready pool alongside issues. ``None``
            (default) means "no explicit override" — the loop auto-detects
            the PR surface from ``docs/agents/issue-tracker.md`` (the
            ``PRs as a request surface: yes/no`` flag that
            ``/setup-agent-skills`` writes and ``/triage`` reads). ``True`` /
            ``False`` force the behaviour regardless of that file. Only
            meaningful for ``issue_source == "github"``.
        max_iterations: Cap on iterations. ``0`` (the default) means
            unlimited.
        max_nmt_strikes: Consecutive no-progress iterations tolerated
            before the loop aborts non-zero. Must be ≥ 1.
        deny_tools: Tool names to reject at the SDK permission gate.
        deny_skills: Skill names (the ``arguments.skill`` value passed
            to the ``skill`` meta-tool) to reject.
        verbosity: 0 (default) / 1 (``-v``) / 2 (``-vv``) / 3 (``-vvv``).
        render_reasoning: ``False`` suppresses assistant reasoning output
            regardless of verbosity. Default ``True``.
        otel_enabled: ``True`` when OpenTelemetry tracing is enabled
            (either ``RALPH_OTEL_ENABLED=1`` or
            ``OTEL_EXPORTER_OTLP_ENDPOINT`` is set). The OTel wiring
            itself lands in issue #12; this slice just plumbs the flag.
        pricing_file: Optional explicit path to a ``pricing.toml``.
            ``None`` lets :func:`ralph_afk.pricing.load_pricing` resolve
            from ``RALPH_PRICING_FILE`` or the packaged default.
    """

    model: str | None = None
    reasoning_effort: str | None = None
    issue_source: Literal["github", "prds"] = "github"
    include_prs: bool | None = None
    max_iterations: int = 0
    max_nmt_strikes: int = 3
    deny_tools: frozenset[str] = field(default_factory=frozenset)
    deny_skills: frozenset[str] = field(default_factory=frozenset)
    verbosity: int = 0
    render_reasoning: bool = True
    otel_enabled: bool = False
    pricing_file: Path | None = None

    def __post_init__(self) -> None:
        if self.issue_source not in ("github", "prds"):
            raise ValueError(
                f"issue_source must be 'github' or 'prds', got "
                f"{self.issue_source!r}"
            )
        if self.max_iterations < 0:
            raise ValueError(
                f"max_iterations must be ≥ 0 (0 = unlimited), got "
                f"{self.max_iterations}"
            )
        if self.max_nmt_strikes < 1:
            raise ValueError(
                f"max_nmt_strikes must be ≥ 1, got {self.max_nmt_strikes}"
            )
        if self.verbosity < 0 or self.verbosity > 3:
            raise ValueError(
                f"verbosity must be in 0..3, got {self.verbosity}"
            )
        if (
            self.reasoning_effort is not None
            and self.reasoning_effort not in REASONING_EFFORTS
        ):
            raise ValueError(
                f"reasoning_effort must be one of "
                f"{sorted(REASONING_EFFORTS)} or None, got "
                f"{self.reasoning_effort!r}"
            )
