# 14 — Memory and document publication

Status: not implemented.

Dependencies: [02](02-beads-ledger-and-operations.md), [03](03-configuration-and-projects.md), [13](13-plans-and-root-completion.md)

Normative reading: [memory/publication](../contracts.md#planning-memory-and-publication-commands), [design §9](../design.md#plans-memory-and-publication). Also follow the [index conventions](README.md#worker-contract) and [completed interface contracts](../contracts.md#10-complete-workflow-and-operator-interfaces).

## Outcome and source boundary

Implement curated Beads memory and human-readable plan/memory Git publication.
Retain useful Git inspection from `src/fulcrum/brain.py`; replace any use of Markdown
as an independent work ledger or broad staging/reset of the user's brain tree.

## Commands and records

`memory list/show/set`, `knowledge publish --bead ID`, `plan publish/refine` export
hooks and `config sync` share a narrow `publish/inspect` adapter. Memory input is
`{scope,title,text,references}` with optional ID for replacement. Return actual
memory ID, publication operation, selected paths, local commit, observed remote
commit and origin task. Ledger mutation/local publication/remote sync are distinct.

## Implementation steps

1. Store memory text in native title/description and scoped `fc:memory` metadata.
   Global memory belongs to Vizier; project/role memory to Marshal. Human writes
   are explicit; other roles file proposals/findings. Preserve prior text on the
   mutation receipt. Queries never create memory/analytics issues.
2. Select relevant global/project/role memory into context with a 4,000-character
   selected-text bound, titles and continuation references. Do not truncate original
   task requirements or include full conversations. Marshal's whole brief remains
   within its own bound after memory selection. Marshal's live working set and
   decision rationale come from task 09's Beads projection; curated memory adds
   lasting preferences/lessons, never a second backlog, incident history or
   transcript summary. Memory unavailability cannot disable that projection.
3. Validate export destinations/relative paths against configured roots, including
   symlink escapes. Record exact selected content and paths before filesystem/Git
   effects. Use an isolated Git worktree so unrelated staged/dirty user changes
   remain untouched. Never force-push or stage live Dolt files.
4. Preserve current remote commits and local unsent content. Commit selected artifacts
   with Conventional Commits, push, then inspect ancestry. Required sync is complete
   only if the remote contains the exact recorded commit. A newer unrelated remote
   commit is fine when it contains that commit; divergence/conflicts need repair.
5. Lost commit/push response is inspected through recorded source/path/native Git
   identities before retry. No content hashes or separate sync journal. Local
   authored data survives remote outage and remains visible through plan/memory CLI.
6. Exported Markdown is derived from canonical Beads scope. Manual edits are not
   silently imported as ownership/dispatch truth. Configuration export publishes
   authorized current YAML only; mechanical maintenance does not authorize edits or
   conflict resolution by Marshal/Justiciar.

## Acceptance and failures

Use disposable local repositories/remotes to prove selected-path publication,
unchanged unrelated index/worktree, preservation of remote commits, ancestry-based
success and response-loss reconciliation. Introduce a conflicting remote edit and
assert local data remains plus a repair item; do not force it away. Exercise a
path/symlink escape rejection. Read memory with service stopped and context after
task replacement. Missing knowledge remote blocks only required publication/
activation, not accepted intake or unrelated delivery. Task 15 adds the shared
five-minute cadence; do not implement another publication daemon here.
