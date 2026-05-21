# The Full Workflow

> The end-to-end loop this kit is designed for. Seven phases, six of them human-driven. The autonomous AFK loop is **one** phase, not the whole story.

```
Idea → Grill → Brief → PRD → Issues → Triage → [AFK loop] → QA → Repeat
 ^                                                                  |
 └────────────────────── new issues ───────────────────────────────┘
```

Every step up to "AFK loop" is **human-in-the-loop**. Once you kick off the loop, you go AFK. QA is yours again — it's where you impose taste.

You don't need to use every phase. The skills are independent — pick what helps. The phases are ordered for new projects; experienced users skip phases that don't apply (e.g., a tiny one-off change might go straight from a `gh issue create` to `/triage`).

## Phase 1 — Alignment (`/grill-me`, then `/grill-with-docs`)

**Goal:** Reach a shared design concept with the agent before producing any artifacts.

This is the most important phase and the one most people skip. The "specs to code" antipattern — generating specs without keeping the existing code in the loop — produces plans that don't survive contact with the codebase.

```bash
copilot
> /grill-me  # then paste or reference your starting brief
```

The skill interviews you relentlessly, walks each branch of the design tree, gives its recommended answer before asking each question, and asks one question at a time. Sessions can run 20–80+ questions. **Don't let it jump to a plan prematurely.** The output you want is alignment, not a document.

### `/grill-me` vs `/grill-with-docs` — pick the right one

The deciding axis isn't "is there a codebase?" — it's **whether durable shared vocabulary is load-bearing for the work**. Two questions to ask yourself:

1. Will the **artifact outlive this conversation** (code, ADRs, a deck, a system design, a training plan)?
2. Will the **terms you settle on cascade into other decisions** later — entity names, file names, network zone labels, character names, role names? And do you expect future sessions on the same domain where re-explaining context would be expensive?

If both are yes, reach for **`/grill-with-docs`**. The persistence layer is the real work here — it compiles the grilling output into `CONTEXT.md` (and, lazily, `docs/adr/`) so the next session doesn't restart from zero. Most ralph-loop projects sit here once they're past the first hour. `CONTEXT.md` is the canonical glossary; ADRs capture the irreversible-ish decisions.

If both are no, reach for **`/grill-me`**. One-shot exercises where the value is in the thinking, not the artifact: deciding whether to take a meeting, talking yourself through a single architectural choice you've already mostly made, sketching out an email or eulogy, processing a frustrating Slack thread before responding. The friction of maintaining a persisted glossary isn't worth it.

**Greenfield edge case (probably you, if you just cloned this kit).** In the first hour of a new project the temptation is to jump straight to `/grill-with-docs` because "vocabulary is most malleable early." Don't. **Use `/grill-me` first** until you actually have something to name. Defining a glossary for entities that don't exist yet front-loads ossification — premature glossary is the language version of premature optimization. Once you've roughed in a shape and the same three or four terms keep coming up, switch to `/grill-with-docs` to codify them. That's the moment `CONTEXT.md` starts earning its keep; before then, the `CONTEXT.md` stub at the repo root is fine to leave alone (it ships pre-populated with `> 📝` notes for `/grill-with-docs` to fill in on demand).

## Phase 2 — Brief

Once aligned, capture the result in `SPEC.md` using [`templates/SPEC.template.md`](../templates/SPEC.template.md). The brief is the canonical source for your domain language, scope, and decisions — anchor `AGENTS.md`, the PRD, and slice issues back to it. If `/grill-with-docs` has already produced a `CONTEXT.md`, the **Language** section there is the authoritative glossary the brief should reuse verbatim.

## Phase 3 — PRD (`/to-prd`)

```bash
> /to-prd  # in the same session as /grill-me, while context is still warm
```

Publishes the brief as the parent PRD issue in your GitHub Issues tracker (the canonical destination — see `docs/agents/issue-tracker.md` if `/setup-agent-skills` has been run for your repo). The PRD becomes the parent every slice issue links back to via its `## Parent` section.

## Phase 4 — Slice Issues (`/to-issues`)

```bash
# Start a new session (Memento Model — see docs/concepts.md).
copilot
> /to-issues
```

The skill re-explores the codebase, quizzes you on slice boundaries, and creates one GitHub Issue per **vertical slice** (schema + service + UI through every layer — never horizontal). Each issue carries `## Parent` and `## Acceptance criteria`, which are the two sections [`ralph/sh-afk.sh`](../ralph/sh-afk.sh) looks for when filtering AFK-ready work.

## Phase 5 — Triage (`/triage`)

```bash
> /triage
```

Walks the open issues through the five-label state machine (`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`). Only `ready-for-agent` issues are picked up by the AFK loop. The canonical label list lives in `docs/agents/triage-labels.md`.

## Phase 6 — AFK Loop (`ralph/sh-afk.sh` or `ralph/python/`)

This is the autonomous phase. Pick a runner, kick it off, walk away.

```bash
# Unlimited iterations, default model.
bash ralph/sh-afk.sh

# Cap at 50 iterations.
bash ralph/sh-afk.sh 50
```

**For everything else** — runner comparison, env vars, per-iteration flow, exit conditions, the commit-message contract, and how the prompt routes work to `/diagnose` / `/prototype` / `/tdd` / `/improve-codebase-architecture` / `/grill-with-docs` / `/zoom-out` — see [`docs/runners.md`](runners.md).

## Phase 7 — QA

Your turn again. Review the merged work, file follow-up issues, run `/triage` to relabel anything that needs human attention, and start the loop again.

---

**Next:**
- [`docs/concepts.md`](concepts.md) — the mental models behind why the workflow looks like this.
- [`docs/runners.md`](runners.md) — the AFK loop in detail.
- [`docs/customization.md`](customization.md) — tailoring `AGENTS.md`, `PROMPT.md`, and skills to your project.
- Back to [`README.md`](../README.md).
