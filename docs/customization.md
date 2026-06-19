# Customization

> Once the [Quick Start](../README.md#quick-start) is done, this is where you tailor the kit to your project — repo structure, AGENTS.md, PROMPT.md, and the per-repo skill configuration written by `/setup-agent-skills`.

## Repo structure reference

### What ships in the kit

```
.
├── CONTEXT.md                      # Domain glossary stub. Referenced by AGENTS.md, PRDs, and slice issues; extended lazily by /grill-with-docs.
├── LICENSE
├── README.md                       # Quickstart.
├── docs/                           # Kit documentation (you're reading docs/customization.md right now).
│   ├── concepts.md
│   ├── workflow.md
│   ├── runners.md
│   └── customization.md
├── templates/                      # Per-repo config templates. Copy each to the repo root and fill in.
│   ├── AGENTS.template.md          # → AGENTS.md. Loaded into every Copilot CLI invocation. /setup-agent-skills populates its trailing block.
│   └── SPEC.template.md            # → SPEC.md. The brief that /to-prd consumes.
├── .copilot/skills/                # Vendored project-local copy of every skill the loop routes to.
│   ├── setup-agent-skills/         # ⭐ Run FIRST in a new project — scaffolds the per-repo `## Agent skills` block and docs/agents/*.md.
│   ├── grill-me/                   # Phase 1 alignment interview.
│   ├── grill-with-docs/            # Stress-test a plan against CONTEXT.md and docs/adr/.
│   ├── to-prd/                     # Brief → published PRD issue.
│   ├── to-issues/                  # PRD → AFK-ready slice issues.
│   ├── triage/                     # Label state machine (needs-triage / ready-for-agent / …).
│   ├── diagnose/                   # Disciplined bug repro → fix loop.
│   ├── prototype/                  # Sketch logic or UI before committing to a slice.
│   ├── tdd/                        # Red → green → refactor discipline for slice implementation.
│   ├── improve-codebase-architecture/  # Surface deepening / refactor candidates.
│   ├── zoom-out/                   # Higher-level map of an unfamiliar area.
│   ├── find-skills/                # Discover other installed skills on demand.
│   ├── write-a-skill/              # Author or update a skill.
│   ├── handoff/                    # Compact a long human-driven session into a continuation doc.
│   ├── microsoft-foundry/          # Azure AI Foundry helpers (delete if not on Microsoft tech).
│   └── caveman/                    # Token-compressed output mode (off by default in the loop).
└── ralph/
    ├── afk.sh                      # Optional one-line convenience launcher for the Python runner.
    ├── PROMPT.md                   # Agent prompt loaded each iteration.
    └── python/                     # The AFK runner, on the GitHub Copilot Python SDK. See ralph/python/README.md.
```

### What you'll add when adopting

```
├── AGENTS.md                       # Filled-in copy of templates/AGENTS.template.md.
├── SPEC.md                         # Filled-in copy of templates/SPEC.template.md.
├── docs/
│   ├── adr/                        # Architecture decision records (created lazily by /grill-with-docs).
│   └── agents/                     # Per-repo skill config — written by /setup-agent-skills.
│       ├── issue-tracker.md        #   Where issues live (GitHub / GitLab / local markdown / other).
│       ├── triage-labels.md        #   Label vocabulary used by /triage.
│       └── domain.md               #   Single- vs multi-context layout for CONTEXT.md / ADRs.
├── prds/                           # Optional legacy local-markdown PRDs (ISSUE_SOURCE=prds).
├── issues/                         # Optional legacy local-markdown issues.
└── <your application code>
```

## The two files you almost always edit

- **`AGENTS.md`** (scaffold from [`templates/AGENTS.template.md`](../templates/AGENTS.template.md)) — fill in **Tech stack** and **Feedback loops**. The loop reads the **Feedback loops** table to know what commands to run before committing. If lint / type-check / test / build commands are wrong here, the agent guesses and CI catches the difference. The trailing **Agent skills** block is owned by `/setup-agent-skills`; don't hand-edit it the first time around.
- **[`ralph/PROMPT.md`](../ralph/PROMPT.md)** — usually leave defaults; only change if you want different skill routing or different commit-message conventions. If you change the commit-message convention, also update the `CLOSE_KEYWORD_RE` regex used by `extract_close_refs` in [`ralph/python/ralph_afk/wrapper.py`](../ralph/python/ralph_afk/wrapper.py) so the auto-close backstop still matches what the agent emits.

The **template files** ([`templates/AGENTS.template.md`](../templates/AGENTS.template.md), [`templates/SPEC.template.md`](../templates/SPEC.template.md), and the [`CONTEXT.md`](../CONTEXT.md) stub at the repo root) each include a `> 📝` placeholder convention and a `> 🗑️ DELETE IF NOT APPLICABLE` convention. Grep for `<[A-Z_]` to find what's left to replace.

## `/setup-agent-skills` — the entry-point skill

This skill is the first thing to run in Copilot CLI for any new project, **before** any of the other planning or implementation skills. It does two things:

1. **Populates the `## Agent skills` block at the bottom of `AGENTS.md`** with concrete pointers to the per-repo config below.
2. **Writes `docs/agents/{issue-tracker,triage-labels,domain}.md`** — the per-repo config files that every other skill (`/to-issues`, `/triage`, `/to-prd`, `/diagnosing-bugs`, `/tdd`, `/improve-codebase-architecture`, `/zoom-out`) reads to learn which issue tracker, label vocabulary, and context layout this project uses.

The skill walks you through three decisions one at a time:

| Decision | What it controls | Defaults |
| --- | --- | --- |
| **Issue tracker** | Whether downstream skills call `gh issue create`, `glab issue create`, write a markdown file under `.scratch/`, or follow custom prose. | GitHub (if a `git remote` points at GitHub), GitLab (if it points at GitLab), local markdown (no remote), or "other" (free-form). |
| **Triage labels** | The exact strings `/triage` applies for each of the five canonical roles. | `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix` — verbatim. Override per-role if your existing tracker uses different names. |
| **Domain docs** | Whether the repo has one global `CONTEXT.md` or a `CONTEXT-MAP.md` pointing to per-context files. | Single-context (most repos). Pick multi-context only if you actually have multiple bounded contexts. |

### Skip it and downstream skills will guess

If `/to-issues`, `/triage`, `/to-prd`, `/diagnosing-bugs`, `/tdd`, or `/improve-codebase-architecture` ever feel like they're missing context about your issue tracker, label vocabulary, or domain layout — that's the signal you skipped this step. Run `/setup-agent-skills` now.

### Re-running it

`/setup-agent-skills` is idempotent. Re-run it whenever you want to:

- Switch issue trackers (GitHub → GitLab → local markdown → other).
- Rename triage labels (e.g., to match a label scheme your repo already has).
- Move from single-context to multi-context (or vice versa).

It edits the existing `## Agent skills` block in place and rewrites `docs/agents/*.md`. Your hand-edits inside `docs/agents/*.md` are preserved when possible, but if you've done substantial customization there, diff before accepting the rewrite.

### Auto-bootstrap behavior

The kit ships with a **two-layer auto-bootstrap** so a forgotten `/setup-agent-skills` doesn't lead to silent agent guessing:

| Layer | Where | What it does |
| --- | --- | --- |
| **Interactive sessions** | Top of `AGENTS.md` (the "First-run bootstrap" directive in [`templates/AGENTS.template.md`](../templates/AGENTS.template.md), loaded into every Copilot CLI invocation) | If `docs/agents/issue-tracker.md` does not exist, the agent invokes `/setup-agent-skills` as its first action — **before** acting on the user's request — then returns to the original ask. |
| **AFK loop runner** | Preflight check in [`ralph/python/`](../ralph/python/) | If `docs/agents/issue-tracker.md` does not exist, the runner exits non-zero **before** the first iteration with a stderr message pointing the operator at `/setup-agent-skills`. Refuses to start because the skill is interactive and cannot safely run under `copilot --yolo -p`. |

The two layers compose: a human starts a fresh repo, runs `uv run --project ralph/python ralph-afk`, gets a clear error, opens `copilot` interactively, sees the AGENTS.md directive auto-trigger `/setup-agent-skills`, answers the three questions, then re-runs the loop. Detection uses the existence of `docs/agents/issue-tracker.md` as the signal that the skill has run.

## Skills reference

The kit ships with a curated subset of the upstream skill library, vendored under [`.copilot/skills/`](../.copilot/skills). The full upstream catalog is the [`mattpocock/ai-engineer-workshop-2026-project`](https://github.com/mattpocock/ai-engineer-workshop-2026-project) library; the GitHub Copilot CLI marketplace has more skills beyond that.

To discover more skills beyond what's vendored:

```bash
# From inside copilot:
> /find-skills <query>

# From the shell:
npx skills find <query>
```

For the breakdown of which skills the AFK loop will and won't invoke, see [`docs/runners.md` → Skill routing](runners.md#skill-routing).

## Stack-agnostic defaults

This kit doesn't care whether your project is Python, Node, Rust, Go, or something else. The single point of stack-specific configuration is the **Feedback loops** table in `AGENTS.md` — fill it in once with your project's lint / type-check / test / build commands, and both the human-driven skills and the AFK loop will read from it.

If you're on Azure or Microsoft tech, the [`templates/AGENTS.template.md`](../templates/AGENTS.template.md) **Azure conventions** and **Microsoft tooling** sections are worth keeping (they document the `SecurityControl=Ignore` tag and the `disableLocalAuth: false` default for Foundry resources). Otherwise delete them.

---

**Next:**
- [`docs/workflow.md`](workflow.md) — the seven-phase workflow these skills slot into.
- [`docs/runners.md`](runners.md) — runner comparison and AFK loop contract.
- [`docs/concepts.md`](concepts.md) — the mental models behind the design.
- Back to [`README.md`](../README.md).
