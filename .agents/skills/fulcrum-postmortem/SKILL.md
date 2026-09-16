---
name: fulcrum-postmortem
description: Reconstruct and write evidence-backed postmortems for a Fulcrum bead, operation, run, incident, or bounded workflow issue. Use when the requested deliverable is a postmortem, incident review, exact workflow timeline, causal analysis, or failure inventory; do not use for an ordinary bug fix or status summary that does not require incident reconstruction.
---

# Fulcrum Postmortem

Produce a report that lets a reader answer four questions without reopening the
incident: what happened, why it happened, why the system did not prevent or expose
it, and what evidence proves the corrective actions.

Before drafting, read [references/report-structure.md](references/report-structure.md).
Also read repository instructions and the relevant Fulcrum architecture or
contract documents. Treat a request to investigate or write a postmortem as
read-only: do not repair workflow state, retry mutations, change configuration,
or implement fixes unless the user explicitly asks for those actions too.

## Establish the investigation boundary

Identify the supplied bead, operation, native task/turn, time range, repository,
and expected workflow. A bead is not always the whole incident: exclude unrelated
later turns and concurrently created work, and explain that boundary. If a key ID
can be recovered from evidence, do so instead of asking the user. Ask only when a
missing choice would materially change the incident scope.

Record the timezone used in the report. Preserve original timestamp precision but
do not imply cross-system clock precision that the evidence cannot support. Define
the start and end events used for every duration.

## Collect durable evidence first

When a local Fulcrum installation is available, prefer the bundled read-only
collector:

```sh
python .agents/skills/fulcrum-postmortem/scripts/collect_evidence.py \
  --bead BEAD_ID \
  --operation OPERATION_ID \
  --repo . \
  --output /tmp/fulcrum-postmortem-evidence.json
```

Add `--operation`, `--task`, and `--git-ref` more than once when needed. Use
`--since` to bound diagnostic and Git history. The bundle is local investigative
material and may contain prompts, paths, or repository metadata; inspect it before
sharing it outside the project. The script never mutates Fulcrum, the provider,
Git, or native tasks. A failed probe is retained as evidence rather than aborting
the bundle.

Supplement the bundle when necessary with exact provider records, historical
native task output, repository files, and source from the incident revision.
Prefer `git show COMMIT:path` or an isolated worktree over changing the active
checkout. Never explain historical behavior solely from current source.

Use this evidence priority when accounts conflict:

1. durable operation receipts and authoritative work state;
2. provider certificates, delivery facts, and committed Git history;
3. native task/turn records and structured diagnostic events;
4. service health and process logs;
5. agent narration and human recollection.

Narration can explain intent, but it does not prove that a handoff, notification,
promotion, or closure happened. Distinguish a command attempt, a poll/replay, an
accepted operation, and a distinct model turn. Do not count them as interchangeable
events.

## Reconstruct before diagnosing

Build a private event ledger before writing prose. Normalize timestamps, attach
every known bead/operation/task/turn/source/provider identifier, and calculate
stage durations from named boundary events. Reconcile duplicate observations and
separate retries of one durable operation from genuinely new operations or turns.

Compare the actual path to the intended path and budget. Include the complete
critical path and any repeated actors, manual repairs, stale replays, validation
waits, promotion waits, closure delay, and archival delay. Mark unexplained gaps
as unknown; do not allocate unattributed time to a model, lock, scheduler, network,
or provider merely because that explanation is plausible.

## Analyze causality

Separate:

- trigger and user-visible impact;
- primary root cause;
- contributing product, protocol, configuration, agent, and operational factors;
- recovery actions and any failures introduced by recovery;
- detection and observability failures;
- latent conditions that did not cause this run but increased its severity.

A root cause must survive a counterfactual test: removing it should prevent the
failure mode or materially change the path. Do not label elapsed time, retries,
or a model's poor decision as the root cause when a deterministic state contract,
recommendation, or missing invariant made that outcome likely. Conversely, do not
claim a code defect explains gaps it cannot account for.

For every material statement, classify the support while drafting:

- **confirmed**: directly supported by durable evidence or incident source;
- **inferred**: the evidence supports the conclusion but does not state it;
- **unknown**: the available evidence cannot distinguish credible explanations.

State important inferences and unknowns explicitly in the finished report.

## Treat observability as its own finding

Report what a single trace did and did not contain, which joins were missing, what
was pruned or overwritten, and how much manual correlation was required. Logging
is insufficient when the timeline cannot be reconstructed from stable identifiers
and timestamps, even if raw clues exist somewhere. Recommend bounded, redacted,
causally linked evidence rather than indiscriminate log volume.

## Corrective actions and validation

Map each action to a named failure. Prefer enforced invariants, deterministic
state transitions, and regression tests over prompt-only advice. Separate actions
already completed from required or intentionally deferred work. For completed
actions, cite the commit, configuration publication, test, benchmark, or live
observation that proves the new behavior. Never mark a latency or reliability goal
complete using a mock that cannot measure it.

If the user requested fixes as well as a report, implement them only after the
incident sequence and causes are stable. Update the postmortem with resolution
status and honest remaining acceptance criteria.

## Final quality check

Before delivery, verify that:

- identifiers, counts, ordering, time arithmetic, and timezones agree;
- the executive summary matches the detailed timeline;
- every causal claim names its evidence or confidence;
- expected versus actual behavior is explicit;
- observability gaps are separate from workflow causes;
- corrective actions cover prevention, recovery, detection, and regression;
- the report contains no secrets or unnecessary raw prompt/log dumps;
- links use the repository's actual paths and the report is saved under the
  user-requested location, normally `docs/postmortems/YYYY-MM-DD-<slug>.md`.
