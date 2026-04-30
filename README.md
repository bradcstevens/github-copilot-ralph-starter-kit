# GitHub Copilot Ralph Starter Kit

A starter kit for running an **AFK (away-from-keyboard) AI coding loop** on top of the **GitHub Copilot CLI**. Drop the skills, prompts, and runner scripts into your project, point them at a kanban of issues, and let an agent implement them autonomously while you do something else.

> Inspired by the [AI Engineer Workshop 2026](https://github.com/mattpocock/ai-engineer-workshop-2026-project) workflow, ported to the GitHub Copilot CLI.

**What you get:**
- Four reusable Copilot CLI skills: `/grill-me`, `/write-prd`, `/prd-to-issues`, `/improve-codebase-architecture`, plus a `/tdd` discipline skill.
- Two ready-to-run loop scripts: `ralph/ralph-once.sh` (single supervised pass) and `ralph/ralph-afk.sh` (full Docker-sandboxed loop until the backlog drains).
- A shared `ralph/prompt.md` you can tune to fit your stack.

**Stack-agnostic.** The example commands assume a Node/TypeScript project with `npm test` / `npm run typecheck` feedback loops, but you can swap those for any language's equivalents by editing `ralph/prompt.md`.

---

## Core Mental Models

Before touching anything, internalize these two constraints — everything in this workflow flows from them.

### The Smart Zone / Dumb Zone

LLMs degrade as context grows. Attention relationships scale quadratically with tokens. A practical threshold: **~100k tokens is your smart zone ceiling**, regardless of whether your model advertises 200k or 1M. Past that you're in the dumb zone — the model starts making stupid decisions.

**Implication:** Size every task so it fits inside the smart zone. Never let the AI bite off more than fits.

### The Memento Model

Every session starts from zero (system prompt). The agent forgets everything between sessions. This is a feature, not a bug — **optimize for it** rather than fighting it with compacting. A cleared context is always a known, clean state. Compacted sediment is unpredictable.

---

## Quick Start

```bash
# 1. Clone this starter kit into a new project (or copy the relevant pieces
#    into an existing one).
git clone https://github.com/bradcstevens/github-copilot-ralph-starter-kit my-project
cd my-project

# 2. Install the skills at the user level so /skillname works in any session.
mkdir -p ~/.copilot/skills
cp -R .copilot/skills/* ~/.copilot/skills/

# 3. Customize ralph/prompt.md for your stack (test command, typecheck command,
#    repo conventions). The defaults assume npm test / npm run typecheck.

# 4. (Optional) Add a minimal AGENTS.md to your repo root.

# 5. Drop a starting brief into client-brief.md and start the workflow below.
copilot
> /grill-me — see client-brief.md for context
```

You don't need to use every phase. The skills are independent — pick what helps.

---

## Repo Structure Reference

```
.
├── .copilot/skills/          # Project-local copy of the skills (also installed at ~/.copilot/skills/)
│   ├── grill-me/SKILL.md
│   ├── write-prd/SKILL.md
│   ├── prd-to-issues/SKILL.md
│   ├── improve-codebase-architecture/SKILL.md
│   └── tdd/SKILL.md
├── ralph/
│   ├── ralph-once.sh         # Single AFK implementation pass (human-supervised, runs on host)
│   ├── ralph-afk.sh          # Full autonomous loop (Docker sandboxed, runs until <promise>NO MORE TASKS</promise>)
│   └── prompt.md             # Shared agent prompt used by both scripts
└── README.md
```

When you adopt this in a real project, you'll typically add:

```
├── app/                      # Your application code
├── prds/                     # PRD docs (one file per feature): prds/<YYYY-MM-DD>-<core-name>-prd.md
├── issues/                   # Local markdown issue files grouped per PRD: issues/<core-name>/NNN-*.md
├── client-brief.md           # Starting brief for /grill-me
└── AGENTS.md                 # Agent behavior configuration (read by Copilot CLI)
```

---

## The Full Workflow

```
Idea → Grill → PRD → Kanban → [AFK Implement Loop] → QA → Repeat
 ^                                                           |
 └───────────────────── new issues ─────────────────────────┘
```

Every step up to "AFK Implement Loop" is **human-in-the-loop**. Once you kick off the loop, you go AFK. QA is yours again — it's where you impose taste.

---

## Phase 1 — Alignment (Grill Me)

**Goal:** Reach a shared design concept with the agent before producing any artifacts.

This is the most important phase and the one most people skip. The "specs to code" antipattern — generating specs without keeping the existing code in the loop — produces plans that don't survive contact with the codebase. Don't ignore the code, don't vibe code your way through planning.

### How to run it

```bash
# Start a fresh Copilot CLI session
copilot

# Clear context, then invoke the skill
/grill-me

# Paste or reference client-brief.md
# Example:
> /grill-me — see client-brief.md for context
```

The `grill-me` skill instructs the agent to:
- Interview you relentlessly about every aspect of the plan
- Walk down each branch of the design tree, resolving dependencies one by one
- Provide its recommended answer for each question before asking
- Ask questions **one at a time**

### What to expect

- A sub-agent fires off to explore the repo (~90–100k tokens on Opus — isolated context, drip-feeds summary back)
- You get a stream of targeted questions: data model decisions, scope boundaries, retroactive concerns, UI placement, etc.
- You can ask the agent to pull more repo context at any time
- Sessions can run 20–80+ questions; you control the pace

### Key discipline

Don't let the agent jump to a plan prematurely. If it tries, push back. The **output you want is alignment** — a shared design concept — not a plan document. The PRD comes next.

```
Token budget tip: after a full grill session you'll have ~20–30k tokens used.
That's gold. Don't compact it. Move directly to Phase 2 in the same session.
```

---

## Phase 2 — Destination Document (PRD)

**Goal:** Crystallize the design concept into a durable artifact.

Run this immediately after the grill session, while the shared context is still warm.

### How to run it

```bash
# In the same session, invoke the next skill
> /write-prd
```

The `write-prd` skill will:
1. Ask for a long detailed description of the problem (pull from the grill session context)
2. Optionally re-explore the repo if it's a fresh session
3. Produce a structured PRD with: problem statement, solution, user stories, implementation decisions, out-of-scope items, and proposed modules to modify

### PRD structure (what gets generated)

| Section | Purpose |
|---|---|
| Problem Statement | What the user is experiencing |
| Solution | What you're building |
| User Stories | Cucumber-style acceptance criteria |
| Implementation Decisions | Architectural choices made during grill |
| Out of Scope | Definition of done — what you're NOT doing |
| Proposed Modules | Specific files/services to create or touch |

### Do you need to read it?

If the grill session worked, you have a shared design concept and the LLM is good at summarization. What matters is the modules section. Scan that. Make sure it reflects the deep module design you want (more on this in Phase 6).

The PRD gets written directly to `prds/<YYYY-MM-DD>-<core-name>-prd.md` (e.g. `prds/2026-04-29-per-image-object-quantities-prd.md`). Don't commit it long-term — doc rot is real. Once the feature ships, close/archive it.

---

## Phase 3 — Kanban Board (PRD → Issues)

**Goal:** Break the destination into independently parallelizable work units using vertical slices.

### The horizontal vs. vertical trap

AI defaults to **horizontal slices** — all schema work in phase 1, all API work in phase 2, all frontend in phase 3. This is bad because you get no integrated feedback until the very end.

**Vertical slices (tracer bullets)** cut through all layers at once. After issue 1 completes, you should be able to load a page, click something, and see it work end-to-end — even if it's minimal.

A valid first vertical slice for a gamification feature:
- Schema migration for `points` table ✓
- `GamificationService` with `awardPoints()` method ✓
- Dashboard showing a user's point total ✓

An invalid (horizontal) first slice:
- Just the schema changes

### How to run it

```bash
# Start a new Copilot CLI session (clear context — Memento model)
copilot

> /prd-to-issues
```

The skill will:
1. Locate the PRD in `prds/`
2. Re-explore the codebase
3. Quiz you on slice boundaries
4. Output local markdown issue files into `issues/<core-name>/NNN-*.md` with blocking relationships declared

### Review the output

This is cheap to do and important. Look for:

- **Blocking relationships** — are they correct? Issue 3 blocked by issues 1 and 2?
- **First issue is a vertical slice** — does it touch schema + service + UI?
- **AFK vs. human-in-the-loop tags** — implementation is AFK, planning/grilling is not
- **Parallelizability** — can issues 2 and 3 run simultaneously after issue 1?

Correct horizontal slices before proceeding. The kanban structure turns your sequential plan into a DAG — that's what enables parallel agents later.

---

## Phase 4 — AFK Implementation Loop (Ralph)

**Goal:** Let agents implement the kanban backlog autonomously.

### ralph-once.sh — start here

```bash
# From repo root — process every open issue
bash ralph/ralph-once.sh

# Or scope to one (or more) PRD issue folders by name. <prd-folder> is any
# subfolder under issues/ (typically created by the prd-to-issues skill).
bash ralph/ralph-once.sh <prd-folder>
bash ralph/ralph-once.sh <prd-folder-a> <prd-folder-b>
```

What it does:
1. Reads open issues from `issues/**/*.md` (excluding `done/` archives) into a local variable. If `prd-folder` arguments are supplied, only `issues/<folder>/**/*.md` is read for each one.
2. Grabs the last 5 git commits (for context continuity)
3. Constructs a prompt with: issue backlog + commit history + implementation instructions
4. Runs `copilot --yolo` directly on the host

The agent will:
- Pick the highest-priority AFK issue it can unblock
- Run **TDD: red-green-refactor** (write failing test → implement → confirm green)
- Execute your project's test and typecheck commands as feedback loops
- Self-correct type errors before finishing
- Output a commit + summary

Run this once, watch what it does, tune the prompt or skills if needed, then graduate to the full loop.

### ralph-afk.sh — full autonomous loop

```bash
# Run until every issue is done (no iteration cap)
bash ralph/ralph-afk.sh

# Optionally pass one or more PRD issue folders to scope work to those PRDs.
# <prd-folder> is any subfolder under issues/ (typically created by the
# prd-to-issues skill).
bash ralph/ralph-afk.sh <prd-folder>
bash ralph/ralph-afk.sh <prd-folder-a> <prd-folder-b>

# Help
bash ralph/ralph-afk.sh --help
```

This runs each iteration inside a **Docker sandbox** (isolated filesystem, no footguns) via `docker sandbox run copilot . -- --yolo --output-format json -p "..."`. The loop:
1. Picks next unblocked AFK issue
2. Implements with TDD in a git work tree (isolated branch)
3. Runs an automated **reviewer** pass in a fresh context (smart zone review, not dumb zone)
4. Merges if clean; resolves type/test conflicts if not
5. Loops to next issue **indefinitely**, only exiting when the agent emits `<promise>NO MORE TASKS</promise>` (or you Ctrl-C)

```
The loop has no iteration cap. The agent owns the exit condition by
emitting <promise>NO MORE TASKS</promise> when the kanban is drained.
If you don't want it running forever, scope it to a PRD folder or stop
it manually.
```

```
Key insight: the reviewer runs in a fresh context window.
Reviewing in the dumb zone (after implementation) = dumber reviewer than implementer.
Fresh context = smart zone review. Always separate these.
```

### Feedback loop quality is your ceiling

If the agent is producing garbage, the problem is almost always the feedback loops. Ask:
- Are your tests actually testing behavior or just wrapping functions?
- Does your type checker catch what matters?
- Do you have integration-level tests, or only unit tests on shallow modules?

A codebase with weak feedback loops produces weak AI output. No prompt engineering fixes that.

### Picking a Copilot CLI model

Both scripts shell out to the GitHub Copilot CLI, which lets you pin a model with `--model <id>`. `ralph-once.sh` already passes `--model claude-opus-4.7-xhigh`; `ralph-afk.sh` does the same inside the sandboxed CLI invocation. Pick based on the iteration's job — implementer, long-context reviewer, or fast reasoner.

| Model id | When to reach for it |
|---|---|
| `gpt-5.5` | Strong, fast generalist. Good default for `ralph-once.sh` when you want quick TDD iterations and don't need Opus-level long-form reasoning. Use for issue triage, scaffolding, and small vertical slices where latency matters more than depth. |
| `claude-opus-4.7` | Baseline Opus 4.7. Best general-purpose **AFK implementer** — deep reasoning over a normal context window with predictable cost. Solid choice for `ralph-afk.sh` when issues are well-scoped and individually fit inside the smart zone (~100k tokens). |
| `claude-opus-4.7-1m-internal` | 1M-token context window (internal). Use when a single iteration genuinely needs to ingest a huge surface — e.g. a kanban scoped to many PRDs at once, large monorepos, big migrations, or sweeping refactors that touch many modules. Remember: 1M ≠ smart zone. Treat the extra context as recall, not reasoning headroom. |
| `claude-opus-4.7-high` | Opus 4.7 with **high reasoning effort**. Use for the **automated reviewer pass** in `ralph-afk.sh`, gnarly debugging iterations, or issues with non-trivial architecture decisions where you'd rather burn tokens than ship a wrong design. |
| `claude-opus-4.7-xhigh` | Opus 4.7 with **extra-high reasoning effort** (current default in both scripts). Pull this out for the hardest iterations — first vertical slice of a feature, schema/migration design, deep-module redesigns, or when an earlier iteration produced subtly wrong output and you want maximum think-time on the retry. Slowest and most expensive; use deliberately. |

Rules of thumb:
- **Implementer ≠ reviewer.** If you can afford it, run the implementer on `claude-opus-4.7` and the reviewer on `claude-opus-4.7-high` so the reviewer is structurally smarter than the code it's reviewing.
- **Match model to context size.** Don't reach for `-1m-internal` unless the prompt actually requires it; bigger context = slower response and more dumb-zone risk.
- **Escalate, don't camp.** Start `ralph-once.sh` runs on `gpt-5.5` or `claude-opus-4.7`; only graduate to `-high` / `-xhigh` for iterations that fail or for genuinely hard issues.
- **Tag the model in the prompt.** When you change models, mention it in the AFK prompt so the agent's self-talk matches its capability ceiling.

### Parallelization

For advanced use after you have the single-loop working: wrap the runner in a parallel DAG executor that picks independent issue groups, sandboxes each into its own git work tree, and merges them back when each iteration is clean. Use this once you trust the single-agent loop. The [SandCastle library](https://github.com/mattpocock/sandcastle) is one reference implementation of this pattern.

---

## Phase 5 — QA and Code Review

**Goal:** Impose your taste and catch what the agent missed.

This is the only phase that's irreducibly human. Don't try to automate it fully — apps without a human QA pass lack taste and often don't work as intended.

### What to review

Start with the tests. If the tests are testing reasonable behavior and they pass, the implementation is probably sound. Then:

1. Load the app, exercise the new flow manually
2. Check the new service's interface — is it a **deep module** (small interface, substantial internals) or did the agent produce a shallow module cluster?
3. Run your test and typecheck commands yourself
4. Eyeball any migrations — does the schema make sense?

### Generating new issues from QA

When you find something wrong, don't fix it inline — **add a new issue to the kanban**. This keeps the loop clean and lets the next AFK cycle pick it up. The kanban is append-only during implementation.

---

## Phase 6 — Codebase Architecture (Ongoing)

**Goal:** Keep the codebase in shape so the agent keeps producing good output.

Unguided agents produce **shallow module** codebases — many small files with tangled dependencies, no clear test boundaries. This is a self-reinforcing trap: shallow modules → weak feedback loops → worse AI output.

**Deep modules** (John Ousterhout, *A Philosophy of Software Design*): small exposed interface, rich internal behavior. Easy to test from the outside. The agent can see the whole flow.

### How to improve it

```bash
# Run in a fresh Copilot CLI session, no open PRD context
> /improve-codebase-architecture
```

The skill scans the repo and identifies:
- Clusters of related shallow modules that could be consolidated
- Modules with zero test coverage (biggest gaps first)
- Opportunities to define a single clean interface around a subsystem

### The gray-box strategy

You don't need to read every line the agent writes inside a deep module. You need to know:
- What the interface is
- How it behaves under the conditions your tests cover

Design the interface, delegate the internals. This is how you stay sane while moving fast and still owning your codebase.

---

## Skill Reference

All skills live in `~/.copilot/skills/` (user-level) once installed. Invoke them with `/skillname` from within a Copilot CLI session.

| Skill | When to use | Human in loop? |
|---|---|---|
| `/grill-me` | Every new feature, starting from a brief | Yes — you are the answerer |
| `/write-prd` | Immediately after grill session | Scan only |
| `/prd-to-issues` | After PRD, to generate kanban | Review slice structure |
| `/tdd` | Inside any implementation iteration (referenced from `ralph/prompt.md`) | No |
| `/improve-codebase-architecture` | Periodically, or before a big feature | Review suggestions |

Skills use **pull** semantics — the agent fetches them when relevant. For the automated reviewer in the AFK loop, coding standards are **pushed** explicitly so the reviewer has full context.

### Installing the skills

The skills ship in `.copilot/skills/` inside this repo for visibility, but they need to live at `~/.copilot/skills/` for the CLI to discover them as `/skillname` commands.

```bash
# Install (or re-install) all skills from this kit at the user level
mkdir -p ~/.copilot/skills
cp -R .copilot/skills/* ~/.copilot/skills/
```

You can also symlink instead of copy if you want edits in this repo to flow back to the user-level install.

---

## Customizing for Your Stack

The runner scripts are intentionally thin. The two places that encode stack assumptions are:

1. **`ralph/prompt.md` — feedback loop commands.** Default is `npm run test` and `npm run typecheck`. Replace with whatever your project uses (`pytest`, `cargo test`, `go test ./...`, `mix test`, etc.) and add lint or build steps if they're load-bearing.
2. **`ralph/prompt.md` — task selection priorities.** The default order (critical bugfixes → dev infra → tracer bullets → polish → refactors) is sensible for most projects. Tune if your team works differently.

Beyond that, the scripts only assume:
- `git` is available and the repo has at least one commit
- `copilot` CLI is on your `PATH`
- For `ralph-afk.sh`: `docker sandbox run copilot ...` is configured
- For `ralph-afk.sh`: `jq` is on your `PATH` (used for streaming + sentinel detection)

---

## AGENTS.md Tips

If you use an `AGENTS.md` in your project root, keep it minimal. The Copilot CLI reads it on every session. A reasonable starting config:

```markdown
When talking to me, sacrifice grammar for the sake of concision.
```

Don't stuff it with 250k tokens of context — you'll start every session already in the dumb zone.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Agent produces shallow module clusters | No architecture guidance in PRD | Add explicit module map to PRD; run `/improve-codebase-architecture` |
| Tests are passing but implementation is wrong | Agent wrote tests after implementation (cheated) | Enforce TDD in the ralph prompt; check red step actually ran |
| AFK loop produces increasingly bad output | Session drifted into dumb zone | Don't compact — clear and restart with a fresh session |
| Agent keeps re-exploring unnecessarily | Sub-agent results not summarized back cleanly | Check `AGENTS.md` config; ensure sub-agents are set up for summary-only return |
| Type errors on every commit | Schema migration ran but app tables not updated | Run your project's migrate command before running the app |
| PRD doc rot influencing bad agent decisions | Old PRD left in `prds/` after feature shipped | Mark as closed/archive; don't let stale docs accumulate |
| `ralph-afk.sh` exits immediately with "No issues found" | `issues/` doesn't exist or all issues are under `done/` | Run `/prd-to-issues` first, or pass an explicit `<prd-folder>` that has open issues |

---

## Reading List

These books verbalized AI-compatible software practices in English long before AI existed. Worth reading as prompt-engineering goldmines:

- *The Pragmatic Programmer* — Dave Thomas & Andy Hunt (tracer bullets, don't repeat yourself, small tasks)
- *Refactoring* — Martin Fowler (keep changes small and reviewable)
- *A Philosophy of Software Design* — John Ousterhout (deep vs. shallow modules)
- *The Design of Design* — Fred Brooks (shared design concept, design tree)

---

## License

MIT. See [LICENSE](./LICENSE).
