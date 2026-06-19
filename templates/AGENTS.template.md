# Agent guide — `<PROJECT_NAME>`

> **Template notice.** This file is a starting point. Replace every `` `<PLACEHOLDER>` `` with real content, address every `> 📝` note, delete every section marked **DELETE IF NOT APPLICABLE** you don't need, then delete this notice. Once filled in, this file is loaded into every AI agent's context on every invocation — keep it short, scannable, and current.

## How to use this template

1. Replace every backticked placeholder like `` `<PROJECT_NAME>` `` with real content. Grep for `<[A-Z_]` to find them.
2. Read every `> 📝` blockquote note and act on it, then delete the note.
3. Delete any section whose first line is `> 🗑️ DELETE IF NOT APPLICABLE` that you don't need.
4. Run `/setup-agent-skills` to populate the trailing **Agent skills** block. It writes a per-repo `docs/agents/` triplet too. _(If you forget, the **First-run bootstrap** directive in the persistent body below will auto-trigger this skill the next time you start `copilot` from this repo — but doing it manually before any other agent work is cleaner.)_
5. If you're using the autonomous loop, copy `ralph/` into the new repo as well.
6. Delete this whole **How to use this template** section.

Sections below are ordered by what an agent reads first: project context → tech stack → how to verify changes → conventions → optional tooling → autonomous loop → skills config.

---

> 🤖 **First-run bootstrap (read on every invocation).** If `docs/agents/issue-tracker.md` does **NOT** exist at the repo root, your very first action this session is to invoke `/setup-agent-skills` — **before any other work**, including the user's stated request. After it completes, return to whatever the user originally asked. If `docs/agents/issue-tracker.md` already exists, this bootstrap is satisfied; ignore this paragraph and proceed normally. The autonomous AFK loop runner (`ralph/python/`) refuses to start without this file, so if you are reading this directive from inside a `copilot --yolo -p` invocation, surface the inconsistency and stop.

> 📝 Replace this paragraph with one or two sentences describing what the project is, who uses it, and the user-facing goal. Anchor to canonical sources rather than restating them.

`<ONE_OR_TWO_SENTENCE_PROJECT_DESCRIPTION>`. See [`SPEC.md`](SPEC.md) for the full problem statement and the canonical PRD at `<PRD_LOCATION>` (e.g. `docs/PRD.md` or a GitHub issue URL).

> 🗑️ **Repo state** _(delete this whole blockquote once the project is scaffolded)_: pre-scaffold — no application code exists yet. The agent picking up the first implementation slice will scaffold the project from scratch following the **Tech stack** section below.

## Tech stack

> 📝 List the load-bearing technology choices. Anchor each line to a single canonical source (your scaffold issue, an ADR, a `SETUP.md`) so the stack list never silently drifts from reality. Delete rows you don't have; add rows for anything an agent would otherwise have to guess.

- **App:** `<FRAMEWORK + VERSION + KEY MODE>` (e.g. Next.js App Router on TypeScript strict mode).
- **UI:** `<COMPONENT_LIBRARY + STYLING + TYPOGRAPHY>`.
- **State / data:** `<CLIENT_STATE_LIB + VALIDATION_LIB + FORMS_LIB>`.
- **Tests:** `<UNIT_RUNNER>` for units; `<E2E_RUNNER>` for end-to-end.
- **Lint:** `<LINTER + FORMATTER>`. Note any non-default rules (import-isolation, `no-implicit-any`, etc.).
- **Package manager:** `<PACKAGE_MANAGER>` (pnpm / npm / yarn / bun / uv / poetry / …). `<RUNTIME_LTS_VERSION>` pinned via `.nvmrc`, `.tool-versions`, or `pyproject.toml`.
- **Persistence:** `<DB_ENGINE + DEPLOYMENT_MODE>`.
- **Auth:** `<IDENTITY_MODEL>` (OIDC provider, magic links, password, etc.).
- **Infra:** `<IAC_TOOL>` + key resources.
- **CI/CD:** `<CI_PROVIDER>` + key workflows.

## Feedback loops

> 📝 The exact commands agents must run before committing. Be specific — vague verbs like "run the tests" force agents to grep `package.json` and guess. Keep this table in sync with what CI actually runs. Until the scaffold lands these scripts won't exist; either delete the table or annotate it so the first slice knows what to build toward.

| Loop          | Command                              | When to run                                                                              |
| ------------- | ------------------------------------ | ---------------------------------------------------------------------------------------- |
| Lint          | `<PM> lint`                          | Any code change                                                                          |
| Type-check    | `<PM> typecheck`                     | Any typed change                                                                         |
| Unit tests    | `<PM> test:unit`                     | Any code change                                                                          |
| Build         | `<PM> build`                         | Anything touching routes, configs, deps                                                  |
| E2E tests     | `<PM> test:e2e`                      | Anything user-visible; save reports to `tests/e2e/<YYYY-MM-DD-HHMMSS>/`                  |
| Infra what-if | `<IAC_WHAT_IF_COMMAND>`              | Any infra change                                                                         |

Run only the loops relevant to what you changed. PR CI runs `<LIST_OF_REQUIRED_LOOPS>` and blocks merge if any fail.

## Code conventions

> 📝 The handful of rules that bend the codebase away from defaults. Skip anything covered by the linter — only list things an agent could plausibly violate without tripping CI.

