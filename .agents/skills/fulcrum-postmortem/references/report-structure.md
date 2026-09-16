# Postmortem report structure

Use this as a coverage guide, not a mandatory heading template. Combine or rename
sections when that improves the specific report, but preserve the evidence and
causal distinctions.

## Front matter

Record the incident date and timezone, status, severity, affected bead and initial
operation, incident source revision, delivered revision if any, and the exact
investigation boundary. Do not invent severity definitions or owners that the
project does not have.

## Executive summary

In the first two paragraphs, state:

- the requested and expected path;
- the actual outcome and elapsed time;
- the primary confirmed cause;
- the largest contributing failures;
- whether user data, source, production, or only the test objective was affected;
- whether remediation is complete, partial, or unverified live.

Use a compact path diagram when repeated roles or loops are otherwise hard to see.

## Impact and expected versus actual

Describe concrete effects: incorrect source changes, delay, duplicate agents,
manual intervention, broken references, misleading state, or retained UI clutter.
Avoid hypothetical impact in the impact section.

A useful comparison table is:

| Stage | Expected boundary/budget | Actual boundary/duration | Evidence | Result |
| --- | --- | --- | --- | --- |

State the exact start and finish events beneath the table.

## Actors and identifiers

Map each logical role to native task/thread IDs, turn IDs, entry/finish operations,
source OIDs, and provider handles. Explain reused standing leaders separately from
new worker tasks. Note excluded later turns or adjacent beads.

## Detailed timeline

Prefer one row per causally meaningful event:

| Time (TZ) | Event | Durable identifiers/evidence | Gap from prior stage |
| --- | --- | --- | ---: |

Include command acceptance separately from user-facing narration when they differ.
Say “unexplained gap” when no durable span attributes the interval.

## Causal analysis

Show the failure chain from state or input through decisions to impact. Quote only
short fragments that are necessary to establish a contradiction. Use historical
source for code claims and current source only to describe remediation.

For complex incidents, a failure inventory can use:

| Failure | Layer | Evidence | Causal role | Confidence |
| --- | --- | --- | --- | --- |

Useful layers include intake/scope, leadership, admission, workspace, command
contract, delivery, reconciliation, archival, performance, and observability.

## Observability assessment

List the public trace result, missing correlations or spans, retention limitations,
and the extra sources required for reconstruction. Specify the smallest telemetry
changes that would make the same investigation routine. Keep payload retention
bounded and redacted.

## Corrective actions

Use explicit status rather than a flat recommendation list:

| Status | Action | Failure prevented/detected | Proof | Remaining acceptance |
| --- | --- | --- | --- | --- |

Cover prevention, automatic recovery, operator visibility, performance only where
measured, and regression tests. A prompt clarification without an invariant is
rarely sufficient for a deterministic orchestration defect.

## Evidence quality and final assessment

Summarize which portions are confirmed, inferred, or unknowable. End with the
behavior that the next operational test must demonstrate. Do not convert missing
evidence into a confident narrative for rhetorical neatness.
