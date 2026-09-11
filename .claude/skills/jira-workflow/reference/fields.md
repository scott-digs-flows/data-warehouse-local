# Field IDs and JQL recipes

Project `DW` on cloudId `1a4eaaaa-9409-4e9a-bbfc-2ffadfbbc3af`.

## Issue type IDs

| Type | ID | Hierarchy level |
| --- | --- | --- |
| Epic | `10001` | 1 |
| Story | `10004` | 0 |
| Task | `10003` | 0 |
| Subtask | `10002` | −1 |

There is no Bug type. Defects are Tasks labelled `bug`.

## Status IDs

| Status | ID | Category |
| --- | --- | --- |
| To Do | `10000` | new |
| In Progress | `10001` | indeterminate |
| Done | — | done |

Transition IDs differ from status IDs and vary by issue type. Always resolve
them with `getTransitionsForJiraIssue` before transitioning.

## Custom fields

Pass these through `additional_fields` on `createJiraIssue`, or `fields` on
`editJiraIssue`.

| Field | ID | Type | Notes |
| --- | --- | --- | --- |
| Story point estimate | `customfield_10016` | number | Team-managed projects use this, **not** `customfield_10026` |
| Sprint | `customfield_10020` | array | |
| Start date | `customfield_10015` | date | |
| Flagged | `customfield_10021` | array of option | Only allowed value is `Impediment` (id `10019`) |
| Rank | `customfield_10019` | lexorank | Managed by the board; do not set by hand |
| Team | `customfield_10001` | team | Unused on this project |

System fields available: `summary`, `description`, `parent`, `priority`,
`labels`, `assignee`, `reporter`, `duedate`, `issuelinks`, `attachment`.

**Priority** defaults to Medium. Allowed: Highest `1`, High `2`, Medium `3`,
Low `4`, Lowest `5`.

**Parent** is how epic membership works here (team-managed project). Set
`parent` to the epic key on a Story or Task; there is no Epic Link field.

## People

| | |
| --- | --- |
| Scott Digiambattista | `712020:edabda98-f307-4ebf-b679-cd7e4be9351c` |

Resolve anyone else with `lookupJiraAccountId`.

## Labels

Keep the vocabulary small and meaningful:

| Label | Meaning |
| --- | --- |
| `bug` | A defect (there is no Bug issue type) |
| `iceberg` | Touches the lake, catalog, or object store |
| `clickhouse` | Touches the ClickHouse engine layer |
| `ingestion` | Raw file → Iceberg path |
| `contract` | Changes `engines.yaml` — breaking for the BI app |
| `infra` | Compose, ports, volumes, environment |
| `docs` | Documentation only |
| `tech-debt` | Cleanup of superseded structure |

## JQL recipes

```jql
-- The live backlog, highest priority first
project = DW AND statusCategory != Done ORDER BY priority DESC, Rank ASC

-- What is in flight
project = DW AND status = "In Progress" ORDER BY updated DESC

-- Everything under one epic
project = DW AND parent = DW-10 ORDER BY Rank ASC

-- Epics and their state
project = DW AND issuetype = Epic ORDER BY created ASC

-- Ready to pick up: unassigned, not blocked, not started
project = DW AND status = "To Do" AND assignee IS EMPTY ORDER BY Rank ASC

-- Blocked work
project = DW AND issueLinkType = "is blocked by" AND statusCategory != Done

-- Flagged as impediments
project = DW AND Flagged = Impediment

-- Defects
project = DW AND labels = bug AND statusCategory != Done

-- Contract changes the BI app must know about
project = DW AND labels = contract ORDER BY updated DESC

-- Recently completed, for a status report
project = DW AND statusCategory = Done AND updated >= -14d ORDER BY updated DESC
```

## Tool notes

- `searchJiraIssuesUsingJql` returns very large payloads. Pass an explicit
  `fields` list (e.g. `["summary","status","issuetype","parent","labels"]`) to
  keep responses manageable, and prefer `responseContentFormat: "markdown"`.
- `createJiraIssue` takes `description` as Markdown by default
  (`contentFormat: "markdown"`). Use it — hand-writing ADF JSON is unnecessary.
- Issue links are created after the fact with `createIssueLink`, not as a field
  on create. Check available types with `getIssueLinkTypes`.