- `<DEEP_MODULE_RULE>` (e.g. "Pure logic lives in `src/engine/`; ESLint blocks cross-folder imports. Protect this seam.").
- Use the project's domain vocabulary in module names, test names, issue comments, and commit messages. The canonical glossary is `CONTEXT.md` (created lazily by `/grill-with-docs`). Until it exists, treat the PRD as the glossary.
- Architectural decisions go to `docs/adr/` (created lazily). Don't silently override an existing ADR — surface the contradiction in your PR or issue comment.

## Azure conventions

> 🗑️ DELETE IF NOT APPLICABLE — remove this whole section if the project isn't on Azure.

- Tag every Azure resource and the resource group with `SecurityControl=Ignore` plus your project's identifying tags (e.g. `Project=<slug>`, `Environment=<env>`, `Owner=<handle>`). The `SecurityControl=Ignore` tag exempts the resource from tenant-level MCAPS auth policies that would otherwise force `disableLocalAuth: true` on Cosmos / Cognitive Services.
- For Microsoft Foundry / Azure AI Services accounts, default `disableLocalAuth: false` and use `AzureKeyCredential`-based clients unless the user explicitly asks for managed-identity / keyless auth.
- Use the `microsoft-docs`, `microsoft-foundry`, and `azure-*` skills for authoritative answers before reaching for memory.

## Frontend testing

> 🗑️ DELETE IF NOT APPLICABLE — remove this whole section if the project has no end-to-end UI.

Use `<E2E_RUNNER>` (e.g. Playwright) for end-to-end scenarios. Drive the real UI, assert on rendered DOM and network. Save reports under `tests/e2e/<YYYY-MM-DD-HHMMSS>/` so a failed CI run leaves an inspectable trail.

## Microsoft tooling

> 🗑️ DELETE IF NOT APPLICABLE — remove this whole section if the project isn't on Azure or Microsoft tech.

When working on Azure or Microsoft technology, prefer skill-backed answers over guessing. Useful skills (typically installed in this environment):

- **`microsoft-docs`** — query official Microsoft documentation.
- **`microsoft-foundry`** — Azure AI Foundry resources and SDKs.
- **`azure-*`** — provisioning, deployment, diagnostics (`azure-deploy`, `azure-storage`, `azure-kubernetes`, `azure-rbac`, `azure-cost`, `azure-quotas`, `azure-resource-lookup`, etc.).
- **`entra-app-registration`**, **`entra-agent-id`** — identity and OIDC bootstrap.

## Autonomous loop

> 🗑️ DELETE IF NOT APPLICABLE — remove this whole section if `ralph/` has not been copied into this repo.

The AFK runner lives at [`ralph/python/`](ralph/python/) — an unattended Copilot CLI loop on the GitHub Copilot Python SDK that pulls AFK-ready issues (`ready-for-agent` label, body contains `## Parent` plus `## Acceptance criteria`), feeds each one to a fresh `copilot --yolo -p` invocation against [`ralph/PROMPT.md`](ralph/PROMPT.md), and **terminates cleanly when the AFK-ready pool is empty** or **aborts non-zero after `MAX_NMT_STRIKES` (default 3) consecutive no-progress iterations**. After each iteration the wrapper backstops the agent's `gh issue close` step by walking new commits for `Closes/Fixes/Resolves #N` references restricted to that iteration's AFK-ready pool — if the agent forgot to call `gh issue close`, the wrapper does it. The wrapper also refuses to start a new iteration if the working tree is dirty. It honours the `MODEL` / `ISSUE_SOURCE` / `MAX_NMT_STRIKES` env vars and adds a richer terminal UX (frozen iteration `Panel`s, per-iteration token + estimated-cost signal, `.ralph/logs/*.jsonl` replay log, `.ralph/runs/*.json` per-iteration rollup, opt-in OpenTelemetry tracing). Invoke it with `uv run --project ralph/python ralph-afk` (one-time `uv sync --project ralph/python` bootstrap); [`ralph/afk.sh`](ralph/afk.sh) is an optional one-line convenience launcher that calls it with a default model. See [`ralph/python/README.md`](ralph/python/README.md) for bootstrap, invocation, exit codes, env-var surface, and observability artefact locations. The companion prompt routes work to `/diagnosing-bugs`, `/prototype`, `/tdd`, and `/codebase-design` at the appropriate phase (the plan-stress-test and higher-level-mapping steps are inlined, since those skills are human-only).

**Commit-message contract (when the loop is in use):**

- Completion commits: `Closes #N`, `Fixes #N`, or `Resolves #N` (case-insensitive) — the wrapper's auto-close backstop matches these.
- Partial-progress commits: `Refs #N` or `Progress on #N` so the wrapper does **not** auto-close.

The legacy `<promise>NO MORE TASKS</promise>` sentinel is now informational only — emit it only if you genuinely have nothing actionable after triaging the provided issues; emitting it after doing work is treated as a strike.

Skills the loop knows about live under `~/.copilot/skills/`. To discover more, run `npx skills find <query>` or invoke `/find-skills`.

## Agent skills

> 📝 The three subsections below are owned by `/setup-agent-skills`. Run that skill in a fresh repo to populate them from real choices (GitHub vs GitLab vs local-markdown, label vocabulary, single- vs multi-context). The skill also writes the matching `docs/agents/*.md` triplet that each subsection points to.

### Issue tracker

`<ONE_LINE_SUMMARY_OF_WHERE_ISSUES_LIVE_AND_THE_CLI>`. See `docs/agents/issue-tracker.md`.

### Triage labels

Five canonical labels (`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`) — verbatim. See `docs/agents/triage-labels.md`.

### Domain docs

`<ONE_LINE_SUMMARY_SINGLE_OR_MULTI_CONTEXT_AND_LOCATIONS>`. See `docs/agents/domain.md`.
