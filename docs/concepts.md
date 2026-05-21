# Concepts

> The two mental models the rest of this kit is built around. Internalize these before reaching for any of the runners or skills — every other design choice flows from them.

This kit assumes a particular shape of LLM-driven engineering. The shape is not "give the AI everything and let it figure out the rest"; it's "size the work so the AI stays in its competent envelope, and restart from a known-good state every iteration." Those two ideas have names.

## The Smart Zone / Dumb Zone

LLMs degrade as context grows. Attention relationships scale quadratically with tokens. A practical threshold: **~100k tokens is your smart zone ceiling**, regardless of whether the model advertises 200k or 1M. Past that you're in the dumb zone — the model starts making stupid decisions.

**Implication:** Size every task so it fits inside the smart zone. Never let the AI bite off more than fits.

This is why the workflow ([`docs/workflow.md`](workflow.md)) breaks a brief down into **vertical slice issues** before the AFK loop ever runs, and why each iteration of the loop is one fresh Copilot CLI invocation against one slice — not a long-lived session accreting state.

## The Memento Model

Every iteration starts from zero (system prompt + `AGENTS.md` + the issue). The agent forgets everything between iterations. This is a feature, not a bug — **optimize for it** rather than fighting it with compaction. A cleared context is always a known, clean state. Compacted sediment is unpredictable.

[`ralph/sh-afk.sh`](../ralph/sh-afk.sh) (and its Python peer at [`ralph/python/`](../ralph/python/)) invokes a fresh `copilot --yolo -p` per iteration on purpose. The two channels through which state survives between iterations are deliberate and narrow:

- **Git commits.** The previous iteration's commits are the durable record of what was done.
- **Issue tracker state.** Closing an issue (and the wrapper's auto-close backstop) is how "this slice is done" propagates forward.

That's it. No handoff documents, no scratchpads, no compaction. If something doesn't survive in commits or issue state, the next iteration won't see it — and that's the right behavior.

## What this kit gives you

A scaffold for a project that uses this shape end-to-end:

- **Per-repo configuration templates** under [`templates/`](../templates/) — `AGENTS.md` (loaded into every Copilot CLI invocation) and `SPEC.md` (the brief that `/to-prd` consumes).
- **A vendored copy of every Copilot CLI skill the workflow routes to**, under [`.copilot/skills/`](../.copilot/skills) — alignment, planning, implementation, and meta.
- **Two interchangeable AFK runners** — pure-bash for the minimal-deps audience, Python on the Copilot SDK for the richer observability audience. Both share the same wrapper contract. See [`docs/runners.md`](runners.md).
- **Stack-agnostic.** Customize the **Feedback loops** table in `AGENTS.md` once for your project's lint / type-check / test / build commands; both the human-driven skills and the AFK loop read from it.

## Inspiration

Inspired by the [AI Engineer Workshop 2026](https://github.com/mattpocock/ai-engineer-workshop-2026-project) workflow, ported to the GitHub Copilot CLI.

---

**Next:**
- [`docs/workflow.md`](workflow.md) — the seven-phase workflow that operationalizes these models.
- [`docs/runners.md`](runners.md) — pick a runner and learn the AFK loop's contract.
- Back to [`README.md`](../README.md) — quickstart and "where to go next".
