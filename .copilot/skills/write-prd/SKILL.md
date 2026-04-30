---
name: write-prd
description: Generate a PRD from the client brief and write it as a local markdown file in issues/. Use when the user wants to turn a client request into a structured PRD.
---

This skill will be invoked when the user wants to create a PRD. You may skip steps if you don't consider them necessary.

1. Ask the user for a long, detailed description of the problem they want to solve and any potential ideas for solutions.

2. Explore the repo to verify their assertions and understand the current state of the codebase.

3. Interview the user relentlessly about every aspect of this plan until you reach a shared understanding. Walk down each branch of the design tree, resolving dependencies between decisions one-by-one.

4. Sketch out the major modules you will need to build or modify to complete the implementation. Actively look for opportunities to extract deep modules that can be tested in isolation.

A deep module (as opposed to a shallow module) is one which encapsulates a lot of functionality in a simple, testable interface which rarely changes.

Check with the user that these modules match their expectations. Check with the user which modules they want tests written for.

5. Once you have a complete understanding of the problem and solution, use the template below to write the PRD as a local markdown file. Create any required directories if they do not already exist. Do NOT submit a GitHub issue or call any external service.

<output-path-rules>
Determine the PRD output path from the source file the user provided (plan, spec, design, brief, etc.):

- **If a source file was provided**, derive the PRD filename from its basename:
  1. Strip the extension.
  2. Replace any trailing artifact-type suffix (`-design`, `-spec`, `-plan`, `-brief`, `-rfc`, `-proposal`, `-notes`) with `-prd`. If no recognized suffix is present, append `-prd`. This is the **PRD filename** (e.g. `2026-04-29-per-image-object-quantities-prd.md`).
  3. Write the PRD directly to `<project-root>/prds/<prd-filename>.md`, where `<project-root>` is the repository root containing the source file (NOT the source file's own directory). Create `prds/` if it does not already exist. Do NOT create a per-feature subfolder inside `prds/`.

  Example:
  - Source:   `/Users/.../visionary-lab/docs/superpowers/specs/2026-04-29-per-image-object-quantities-design.md`
  - PRD file: `2026-04-29-per-image-object-quantities-prd.md`
  - PRD path: `/Users/.../visionary-lab/prds/2026-04-29-per-image-object-quantities-prd.md`

- **If no source file was provided**, ask the user for a short kebab-case slug describing the feature, then write the PRD to `<project-root>/prds/<YYYY-MM-DD>-<slug>-prd.md` using today's date.

Confirm the final output path with the user before writing the file.
</output-path-rules>

<prd-template>

## Problem Statement

The problem that the user is facing, from the user's perspective.

## Solution

The solution to the problem, from the user's perspective.

## User Stories

A LONG, numbered list of user stories. Each user story should be in the format of:

1. As an <actor>, I want a <feature>, so that <benefit>

<user-story-example>
1. As a mobile bank customer, I want to see balance on my accounts, so that I can make better informed decisions about my spending
</user-story-example>

This list of user stories should be extremely extensive and cover all aspects of the feature.

## Implementation Decisions

A list of implementation decisions that were made. This can include:

- The modules that will be built/modified
- The interfaces of those modules that will be modified
- Technical clarifications from the developer
- Architectural decisions
- Schema changes
- API contracts
- Specific interactions

Do NOT include specific file paths or code snippets. They may end up being outdated very quickly.

## Testing Decisions

A list of testing decisions that were made. Include:

- A description of what makes a good test (only test external behavior, not implementation details)
- Which modules will be tested
- Prior art for the tests (i.e. similar types of tests in the codebase)

## Out of Scope

A description of the things that are out of scope for this PRD.

## Further Notes

Any further notes about the feature.

</prd-template>
