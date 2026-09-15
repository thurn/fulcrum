# 15 — Brain publication

Status: implemented and validated.

Dependencies: [02](02-beads-ledger-and-operations.md), [03](03-configuration-and-projects.md), [08](08-controller-supervision.md), [14](14-memory-and-document-publication.md)

Normative reading: [brain cadence](../contracts.md#brain-git-publication-and-cadence), [bookkeeping boundaries](../contracts.md#publication-bookkeeping-boundaries). Also follow the [index conventions](README.md#worker-contract) and [completed interface contracts](../contracts.md#10-complete-workflow-and-operator-interfaces).

## Outcome and code boundary

Persist the shared stock Beads database to the brain repository's dedicated Git
Dolt ref, separately from ordinary YAML/knowledge publication. Replace issue-export
and old private publication-obligation assumptions in `brain.py`/`controller.py`.
The authoritative operational state remains Beads, even during remote outage.

## Commands and durable facts

`ledger sync/status` and `config sync` return native/local commit IDs, last observed
remote ref/commit, pending age, cadence, unresolved receipt and failure details.
Default cadence is 300 seconds with changes; `ledger sync` flushes immediately.
Store publication fields on `fc-system.fc.publication` and one `ledger.sync`
receipt per logical batch, reusing it across uncertainty/retries.

## Implementation sequence

1. Configure the stock Beads Git transport remote to the same configured brain Git
   repository. Probe actual `refs/dolt/data` behavior with disposable Git transport;
   do not equate its Git OID to the native Dolt commit. No SQL access or issue export.
2. Determine real pending changes using supported Beads diff/history/current reads
   against the retained native boundary. Exclude only publication bookkeeping fields
   and `ledger.sync` records; a leadership change on the same control issue still
   counts. Unsupported field-level evidence is an explicit capability failure,
   never a reason to synthesize another dirty-state database.
3. Record batch intent, commit the current Beads working set and retain its actual
   native commit. Push and inspect remote history for that commit. A changed Git
   ref alone does not prove the intended database state was published. Use supported
   native history inspection in an isolated scratch checkout after uncertainty.
4. Coalesce dirty changes without sliding the five-minute deadline on every edit.
   Native `bd` writes participate. Concurrent writes after the commit boundary
   belong to the next batch. Bookkeeping recorded after successful push rides with
   the next real batch and cannot generate an infinite sequence of pushes.
5. Trigger ordinary selected-file publication through task 14 for authorized YAML
   and knowledge edits. Report it separately from native ledger persistence.
6. Retry proved transient failure three sends total. After exhaustion, timer ticks
   and unrelated new work do not refresh the budget. Explicit sync/reconcile or an
   observed failed-to-healthy relevant capability transition records a new retry
   grant on the same receipt. Divergence needs controlled repair with writers stopped.
7. On startup inspect overdue/unsettled batches; on graceful shutdown try one flush
   within 30 seconds. Failure preserves local data and does not prevent shutdown.
   With no controller running, no background publisher exists.

## Acceptance

Advance the test clock around native edits and demonstrate publication by the next
fixed tick, no idle commit, no self-trigger from bookkeeping, and inclusion of real
control/config changes. Lose an applied push response and reconcile without a new
receipt or duplicate batch. Verify last-success/pending-age/error output after an
outage and exhausted retries across restart. A fresh remote read must prove the
native commit's presence. Historical replacement is reserved for task 21 reset;
ordinary maintenance never force-pushes or pulls remote ownership into running work.
