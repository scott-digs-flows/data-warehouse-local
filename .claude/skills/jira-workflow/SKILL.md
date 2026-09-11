---
name: jira-workflow
description: Conventions and mechanics for tracking data warehouse work in Jira — project coordinates, issue hierarchy, field IDs, ticket templates, definition of ready and done, and JQL recipes. Use whenever creating, grooming, transitioning, linking, or reporting on issues in the "Data Warehouse" project.
---

# Jira workflow

All work on this repo is tracked in Jira. This skill carries the coordinates and
conventions so nobody has to probe the API to rediscover them.

## Project coordinates

| | |
| --- | --- |
| Site | `scottdigsflows.atlassian.net` |
| `cloudId` | `1a4eaaaa-9409-4e9a-bbfc-2ffadfbbc3af` |
| Project name | Data Warehouse |
| **Project key** | **`DW`** |
| Type | Team-managed (next-gen) software project |
| Browse URL | `https://scottdigsflows.atlassian.net/browse/<KEY>` |

The project was re-keyed from `SCRUM` (Jira's generated default) to `DW` on
2026-09-11, before any real backlog existed. Jira keeps `SCRUM` as an alias, so
old `/browse/SCRUM-N` links still redirect — but **write `DW` everywhere**.
An issue referred to as `SCRUM-3` is today's `DW-3`; the numbering carried over
unchanged.

Every Atlassian tool call needs `cloudId`. Pass the UUID above.

## Issue hierarchy

| Level | Type | Use for |
| --- | --- | --- |
| 1 | **Epic** | A coherent capability spanning several stories. "Iceberg is the single ingestion path." |
| 0 | **Story** | User- or consumer-visible value, finishable in one sitting. "ClickHouse serves `fact_internet_sales` from Iceberg." |
| 0 | **Task** | Engineering work with no direct consumer value. "Remove the off-pipeline CSV→MergeTree loader." |
| −1 | **Subtask** | A step inside a Story or Task. Use sparingly — prefer splitting the parent. |

**There is no Bug issue type in this project.** File defects as **Task** with
the `bug` label.

This is a team-managed project, so **epic membership is the `parent` field** —
there is no legacy "Epic Link" custom field. Set `parent` to the epic key when
creating a Story or Task, and to the parent key when creating a Subtask.

## Workflow states

`To Do` (id `10000`) → `In Progress` (id `10001`) → `In Review` (id `10002`)
→ `QR` (id `10003`) → `Done` (id `10004`)

`In Review` and `QR` are both in Jira's `indeterminate` category. This skill
documented only three states until 2026-09-11, which is how work got planned as
if `Done` came straight after `In Progress`.

In practice every transition in this project is **global** — `Done` is reachable
in one hop from `In Progress`, so the two middle states are conventions, not
gates. Do not infer from that that they can be ignored: globality is a project
setting someone can change, which is exactly why the ids below must still be
resolved per issue.

Transition IDs are not stable across issue types. Call
`getTransitionsForJiraIssue` for the issue, then `transitionJiraIssue` with the
id it returns. Never hardcode a transition id.

## Writing the ticket

Templates for each issue type are in
[`reference/templates.md`](reference/templates.md). Field IDs — story points,
sprint, start date, flagged — are in
[`reference/fields.md`](reference/fields.md), along with the JQL recipes.

The rules that matter more than the template:

**Name the outcome, not the activity.** "ClickHouse reads `dim_customer` from
Iceberg" beats "Update ClickHouse config".

**Acceptance criteria must be checkable.** Prefer a command and its expected
output. Compare:

> ✅ `uv run python scripts/smoke_test.py --engine clickhouse` exits 0 and
> reports 60,398 rows for `fact_internet_sales`.
>
> ❌ ClickHouse works correctly with Iceberg.

**Read the repo before describing it.** Cite real paths. A ticket referencing a
script that does not exist wastes the implementer's first ten minutes.

**State what is out of scope** when the work sits next to something tempting.
This repo has a documented history of drifting into benchmarking and
performance tuning that its ~1M-row dataset cannot support.

## Definition of ready

A ticket is ready to be picked up when:

- The outcome is stated and the acceptance criteria are checkable.
- Its place in the hierarchy is set (`parent` for stories and tasks under an epic).
- Dependencies are linked (`createIssueLink`, typically *blocks* / *is blocked by*).
- It does not require breaking a documented invariant in `CLAUDE.md`. If it
  appears to, resolve that first — the ticket is wrong, or the invariant needs a
  `DECISIONS.md` entry superseding it.

## Definition of done

- Every acceptance criterion demonstrably met, with the command output to show it.
- Loads are idempotent — re-running the loader is safe.
- Verification passed (see the `data-validation` skill).
- Docs updated where behaviour changed: stack README, `engines.yaml`, `CLAUDE.md`.
- `DECISIONS.md` updated if an architectural decision changed — see the
  `decision-record` skill.
- The issue transitioned to Done, with a closing comment naming what shipped.

## Housekeeping

`DW-1` … `DW-4` are Jira's auto-generated sample issues ("Task 1",
"Task 2", "Task 3", "Subtask 2.1") with empty descriptions. They are not real
work. Delete or repurpose them before building the real backlog; do not let
them dilute status reporting.

## Keeping the board honest

- Move an issue to `In Progress` when work actually starts, not when it is
  planned.
- If reality diverges from the ticket, update the ticket — a stale description
  is worse than none.
- **A stale claim and a stale instruction are corrected differently.** When work
  disproves something a ticket said, what you do depends on whether a reader can
  *act* on it:

  | The ticket said | What to do |
  | --- | --- |
  | A **claim** — "this type has no ClickHouse equivalent", "attachment can fail silently" | Leave it as written; put the correction in the closing comment. The record that the question was asked and tested is worth more than tidiness. |
  | An **instruction** — "connect with driver X", "run script Y first" | Mark it **at the point of use** with a dated `SUPERSEDED` note, then leave the original text intact below it. |

  The difference is where the cost lands. A wrong claim costs a reader nothing
  until they check it. A wrong instruction is acted on *before* they ever reach
  your correction — so the marker has to sit where their eye lands, not at the
  bottom of the comment thread. Never delete the superseded text: the reasoning
  that led somewhere wrong is exactly what stops it being re-derived.

  Same test applies to the repo. A disproved claim in a README gets corrected in
  place; a disproved *command* gets fixed immediately, because someone will paste
  it.
- Comment on the issue when a decision is made or an approach changes, so the
  reasoning survives outside the transcript.
- Reference the issue key in commit messages and PR titles: `DW-12: ...`.
