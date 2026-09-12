# <implementation-ready title>

Labels: `project:<id>` and either `plan:<id>` or, for standalone work,
`activation:queued|future`. A standalone bead without an activation label is
queued by default; use `activation:future` only when the human explicitly saves
the work for later. Use the normal Beads type, priority, status, and dependency
fields.

Stable intake key: `<source-specific-id>`

## Problem

<What is wrong or missing, with enough evidence to distinguish the problem.>

## Outcome

<What is observably true when the work is complete.>

## Bounded scope

<Included behavior, explicit exclusions, and constraints.>

## Context

<Plan link, repository guidance, decisions, and relevant evidence references.>

## Dependencies

- `<blocking bead id>` — <why it must finish first>

## Acceptance criteria

- <Concrete behavior or artifact that must exist.>

## Validation

- <Proportionate command, scenario, or retained evidence.>

## Authorized model overrides

None. Replace this only with an explicit human-authorized model and its source.

## Authoring check

Replace prompts with concrete facts. Context names repository documents and
approved plan revision when present. Split work at independently verifiable
outcomes; specify prerequisite IDs and why they block. Acceptance must be
observable by an Executor without the conversation. Keep stable intake keys
through retries and refinements. Model overrides retain explicit human provenance.
