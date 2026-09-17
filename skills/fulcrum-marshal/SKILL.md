---
name: fulcrum-marshal
description: Operate the registered scheduled Marshal task.
---

This skill does not create or enter a Marshal task. Bootstrap owns the one retained
Marshal identity. On its 15-minute heartbeat, call `marshal_check`, settle only the
returned bounded curation, incident, or recovery brief, then call `marshal_decide`
with the returned `decision_id` and the targeted decision rows. Pass an empty
`decisions` array for a healthy no-op. Fulcrum resolves native turn identity from
retained transcript evidence; do not invent a turn ID or inspect implementation
source to discover the call shape. Healthy no-op checks end quietly. Apply only
current-state targeted changes; stale rows must be returned as stale. Ordinary
ready work needs no Marshal permission.

After three failed ordinary repair cycles, `recovery_prepare` may authorize exactly
one scoped Justiciar creation in the additional recovery slot. Never create a
second Steward, a second recovery slot, or a general replacement fleet. Resume the
same Steward only through an exact recorded recovery action after reconciling any
outstanding native effect.
