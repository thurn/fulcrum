---
name: marshal
description: Operate the registered scheduled Marshal task.
---

This skill does not create or enter a Marshal task. Bootstrap owns the one retained
Marshal identity. On its 15-minute heartbeat, call `marshal_check`, settle only the
returned bounded curation, incident, or recovery brief, then call `marshal_decide`
with its returned `turn_id` and an `input` object containing its returned
`decision_id` plus the targeted `decisions` rows. Pass an empty `decisions` array
for a healthy no-op. Put both curation rows and native-action recovery rows in
that one array. For each native-action recovery row, execute the exact returned
inspection action first when present, then submit one fresh-state recovery row
with `bead`, `action_id`, `expected_state`, and `decision`. Use
`adopt_observed_task` only after the inspection has reconciled the retained action
to `succeeded`; use `retry_same_action` only when the brief proves
`definitely_not_created`; otherwise use `leave_uncertain`. Execute and report the
exact same-Steward recovery action returned by `marshal_decide` before ending.
`marshal_check` resolves native turn identity from retained
transcript evidence; do not invent any identifier or inspect implementation source
to discover the call shape. Healthy no-op checks end quietly. Apply only current-
state targeted changes; stale rows must be returned as stale. Ordinary ready work
needs no Marshal permission.

After three failed ordinary repair cycles, `recovery_prepare` may authorize exactly
one scoped Justiciar creation in the additional recovery slot. Never create a
second Steward, a second recovery slot, or a general replacement fleet. Resume the
same Steward only through an exact recorded recovery action after reconciling any
outstanding native effect.
