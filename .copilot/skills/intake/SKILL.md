---
name: intake
description: Interactively capture raw feature/change requests for a project and write a grill-ready feature-requests markdown file from the bundled template. Use this BEFORE /grill-me or /grill-with-docs — whenever the user wants to collect, jot down, or stage one or more change requests, feature ideas, bug reports, or "things I want done" for a project, especially when they have several requests, messy/half-formed wording, or supporting material like terminal output or screenshots to attach. Reach for this skill any time the user says they want to write up, capture, intake, or gather feature requests to feed into a grilling/planning session.
---

This skill runs a short **intake interview**: it collects raw change requests from the user,
splits and tidies them, attaches any supporting context, and writes a single
per-run feature-requests markdown file that the grill skills consume as their starting brief.

## What this skill is NOT

Capture only. Do **not** walk the design tree, interrogate trade-offs, propose solutions,
or touch `CONTEXT.md` / ADRs — that is exactly what `/grill-me` and `/grill-with-docs` do
next, and duplicating it here would poison their job. Your goal is a faithful, well-organized
record of *what the user wants changed*, with their original phrasing preserved so the grill
skills have real ambiguity to interrogate. When you're tempted to ask "but how should that
work?", stop — that question belongs to the grill phase.

## Output location

Write to a fresh per-run folder so each intake session is a self-contained brief. The folder and
its markdown file are **both** named with the batch's date/time **and** a descriptive theme
`<slug>`, so a human scanning `docs/feature-requests/` sees *when* a batch was captured and *what
it's about* without opening anything:

```
docs/feature-requests/<YYYY-MM-DD-HHMMSS>-<slug>/
├── <slug>.md                ← generated from the template below
└── context/                 ← copied attachments (created only if needed)
```

- **Timestamp:** a sortable UTC-ish stamp that keeps runs in chronological order, e.g.
  `2026-05-29-143022`. Get it with `date -u +%Y-%m-%d-%H%M%S`.
- **Slug:** a short, human-readable name for *what this batch is about* — see [Naming the slug](#naming-the-slug).
- Because the slug reflects the whole batch, finalize it (and create the folder) once you've
  gathered every request — i.e. at the added-context step — not at the start of the interview.
- If that folder already exists (same second *and* same slug), append `-2`, `-3`, … to the folder name.
- If `docs/` doesn't exist, create the full path anyway — don't error.
- Per-run folders (not one growing file) keep each grill session pointed at one coherent batch
  and stop stale and active requests from mixing.

### Naming the slug

The slug is what makes a batch legible at a glance, so name it for the *content*, never generically:

- **One request:** name it after that single request, e.g. `stream-issue-log-to-bottom`.
- **Multiple requests:** name it after the **core / general theme** that ties them together — e.g.
  `copiloop-packaging-and-rebrand` for a batch spanning packaging, distribution, config, and a
  rename. Find the umbrella; don't just concatenate every request. If the requests are genuinely
  unrelated, pick the dominant area.
- Derive it from the **refined request titles**, not the user's verbatim wording.

**Format — lowercase kebab-case (alphanumeric words joined by single hyphens):**

- Lowercase everything; keep only `a-z`, `0-9`, and `-`.
- Replace spaces, underscores, and slashes with `-`; drop all other punctuation.
- Collapse repeated hyphens and trim any leading/trailing hyphen.
- Keep it tight: 2-6 words, 50 characters or fewer. Drop filler words (`the`, `a`, `please`).

Use this exact slug for **both** the folder suffix and the markdown filename, e.g.
`docs/feature-requests/2026-05-29-143022-stream-issue-log-to-bottom/stream-issue-log-to-bottom.md`.

## The template

The output format lives in [`templates/FEATURE-REQUESTS.template.md`](./templates/FEATURE-REQUESTS.template.md).
Read it before writing. Fill every `<PLACEHOLDER>`, drop optional lines/sections when there's
no signal for them, and never leave template scaffolding behind.

## The interview

Ask **one question at a time** and wait for the answer. Keep your own talking short — the user
is here to dump requests, not read essays.

### 1. Open

Briefly say what you'll do, then ask for their first request:

> "I'll capture your change requests into a feature-requests file you can feed into `/grill-me`
> or `/grill-with-docs`. What's the first thing you'd like changed?"

### 2. Process each request as it comes in

For every raw input, do three things, then show your work:

1. **Improve the wording.** Fix grammar, vague pronouns, and shorthand so the request reads
   clearly on its own — without changing the meaning or inventing scope. If you genuinely
   can't tell what they mean (e.g. "idk just make it better"), ask **one** clarifying capture
   question ("Which part should change, and what outcome would make it better?") — that's
   sharpening the capture, not grilling.
2. **Split complex input.** If one message bundles several distinct changes ("add X, also the
   Y page is slow, and rename Z"), break it into separate requests — each should be
   independently grill-able. Don't over-split a single coherent change into fragments.
3. **Preserve the original.** Keep the user's verbatim text for each resulting request; it goes
   in the **Original wording** field of the template.

Then **echo back** what you recorded and invite correction — lightly, no formal gate:

> "Recorded that as 2 requests: (1) … (2) …. Say if any should be merged, reworded, or dropped —
> otherwise we'll keep going."

If the user corrects you, apply it and move on. Track ambiguities you noticed but deliberately
left unresolved — those become the **Open questions for grilling** bullets (this is the most
valuable thing you hand the grill skills).

### 3. Loop

After the first request is recorded, ask whether there are more:

> "Add another request, or are we done?"

Repeat step 2 for each. Keep numbering continuous across the session.

### 4. Final question — added context

Once they're done adding requests, ask the **last** question:

> "Last thing — any other context to include? A file path to terminal output or logs, a
> screenshot, or notes you want to paste. Or say 'none'."

Handle each kind of context:

- **File path** (`.txt`, `.log`, image, etc.): copy it into the run's `context/` folder and link
  it. If two files share a name, prefix to disambiguate. If the path doesn't exist, tell the user
  and ask them to re-share or skip — don't guess.
- **Pasted text:** write it to `context/notes.md` (or a sensibly named file) and link it.
- **Image / screenshot:** copy as-is into `context/` and link it; the grill skills can view it.
- **Large files** (e.g. big terminal dumps): copy the whole file — do **not** inline its contents
  into the markdown. Reference it by path.

Ask whether each context item is **global** (applies across requests) or tied to a **specific
request**. Default to global if unspecified: global items go in `## Added context`; request-specific
items become that request's `**Context:**` link.

## Write the file

Render the template into `docs/feature-requests/<timestamp>-<slug>/<slug>.md`. Set the
generated-on timestamp, fill `## Project context` from what you learned (or infer one line from
the repo and keep it short), and ensure every `**Context:**` link resolves to a file actually
present in `context/`.

## Hand off

Finish with a copy-pasteable next step — and nothing more (don't start grilling):

> "Captured N request(s) → `docs/feature-requests/<timestamp>-<slug>/<slug>.md`.
> Next: run `/grill-me` (greenfield) or `/grill-with-docs` and point it at that file."
