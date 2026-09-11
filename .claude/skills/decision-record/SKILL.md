---
name: decision-record
description: Recording architectural decisions in DECISIONS.md — what warrants an entry, how to supersede a previous decision without erasing it, and where findings, non-goals, and open directions belong. Use when a change alters the architecture, reverses a prior decision, rules something out, or produces a finding worth not rediscovering.
---

# Recording decisions

`DECISIONS.md` holds the reasoning that is **not recoverable from the code** —
why the repo is shaped the way it is, and the paths deliberately not taken. The
README covers *what* and *how*; this file covers *why*.

It is load-bearing because this project's value is largely its architectural
position. A decision that is not written down gets silently reversed by the next
plausible-sounding convenience.

## What earns an entry

- **An architectural choice with a live alternative.** Iceberg as the mandatory
  middle layer, ClickHouse as the first engine, pinned types over inference.
- **Reversing or superseding a previous decision.** Always — this is the most
  important case.
- **Ruling something out.** Druid was dropped because it cannot read Iceberg;
  that entry stops the question being reopened every quarter.
- **A finding that would otherwise be rediscovered.** "ClickHouse can read
  Iceberg with no catalog at all" changes how you think about the minimum
  viable lake.
- **A deliberate non-goal.** Benchmarking and performance tuning are out of
  scope here because the dataset cannot support conclusions, and saying so
  prevents recurring drift.

## What does not

- Implementation detail recoverable by reading the code.
- Anything already captured as a stack-README gotcha — environment failure modes
  live there, architectural reasoning lives here.
- Routine dependency bumps and refactors.

## Structure

The file has four sections. Put the entry in the right one:

| Section | Holds |
| --- | --- |
| **Decisions** | A table of choice → rationale. One row, one sentence of reasoning that names the alternative rejected. |
| **Findings worth remembering** | Things learned that change how you'd design it. |
| **Deliberately out of scope** | Non-goals, with the reason they are non-goals. |
| **Open directions** | Considered, not built — with enough detail to resume. |

## Writing the entry

**Give the rationale, not the restatement.** "ClickHouse as default — it is the
default" is worthless. "ClickHouse over StarRocks — far lighter, and StarRocks'
real advantage (materialised views over Iceberg) is invisible at this data
volume" tells the next reader when to revisit.

**Name what you rejected and why.** A decision without its alternative is not a
decision, it is a description.

**Say what would change your mind.** The most useful entries carry their own
expiry condition — "revisit if the dataset grows enough for materialised views
to show a difference".

## Superseding a decision

**Never delete or silently edit a decision that was genuinely made.** The
reasoning that led to it was real, and erasing it means the same argument gets
relitigated from scratch.

Mark the old row as superseded and add the new one with the reason the position
changed:

```markdown
| ~~Iceberg is one strategy, not the foundation~~ *(superseded 2026-09-11)* | Held while the goal was comparing self-contained stacks. Superseded when engine-neutrality became the project's purpose — see below. |
| Iceberg is the mandatory middle layer | Every engine reads from Iceberg; no engine owns the data. Costs a catalog and an object store, and buys engines that are genuinely swappable — which is the point of the project, not an optimisation. |
```

Date the supersession. If it invalidates code or docs, that is a ticket, not a
footnote — file it.

## Keeping it honest

When `DECISIONS.md` and the repo disagree, one of them is a defect. Either the
code drifted from the decision, or the decision changed and was not recorded.
Resolve it rather than leaving the contradiction for the next reader — and
prefer fixing the record immediately, since it is cheap now and expensive later.
