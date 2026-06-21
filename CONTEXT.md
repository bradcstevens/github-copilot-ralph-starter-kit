# GitHub Copilot Ralph Starter Kit

The kit's domain is the **AFK runner**: an unattended loop that drives the GitHub
Copilot CLI to implement triaged GitHub Issues one at a time. This glossary fixes
the vocabulary that the runner, its prompts, and its live interface all share.

## Language

### The run loop

**Run**:
One invocation of the AFK loop, identified by a `run_id`, spanning many iterations
until the work is exhausted or the strike limit is reached.

**Iteration**:
One cycle of the loop — collect the pool, let the agent work exactly one task, then
do commit accounting and a progress check. The unit by which elapsed time and
streamed output are measured and attributed.
_Avoid_: round, pass, tick.

**Pool**:
The set of AFK-ready issues collected at the start of an iteration and offered to
the agent together in a single prompt; the agent picks one.
_Avoid_: batch, backlog.

**Strike**:
A recorded instance of an iteration making no meaningful progress; a fixed number of
strikes ends the run.
_Avoid_: failure, miss.

### Issues and attribution

**Active issue**:
The single issue the agent is working during the current iteration, self-selected
from the pool.
_Avoid_: current task, current ticket.

**Working marker**:
The agent's explicit, up-front declaration of its active issue, used to attribute the
iteration's timing and streamed output to that issue in real time.

**Queue**:
The per-run ledger of every issue seen in any pool during the run, each carrying a
status; the selectable list shown in the live interface. Distinct from the pool,
which is a single iteration's input.
_Avoid_: backlog, list.

**Status**:
An issue's lifecycle within a run: **queued** (seen, not yet worked), **active**
(being worked now), **closed** (finished and closed via a commit close-keyword),
**advanced** (progressed but not closed), **no-progress** (worked without meaningful
change), **gone** (left the pool without resolution).

### Leaving a run

**Stop**:
Ending a run deliberately — the current iteration is wound down cleanly and the loop
exits.
_Avoid_: quit, kill, abort.

**Detach**:
Leaving the live interface while the run keeps going unattended, falling back to the
line-by-line scrollback output.
_Avoid_: background, minimize, exit.

## Relationships

- A **Run** has many **Iterations**.
- An **Iteration** is offered one **Pool** and produces at most one **Active issue**.
- A **Queue** belongs to exactly one **Run** and aggregates every issue seen across
  its **Iterations**, keyed by issue.
- An **Active issue** is the **Pool** member named by the current **Working marker**.

## Example dialogue

> **Dev:** "If the agent works issue #12 across two different iterations, is that one
> queue entry or two?"
> **Domain expert:** "One **Queue** entry — the queue is keyed by issue, and its
> active time sums across every iteration that worked it. Those are two distinct
> **Iterations**, but the same **Active issue**."

## Flagged ambiguities

- `queue` was used to mean both a single iteration's input set and the whole-run list
  of issues — resolved: the per-iteration input is the **Pool**; the whole-run,
  status-bearing list is the **Queue**.
- `current task` / `current issue` was used loosely for whatever the agent was doing —
  resolved: the agent's in-flight selection is the **Active issue**, declared via its
  **Working marker**.
