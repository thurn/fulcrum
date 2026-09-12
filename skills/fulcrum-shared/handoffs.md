# Reliable handoffs

This protocol applies to persistent and implementation roles. A Weaver's
completion report is a separate one-way message: send it at most once to the
current Archon when routable, do not await acknowledgement, and do not create
progress, assignment, or delivery-retry state.

Handoffs are direct messages to the exact registered recipient. Fulcrum roles
must not call Codex `wait_threads` or `mcp__codex_app__wait_threads` to await
delivery, inspect a zero-timeout snapshot, babysit another task, resolve an
uncertain handoff, or wait for a current-turn result. Use one-shot inspection
when needed and retain the expected next actor/action in progress.

1. Check the recipient's resolved task/host ID and current bead/run/candidate.
   Record intended actor/action, handoff_needed=true, handoff_sent=false before
   sending. Include approved scope, evidence, and the exact requested action.
2. Call Codex send_message_to_thread with that ID and host. Do not use a final
   answer as a substitute, and do not route to clientThreadId.
3. Only a successful tool result permits handoff_sent=true and the expected
   wait/completion phase. Acceptance by Codex is not action by the recipient.
4. On error or uncertain result, retain handoff_needed=true and delivery_error.
   Inspect the destination with read_thread and the original tool result before
   retry. Reconcile a confirmed delivered message; retry only after establishing
   it was not delivered. Do not create a second assignment or mandate for a
   duplicate message. Recipient verifies exact assignment/candidate before acting.

fulcrum.roles.prepare_handoff and finish_handoff produce owned progress records
for registered roles; call the atomic writer before sending and after the
outcome. They never send messages or invent delivery receipts. A pending
unconfirmed handoff must be reconciled before preparing another one. They are
not used by ephemeral Weavers.
