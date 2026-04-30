---
name: prd-to-issues
description: Break a PRD into independently-workable issues and write each as a local markdown file in issues/. Use when the user wants to turn a PRD into a list of concrete tasks.
---

# PRD to Issues

Break a PRD into independently-grabbable issues using vertical slices (tracer bullets), written as local markdown files.

## Process

### 1. Locate the PRD

Ask the user for the PRD file path (e.g. `prds/2026-04-29-per-image-object-quantities-prd.md`). PRDs live directly in `prds/` (no per-feature subfolder).

If the PRD is not already in your context window, read it from the file.

Compute the **core name** from the PRD filename: strip the extension, strip any leading ISO date prefix (`YYYY-MM-DD-`), and strip the trailing `-prd` suffix. Example: `2026-04-29-per-image-object-quantities-prd.md` → `per-image-object-quantities`. The core name will be used to group issues under `issues/<core-name>/` in step 5.

### 2. Explore the codebase (optional)

If you have not already explored the codebase, do so to understand the current state of the code.

### 3. Draft vertical slices

Break the PRD into **tracer bullet** issues. Each issue is a thin vertical slice that cuts through ALL integration layers end-to-end, NOT a horizontal slice of one layer.

Slices may be 'HITL' or 'AFK'. HITL slices require human interaction, such as an architectural decision or a design review. AFK slices can be implemented and merged without human interaction. Prefer AFK over HITL where possible.

<vertical-slice-rules>
- Each slice delivers a narrow but COMPLETE path through every layer (schema, API, UI, tests)
- A completed slice is demoable or verifiable on its own
- Prefer many thin slices over few thick ones
</vertical-slice-rules>

### 4. Quiz the user

Present the proposed breakdown as a numbered list. For each slice, show:

- **Title**: short descriptive name
- **Type**: HITL / AFK
- **Blocked by**: which other slices (if any) must complete first
- **User stories covered**: which user stories from the PRD this addresses

Ask the user:

- Does the granularity feel right? (too coarse / too fine)
- Are the dependency relationships correct?
- Should any slices be merged or split further?
- Are the correct slices marked as HITL and AFK?

Iterate until the user approves the breakdown.

### 5. Create the issue files

For each approved slice, write a markdown file inside a per-PRD subfolder under `issues/`.

<output-path-rules>
The issues subfolder uses the PRD's **core name** (computed in step 1):

- **PRD file**:      `<project-root>/prds/<prd-filename>.md`
- **Issues folder**: `<project-root>/issues/<core-name>/`

Create `issues/` and `issues/<core-name>/` if they do not already exist. `<project-root>` is the repository root containing the PRD (NOT the PRD's own directory).

Example:
- PRD:           `/Users/.../visionary-lab/prds/2026-04-29-per-image-object-quantities-prd.md`
- Core name:     `per-image-object-quantities`
- Issues folder: `/Users/.../visionary-lab/issues/per-image-object-quantities/`
- Issue file:    `/Users/.../visionary-lab/issues/per-image-object-quantities/001-add-quantity-input.md`
</output-path-rules>

Within that subfolder, name files using the pattern `NNN-short-title.md` (e.g. `001-add-user-auth.md`). Numbering starts at `001` per PRD folder — do NOT carry numbering across PRDs. Check the target subfolder for any existing files and continue from the next available number if it isn't empty.

Create files in dependency order (blockers first) so you can reference real filenames in the "Blocked by" field. Cross-references between issues in the same PRD use the bare filename (e.g. `001-add-user-auth.md`); the parent PRD reference uses its path relative to the project root.

Do NOT use `gh issue create` or any GitHub CLI commands. Do NOT reference GitHub issue numbers. Use local filenames for all cross-references.

<issue-template>
## Parent PRD

`prds/<prd-filename>.md`

## What to build

A concise description of this vertical slice. Describe the end-to-end behavior, not layer-by-layer implementation. Reference specific sections of the parent PRD rather than duplicating content.

## Acceptance criteria

- [ ] Criterion 1
- [ ] Criterion 2
- [ ] Criterion 3

## Blocked by

- Blocked by `NNN-title.md` (sibling file in the same issues subfolder, if any)

Or "None - can start immediately" if no blockers.

## User stories addressed

Reference by number from the parent PRD:

- User story 3
- User story 7

</issue-template>

Do NOT close or modify the parent PRD file.
