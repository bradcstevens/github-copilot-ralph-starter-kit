# GitHub Copilot Ralph Starter Kit

A starter kit for running an **AFK (away-from-keyboard) AI coding loop** on top of the **GitHub Copilot CLI**. Drop the templates, skills, and runner scripts into a new repo, fill in `AGENTS.md`, point the loop at a kanban of triaged GitHub Issues, and let an agent implement them autonomously while you do something else.

> Inspired by the [AI Engineer Workshop 2026](https://github.com/mattpocock/ai-engineer-workshop-2026-project) workflow, ported to the GitHub Copilot CLI.

**What you get:** two interchangeable AFK runners (pure-bash and Python SDK), per-repo configuration templates under [`templates/`](templates/), and a vendored copy of every Copilot CLI skill the workflow routes to under [`.copilot/skills/`](.copilot/skills). Stack-agnostic — customize one **Feedback loops** table and the rest of the kit follows.

This README is the **quickstart**. The deeper docs live under [`docs/`](docs/) — see [Where to go next](#where-to-go-next).

> **Skills setup starts with `/setup-agent-skills`.** Once you've cloned this kit into your new project and installed the skills at the user level, the **first** thing to run in Copilot CLI is the [`/setup-agent-skills`](.copilot/skills/setup-agent-skills/SKILL.md) skill. It populates the `## Agent skills` block in your `AGENTS.md` and writes the per-repo `docs/agents/{issue-tracker,triage-labels,domain}.md` files that every downstream skill (`/to-issues`, `/triage`, `/to-prd`, `/diagnose`, `/tdd`, `/improve-codebase-architecture`, `/zoom-out`) reads. Skip this step and those skills will guess at your issue tracker, label vocabulary, and context layout. **Safety net:** if you forget, the bootstrap directive at the top of [`AGENTS.md`](templates/AGENTS.template.md) auto-invokes the skill on your next interactive `copilot` session, and both AFK runners ([`ralph/sh-afk.sh`](ralph/sh-afk.sh), [`ralph/python/`](ralph/python/)) refuse to start without it. Full detail in [`docs/customization.md`](docs/customization.md#setup-agent-skills--the-entry-point-skill) and [`docs/customization.md` → Auto-bootstrap behavior](docs/customization.md#auto-bootstrap-behavior).

---

## Prerequisites

**Shared by both runners:**

- [GitHub Copilot CLI](https://docs.github.com/copilot/github-copilot-in-the-cli) installed and signed in: `npm install -g @github/copilot` then run `copilot` once to authenticate.
- [`gh`](https://cli.github.com/) on PATH and signed in (`gh auth login`).
- `git` on PATH.
- A GitHub repository for your project (the loop's default issue source).

**Bash runner** ([`ralph/sh-afk.sh`](ralph/sh-afk.sh)) additionally needs [`jq`](https://jqlang.org/) on PATH.

**Python runner** ([`ralph/python/`](ralph/python/)) additionally needs Python **≥ 3.11** and [`uv`](https://docs.astral.sh/uv/) (or `pip` ≥ 24). See [`docs/runners.md`](docs/runners.md) for the runner comparison and [`ralph/python/README.md`](ralph/python/README.md) for the Python bootstrap.

---

## Quick Start

The kit is designed to be **dropped into a new repo as scaffolding**. The steps below take you from `git clone` to running `/grill-me` against a real brief.

```bash
# 1. Clone the kit into a new project directory and reset git history.
git clone https://github.com/bradcstevens/github-copilot-ralph-starter-kit my-project
cd my-project
rm -rf .git
git init && git add -A && git commit -m "Initial commit from github-copilot-ralph-starter-kit"

# 2. Scaffold AGENTS.md and SPEC.md from the templates in templates/.
#    (CONTEXT.md is already at the repo root as a stub; /grill-with-docs will
#    extend it lazily as new terms come up.)
cp templates/AGENTS.template.md AGENTS.md
cp templates/SPEC.template.md   SPEC.md

# 3. Install the vendored skills at the user level so /skillname works in any
#    Copilot CLI session. (This is a plain copy — it does NOT configure the
#    repo; that's step 4.)
mkdir -p ~/.copilot/skills
cp -R .copilot/skills/* ~/.copilot/skills/

# 4. Start Copilot CLI from the project root and run /setup-agent-skills FIRST.
#    This is the entry point for skill configuration in a new repo. It:
#      - populates the `## Agent skills` block at the bottom of AGENTS.md, and
#      - writes docs/agents/{issue-tracker,triage-labels,domain}.md — the per-repo
#        config files every other skill reads to learn which issue tracker, label
#        vocabulary, and context layout this project uses.
#    Skip it and /to-issues, /triage, /to-prd, /diagnose, /tdd, and
#    /improve-codebase-architecture will guess at the wrong defaults.
copilot
> /setup-agent-skills

# 5. With agent-skill config in place, fill in the rest of AGENTS.md and SPEC.md.
#    Each template has a "How to use this template" header — grep for `<[A-Z_]`
#    to find every placeholder that still needs replacing.
$EDITOR AGENTS.md      # project description, tech stack, feedback loops
$EDITOR SPEC.md        # problem statement, user stories, implementation decisions
$EDITOR ralph/PROMPT.md  # loop-specific routing rules (usually leave defaults)

# 6. Walk through the human-in-the-loop workflow.
copilot
> /grill-me            # greenfield: start here, until 3–4 terms keep recurring
# Once vocabulary stabilises, switch to /grill-with-docs to compile it into
# CONTEXT.md + docs/adr/. See docs/workflow.md for the deciding axis.
# ...then /to-prd, /to-issues, /triage, then kick off the AFK loop.
```

You don't need to use every phase. The skills are independent — pick what helps. The full workflow is documented in [`docs/workflow.md`](docs/workflow.md).

### What the two skills setup steps actually do

These two steps are easy to conflate; they're not the same thing.

| Step | Command | What it changes |
| --- | --- | --- |
| **Install skills at user level** | `cp -R .copilot/skills/* ~/.copilot/skills/` | Makes `/grill-me`, `/to-prd`, `/to-issues`, `/triage`, `/diagnose`, `/tdd`, `/improve-codebase-architecture`, `/zoom-out`, `/find-skills`, `/setup-agent-skills`, etc. discoverable in **any** Copilot CLI session on your machine. Run once per machine (or per kit upgrade). |
| **Configure skills for this repo** | `/setup-agent-skills` (inside `copilot`) | Edits **this repo's** `AGENTS.md` `## Agent skills` block and writes **this repo's** `docs/agents/*.md`. Tells the other skills which issue tracker (GitHub / GitLab / local markdown / other), which label vocabulary, and which context layout (single vs multi-context) this project uses. Run once per repo. |

The first is a one-time machine-level install. The second is a one-time per-project configuration that **must** run before any of the other planning/implementation skills.

---

## Where to go next

The README stops here. Pick whichever doc matches what you need to do:

| Doc | Read when… |
| --- | --- |
| [`docs/concepts.md`](docs/concepts.md) | You want to understand **why** the workflow is shaped the way it is — the Smart Zone / Memento Model mental models the rest of the kit is built around. Read this first if you're unfamiliar with AFK-style AI coding loops. |
| [`docs/workflow.md`](docs/workflow.md) | You're ready to walk the **end-to-end workflow** (Idea → Grill → Brief → PRD → Issues → Triage → AFK loop → QA). Includes the [`/grill-me` vs `/grill-with-docs`](docs/workflow.md#grill-me-vs-grill-with-docs--pick-the-right-one) decision tree and the greenfield-project edge case. |
| [`docs/runners.md`](docs/runners.md) | You're ready to kick off the AFK loop and need the **runner comparison**, invocation cookbook, per-iteration flow, exit conditions, commit-message contract, and skill-routing rules. |
| [`docs/customization.md`](docs/customization.md) | You need to **tailor the kit to your project** — repo structure, what to edit in `AGENTS.md` and `PROMPT.md`, what `/setup-agent-skills` actually writes, re-running it, and the skills reference. |
| [`ralph/python/README.md`](ralph/python/README.md) | You picked the Python runner and want the **bootstrap, env-var surface, observability artefacts, and OpenTelemetry tracing** for it. |

A recommended reading order for first-time users: [`docs/concepts.md`](docs/concepts.md) → finish the Quick Start above → [`docs/workflow.md`](docs/workflow.md) → [`docs/runners.md`](docs/runners.md) → [`docs/customization.md`](docs/customization.md) on demand.

---

## License

MIT — see [`LICENSE`](LICENSE).
