# Ticket templates

Markdown, passed straight to `createJiraIssue` with
`contentFormat: "markdown"`.

Templates are a floor, not a ceiling. Drop a section that genuinely does not
apply rather than filling it with "N/A".

---

## Epic

> **Summary:** name the capability, not the project phase.
> ✅ "Iceberg is the single ingestion path for all engines"
> ❌ "Phase 2"

```markdown
## Goal

One paragraph: the capability this delivers and why it matters to the BI app
or to the engine-neutrality thesis.

## Why now

What this unblocks, or what is currently painful without it.

## Scope

- Bullet the areas of the repo this touches.

## Out of scope

- Bullet the adjacent things this deliberately does not cover, with a
  one-clause reason.

## Done when

The observable end state. An epic is done when its stories are done *and*
this statement is true.
```

---

## Story

> **Summary:** the outcome, in the present tense.
> ✅ "ClickHouse serves `fact_internet_sales` from Iceberg"
> ❌ "Wire up ClickHouse"

```markdown
## Context

Why this is needed and what depends on it. Two or three sentences. Cite real
paths — e.g. `stacks/iceberg-multi-engine/scripts/load_iceberg.py`.

## Acceptance criteria

- [ ] A checkable statement, ideally a command and its expected output.
- [ ] `uv run python scripts/smoke_test.py --engine clickhouse` exits 0.
- [ ] Row counts match `shared/data/<Table>.csv`.
- [ ] Re-running the loader is safe (idempotent).

## Notes

Known gotchas, links to the relevant stack README section, prior art in the
repo. Anything that saves the implementer a rediscovery.

## Out of scope

What not to do while in here.
```

---

## Task

Same shape as a Story, minus consumer framing. Use for refactors, cleanup,
infrastructure, and defects.

```markdown
## What

The change, concretely. Name the files.

## Why

The reason it is worth doing now.

## Acceptance criteria

- [ ] Checkable statements.

## Risk

What could break, and what proves it did not. For anything touching
`engines.yaml`, state the impact on the BI app explicitly.
```

---

## Task (defect)

Label `bug`. There is no Bug issue type in this project.

````markdown
## Observed

What actually happened, with the exact command and output.

## Expected

What should have happened, and what says so — pinned schema, source CSV,
another engine's answer.

## Reproduction

```bash
# exact commands
```

## Scope of impact

Which tables, engines, or consumers are affected. Does the BI app see it?

## Acceptance criteria

- [ ] The reproduction above no longer reproduces.
- [ ] A check exists that would catch a regression.
````

---

## Closing comment

When transitioning to Done, comment with:

````markdown
Shipped in `<branch or commit>`.

**What changed:** one or two sentences.

**Verified:**
```
<the actual command output that demonstrates the acceptance criteria>
```

**Follow-ups:** DW-NN, or "none".
````
