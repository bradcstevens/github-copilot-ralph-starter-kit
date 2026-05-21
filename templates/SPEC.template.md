# `<PROJECT_NAME>`

> **Template notice.** This file is a starting point produced by (or for use with) the `/to-prd` skill. Replace every `` `<PLACEHOLDER>` `` with real content, address every `> 📝` note, delete every section marked **DELETE IF NOT APPLICABLE** you don't need, then delete this notice. Once filled in, this brief is the canonical source for the project's domain language, scope, and decisions — anchor `AGENTS.md`, the PRD issue, and slice issues back to it.

## How to use this template

1. Replace every backticked placeholder like `` `<PROJECT_NAME>` `` with real content. Grep for `<[A-Z_]` to find them.
2. Read every `> 📝` blockquote note and act on it, then delete the note.
3. Delete any section whose first line is `> 🗑️ DELETE IF NOT APPLICABLE` that you don't need.
4. Save as `SPEC.md` (drop the `.template.` segment).
5. When the brief is final, run `/to-prd` to publish it to your issue tracker as the parent PRD, then `/to-issues` to break it into AFK-ready slice issues.
6. Delete this whole **How to use this template** section.

The section structure below matches the `/to-prd` output template so the round-trip from this file → published PRD issue → slice issues is mechanical.

---

## Problem Statement

> 📝 The problem from the user's perspective. Two short paragraphs, no implementation talk. Name the actor, what they're trying to do, why it's hard today, and what's at stake when it stays broken. The reader should finish this section and immediately understand the brief that follows.

`<TWO_PARAGRAPHS_DESCRIBING_THE_USER_PROBLEM_AND_ITS_COST>`

## Solution

> 📝 The solution from the user's perspective, not the architect's. One paragraph. What the user does in the new world, what they no longer have to do, and the one or two decisions the design forces (e.g. who can access, what state lives where). Save the modules / schemas / hosting for **Implementation Decisions**.

`<ONE_PARAGRAPH_DESCRIBING_THE_USER_FACING_SOLUTION>`

## User Stories

> 📝 A LONG, numbered list — exhaustive coverage of every actor × capability pair, not a top-ten. Each story uses the format `As a <ACTOR>, I want <CAPABILITY> so that <BENEFIT>`. Group implicitly by actor to make scanning easier. The slice-generation step (`/to-issues`) maps each slice back to the stories it covers, so missing stories show up as missing slices.

1. As `<ACTOR_1>`, I want `<CAPABILITY>` so that `<BENEFIT>`.
2. As `<ACTOR_1>`, I want `<ANOTHER_CAPABILITY>` so that `<BENEFIT>`.
3. As `<ACTOR_2>`, I want `<CAPABILITY>` so that `<BENEFIT>`.
4. `<...continue exhaustively...>`

## Implementation Decisions

> 📝 The decisions an agent or developer would otherwise have to invent. Be specific about anything reversible-but-load-bearing (data shapes, aggregation rules, role boundaries) and explicit about anything irreversible (auth model, persistence engine, hosting topology). Avoid file paths and code snippets — they go stale fast. Inline a code-shaped snippet only when it encodes a decision more precisely than prose can (a state machine, a reducer, a schema, a type shape), and mark its provenance.

### Domain rules

> 📝 The handful of business / domain rules the rest of the system has to honour. Things like "scoring is sum-of-judges, not average" or "an order can have multiple shipments but at most one refund". Name each rule so slice issues can reference it.

- `<DOMAIN_RULE_1 — short sentence + why it matters>`.
- `<DOMAIN_RULE_2>`.
- `<DOMAIN_RULE_3>`.

### Roles

> 📝 Who can do what. Keep the table small (2–4 roles). If you find yourself listing more, you're probably modelling permissions rather than roles — push those into the **Domain rules** section.

| Role | Permissions |
| --- | --- |
| **`<ROLE_1>`** | `<WHAT_THIS_ROLE_CAN_DO>` |
| **`<ROLE_2>`** | `<WHAT_THIS_ROLE_CAN_DO>` |

### Behavioral defaults

> 📝 The questions that come up early ("edit after submit?", "see peers' state live?") and the chosen default. Mark which are configurable per-tenant / per-event vs hardcoded. Defaults you can revisit later — calling them out here keeps reviewers from re-litigating them.

| Question | Default | Configurable? |
| --- | --- | --- |
| `<QUESTION_1>` | `<DEFAULT_ANSWER>` | `<yes/no>` |
| `<QUESTION_2>` | `<DEFAULT_ANSWER>` | `<yes/no>` |

### Modules to build

> 📝 The system as a small number of **deep modules** (small interface, lots of implementation) plus thin shells. The whole point of this section is to identify the one or two modules worth investing in test-first because they encode the load-bearing logic — typically a pure computation engine or state machine. Everything else is glue. Don't list every helper file; list the units that have to make sense in isolation.

- **`<CORE_PURE_MODULE>` (deep, pure, no I/O):**
  - **Input:** `<INPUT_SHAPE>`.
  - **Output:** `<OUTPUT_SHAPE>`.
  - **Responsibilities:** `<WHAT_IT_COMPUTES_OR_DECIDES>`. No persistence, no auth, no UI concerns. Exhaustively unit-testable.
