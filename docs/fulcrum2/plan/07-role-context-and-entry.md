# 07 — Role context and entry

Status: implemented and validated.

Dependencies: [04](04-work-and-ownership.md), [06](06-codex-runtime-adapter.md)

Normative reading: [formulas](../contracts.md#4-native-beads-setup-and-formulas), [roles](../design.md#4-roles-and-ownership-lifecycle). Also follow the [index conventions](README.md#worker-contract) and [completed interface contracts](../contracts.md#10-complete-workflow-and-operator-interfaces).

## Outcome and source boundary

Ship eight role formulas, corresponding human-invocation-only microskills, static
fallback guidance and the shared context/compaction provider. Replace nested prompt
assembly in `src/fulcrum/prompts.py` and obsolete role assets. Keep useful role
instructions proportionate; do not copy old interview, recurring-patrol or
Executor/Overseer correction-loop requirements.

## Commands and inputs

`enter ROLE --description TEXT [--bead ID] [--thread-id ID] [--model NAME --effort
LEVEL]` creates/adopts/binds work or starts the native task through the same service.
Return bead/task IDs, `ownership_operation`, role, full `instructions`, and pending
operation facts. `context --bead ID` returns current work/role context; `context
--role ROLE` supplies available fallback guidance without invented registration.
`hook context --input -` follows the native hook envelope, not the CLI envelope.

## Implementation sequence

1. Package one stock formula per role with a single cooked `work` step. Include
   outcome, project/workspace, acceptance, current evidence/blockers, role duties,
   next action, and exact finish commands with task/ownership IDs. Render `task terminal stop TASK --all-owned` when handoff
   requires stopping its owned background tools. Persist original
   authoring fields and compiled description in the same work update. Do not pour
   extra stage beads or rewrite the bead's identity on a role transition.
   Weaver/report authoring guidance asks for benefit, concrete outcome, relevant
   dependencies and material uncertainty; rough effort is optional and must have
   a basis. Preserve short/native requests with missing-information markers,
   rather than enforcing an intake checklist. Weaver clarification prompts carry
   Marshal's specific question and return findings on the same bead.
2. Pass the complete cooked text directly at turn start. Add correlation using
   task 06's start receipt. Tests inspect actual sent content, not formatting-only
   snapshots. Context recovery uses the same source without rewriting requirements.
3. Apply model precedence: explicit invocation, work-role override, project-role
   default, global-role default. Record value and origin. Validate advertised
   effort/model support and return unavailable/unsupported rather than substituting.
4. Exact titles use the entire suffix after `fc-` and the eight specified emoji/
   role codes; Vizier/Marshal keep their fixed titles. Reentering a valid same-bead/
   role acquisition does not create a worktree or redundant turn into the caller.
5. Terminal human entry begins immediately within physical capability; managed
   automatic entry uses normal admission. Existing leadership receives only the
   appropriate request. Vizier never receives unsolicited system turns.
6. Sage/Mason transitions preserve interrupted work and delivery facts. On closed
   work, retain terminal disposition for restoration after findings. Keep the new
   specialist role after completion; continuation is routed through Marshal.
7. Degraded ledger/cook/runtime failures return packaged role instructions and
   available evidence, exit 6 when registration is incomplete. No invented bead or
   replay journal. Respect actual host Plan Mode write restrictions.
8. The compact hook injects context only for current managed compacted sessions;
   unrelated/inactive tasks get none. Failure is advisory, never a tool denial.
   Expose a role-specific current-context projection hook for task 09. Marshal
   compaction uses `context --role marshal` to recover current decisions/rationale
   from Beads/YAML, not a replay of old notifications. Task 09 supplies that
   projection without creating a dependency here on later memory/publication.

## Acceptance

Exercise all eight entry commands, title examples, literal multiline descriptions,
model overrides, repeat entry, prompt completeness and offline context. Verify no
initial read-the-bead-only prompt, no role prose in adapters, no source truncation,
and no hooks that start turns or mutate Beads. Confirm all installed skill metadata
disables implicit invocation. Live task execution is verified in task 23, not
required to unit-test every prompt phrase.
