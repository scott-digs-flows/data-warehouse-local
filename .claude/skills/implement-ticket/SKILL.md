---
name: implement-ticket
description: The end-to-end loop for delivering a tracked piece of work — picking up a Jira issue, branching, implementing, verifying, documenting, and closing it out honestly. Use when starting work on a DW issue or when asked to implement a tracked change.
---

# Implementing a ticket

The loop that takes a DW issue from To Do to Done without leaving the board
lying about reality.

## 1. Understand before building

Read the issue in full — description, acceptance criteria, links, comments.
Then read the code it names. **If the ticket describes something that does not
match the repo, stop and resolve that first**; a ticket written against
imagined code is the most common way a day gets wasted.

Check `CLAUDE.md` for the invariants the change must preserve, and the relevant
stack README for gotchas in the area you are touching.

If acceptance criteria are missing or not checkable, get them fixed before
starting. "I'll know it when I see it" is how scope drifts.

## 2. Move it to In Progress

Reflect reality on the board when work actually starts. Resolve the transition
id with `getTransitionsForJiraIssue` — never hardcode it.

## 3. Branch

```bash
git checkout -b feat/DW-12-clickhouse-reads-iceberg
```

`feat/`, `fix/`, or `chore/`, then the issue key, then a short slug. The key in
the branch name is what ties the work back to the board.

## 4. Implement

Match the code you find — the loaders here have a consistent shape and a high
comment standard, and comments explain *why*, especially for workarounds.

Stay inside the ticket. Something adjacent and tempting is a new ticket, not a
bonus. This repo has a documented history of drift into benchmarking and
performance tuning that its data volume cannot support.

Preserve the invariants: pinned types, snake_case at the load boundary,
idempotent loads, `engines.yaml` as the BI-app contract, Iceberg as the only
path into an engine. **If the ticket appears to require breaking one, stop and
raise it** — either the ticket is wrong or the decision needs superseding, and
both are conversations rather than judgement calls.

## 5. Verify — actually run it

Work through the acceptance criteria one at a time and **run the command**.
Capture real output. A traceback you did not run is not a passing test, and
"the code looks correct" is not a result.

For anything touching data, run the checks in the `data-validation` skill. For a
loader change or a new source, get independent verification from
`data-quality-analyst` — the person who wrote the loader is the worst judge of
whether it worked.

If verification fails, that is the finding. Fix it or report it; do not narrow
the criteria to fit what you got.

## 6. Update what the change invalidated

- **Stack README** — if behaviour, commands, or ports changed.
- **`engines.yaml`** — if connection details or table-reference shapes changed.
  Label the issue `contract`; the BI app consumes this file.
- **`CLAUDE.md`** — if the architecture or an invariant changed.
- **`DECISIONS.md`** — if an architectural decision changed or was superseded.
  See the `decision-record` skill.
- **The stack README's gotchas section** — if you lost real time to something
  not written down. That section exists so each failure costs debugging time
  once.

## 7. Commit and open the PR

```
DW-12: ClickHouse serves fact_internet_sales from Iceberg

<what changed and why, briefly>
```

The issue key leads the subject line. The PR body states what changed, the
verification output, and anything deliberately left out.

## 8. Close it out honestly

Transition to Done and comment with what shipped, the actual verification
output, and follow-up issue keys.

**Done means every acceptance criterion demonstrably met.** If part of the work
is incomplete or blocked, the ticket does not move to Done — say what is left,
and either reopen scope or file the follow-up. A board that overstates progress
is worse than no board.

## When the work is bigger than the ticket

If the ticket turns out to be two tickets, stop and split it rather than
quietly delivering half or silently delivering double. Hand it to
`delivery-lead` to restructure, and say which part you are proceeding with.