- **Persistence layer:** stores `<KEY_ENTITIES>`. Small interface: `<SUMMARISE_THE_HALF-DOZEN_OPERATIONS>`. Implementation can be `<RECOMMENDED_OPTIONS>`.
- **Auth layer:** `<IDENTITY_MODEL>` (OIDC / SAML / magic link / password / etc.). Role mapping: `<HOW_ROLES_ARE_DERIVED_FROM_AUTH>`.
- **`<USER_FACING_SURFACE_1>` UI:** `<ONE_LINE_DESCRIPTION_OF_THE_PRIMARY_USER_JOURNEY>`.
- **`<USER_FACING_SURFACE_2>` UI:** `<ONE_LINE_DESCRIPTION>`.
- **Configuration UI / admin surface:** `<WHAT_AN_OPERATOR_CAN_CHANGE_WITHOUT_A_REDEPLOY>`.

### Schema (logical)

> 📝 The smallest type-level sketch that captures the domain. Logical types, not SQL DDL. Field names should match the **Domain rules** vocabulary. If a real ORM / migration tool will own the physical schema, this section is just a forcing function for naming and relationships.

- `<Entity1> { id, <fields>, <relationships> }`
- `<Entity2> { id, <eventOrTenantId>, <fields>, <relationships> }`
- `<Entity3> { id, <fields>, <state_or_status_enum> }`
- `<...>`

### Hosting and deployment

> 📝 The deployment topology in two or three bullets. Cloud vs on-prem, the one-or-two-service shape, and any standard tags / policies that must land on every resource. Don't list every Bicep / Terraform module here — that belongs in the scaffold issue.

- `<HOSTING_TOPOLOGY — e.g. cloud-hosted, static frontend + serverless API, multi-region active-active>`.
- `<TENANCY_AND_ACCESS — e.g. public URL with anonymous access at URL level, real authn/authz at API level>`.
- `<TAGS_AND_POLICIES — e.g. organisation-standard tags on every resource; SecurityControl=Ignore for projects on Azure with MCAPS auth-disabling policies>`.
- `<OPS_POSTURE — e.g. fast spin-up for new <UNITS>; teardown after each cycle; long-lived; etc.>`.

> 📝 Capture any genuinely undecided technology choice as an **Open item** _(replace the example with your own, delete this note, or remove the whole line)_ instead of guessing — reviewers anchor on guesses.
>
> **Open item:** `<DESCRIBE_THE_OPEN_DECISION_AND_WHO/WHEN_DECIDES_IT>`.

## Testing Decisions

> 📝 A short philosophy paragraph + a list of what to cover. The point is to commit to testing **observable behaviour** through public interfaces, not internal helpers. Be explicit about which module deserves exhaustive coverage (usually the deep pure module) and which surfaces are intentionally lightly tested.

`<ONE_PARAGRAPH_ON_THE_TESTING_PHILOSOPHY_FOR_THIS_PROJECT>`. We are not testing `<EXPLICITLY_OUT_OF_SCOPE_TESTING>`; we are testing `<WHAT_MATTERS>`.

### Modules to test

- **`<CORE_PURE_MODULE>`** — full unit coverage. Cover:
  - `<BEHAVIOUR_1 — happy path>`.
  - `<BEHAVIOUR_2 — boundary case>`.
  - `<BEHAVIOUR_3 — incomplete or partial input>`.
  - `<BEHAVIOUR_4 — edge case the domain rules force you to handle>`.
  - `<BEHAVIOUR_5 — alternate configuration produces a different observable result>`.
- **Auth layer** — verify that `<AUTH_INVARIANT_1>` and `<AUTH_INVARIANT_2>`.
- **`<STATE_MACHINE_OR_LIFECYCLE_MODULE>`** — verify the legal transitions and the rejection of illegal ones. Verify any cross-cutting side-effect (e.g. peer-visibility flip on lock) fires only for the affected entity.

### Out of scope for testing

> 📝 What you're explicitly NOT testing, with a one-line reason for each. Surfaces this up so reviewers can push back if they disagree, rather than discover it post-merge.

- `<TEST_SURFACE_NOT_COVERED — reason>`.
- `<TEST_SURFACE_NOT_COVERED — reason>`.

### Prior art

> 📝 Existing tests in the repo that the new tests should learn from / mirror. If the codebase is greenfield, say so — that itself is a signal that the deep module deserves test-first treatment.

`<EITHER: pointer to similar tests in the codebase, OR: "None — greenfield. The <DEEP_MODULE> is small enough to write test-first.">`

## Out of Scope

> 📝 What this project is **deliberately not doing**. Each bullet is a feature someone will eventually ask for; the list pre-empts those asks and protects scope. Save these items in `.out-of-scope/` if your triage process expects that.

- `<FEATURE_NOT_BUILT — one-line reason>`.
- `<FEATURE_NOT_BUILT — one-line reason>`.
- `<FEATURE_NOT_BUILT — one-line reason>`.

## Further Notes

> 📝 Anything that doesn't fit cleanly above but a future implementer needs to know: sensitivity / compliance posture, privacy quirks, failure-mode fallbacks, post-lifecycle handling, reusability constraints. Keep this section short — if a topic grows to more than 2–3 bullets, promote it to its own section.

- **Sensitivity:** `<DATA_CLASSIFICATION_POSTURE — e.g. "no regulated data; standard internal posture is fine">`.
- **Privacy:** `<PRIVACY_RULE_OR_INVARIANT_WORTH_CALLING_OUT>`.
- **Failure mode:** `<WHAT_USERS_DO_WHEN_THE_APP_IS_DOWN>`. Keep a `<SURVIVAL_VIEW_OR_FALLBACK>` available.
- **Reusability:** `<WHAT_THE_SAME_DEPLOYMENT_MUST_SUPPORT_WITHOUT_A_REDEPLOY>`.
- **Post-cycle lifecycle:** `<EXPORT / ARCHIVE / TEARDOWN_STEPS>`.
