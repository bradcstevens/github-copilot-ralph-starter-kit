# ISSUES

Local issue files from `issues/` are provided at start of context. Issues are grouped per PRD into subfolders named after that PRD's core name, e.g. `issues/<core-name>/NNN-short-title.md`. The core name is derived from the PRD filename in `prds/` by stripping the date prefix (`YYYY-MM-DD-`) and the trailing `-prd` suffix. Files under any `done/` subfolder are excluded — those are archived. Parse the provided content to understand the open issues and which PRD each one belongs to.

Each issue references its `Parent PRD` by a path relative to the project root (e.g. `prds/<YYYY-MM-DD>-<core-name>-prd.md`). PRDs live directly in `prds/` — there is no per-feature subfolder. Read the parent PRD when you need broader context, design decisions, or user stories.

You will work on the AFK issues only, not the HITL ones.

You've also been passed a file containing the last few commits. Review these to understand what work has been done.

If all AFK tasks are complete, output <promise>NO MORE TASKS</promise>.

# TASK SELECTION

Pick the next task. Prioritize tasks in this order:

1. Critical bugfixes
2. Development infrastructure

Getting development infrastructure like tests and types and dev scripts ready is an important precursor to building features.

3. Tracer bullets for new features

Tracer bullets are small slices of functionality that go through all layers of the system, allowing you to test and validate your approach early. This helps in identifying potential issues and ensures that the overall architecture is sound before investing significant time in development.

TL;DR - build a tiny, end-to-end slice of the feature first, then expand it out.

4. Polish and quick wins
5. Refactors

# EXPLORATION

Explore the repo.

# IMPLEMENTATION

Use /tdd to complete the task.

# FEEDBACK LOOPS

Before committing, run the feedback loops:

- `npm run test` to run the tests
- `npm run typecheck` to run the type checker

# COMMIT

Make a git commit. The commit message must:

1. Include key decisions made
2. Include files changed
3. Blockers or notes for next iteration

# THE ISSUE

If the task is complete, move the issue file to a `done/` subfolder **inside the same per-PRD issues folder** (e.g. `issues/<core-name>/001-foo.md` → `issues/<core-name>/done/001-foo.md`). Create the `done/` folder if it doesn't exist. Do NOT move issues across PRD folders.

If the task is not complete, add a note to the issue file with what was done.

# FINAL RULES

ONLY WORK ON A SINGLE TASK.