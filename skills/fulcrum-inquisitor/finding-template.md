# Architectural finding: <underlying problem>

Project and repository scope: <registered project; reviewed commit>
Stable problem key: <same boundary/failure key across review runs>
Suggested priority and rationale: <impact and change coupling, not recency>
Related open findings: <IDs inspected; semantic match or new issue rationale>

## Evidence and impact

<Source paths and symbols, call/data path, concrete coupling or failure mode.>
<Separate observed behavior, unavailable evidence, and expected benefit.>

## Behavior-preserving direction

<Responsibility or type boundary to clarify; complexity that can be removed.>
<Existing valid behavior, error semantics, wire/storage formats and public API
that must remain compatible. State uncertainty rather than assuming safety.>

## Affected interfaces

<Callers, domain models, storage/external boundaries, migration concerns.>
<Keep the scope within the assigned project; linked external work goes to Archon.>

## Acceptance and validation

<Observable improvement plus preserved behavior and representative edge cases.>
<Proportionate contract/integration checks; measure any performance claim.>

## Disposition

New finding: future work only. Existing match: append evidence/proposal while
preserving its active contract. No implementation authority is granted.

For no findings, save coverage, reviewed revision, evidence, and remaining
limitations instead of filing an empty or speculative issue.
