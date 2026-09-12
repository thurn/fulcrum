# Bounded debrief interviews

Use actual Sage/subject task and host IDs from supported Codex observations.
Inspect current archival state; unknown is not false. One postmortem may interview
multiple subjects: begin_interview creates a stable per-subject interview_id,
while run_id retains the postmortem identity. Read an existing record by that
interview_id before creating; resume it without resetting deadlines or prior state.
Legacy run-only records require inspection and explicit migration, not invented
archival history. The interviewer owns the record; Watchman only reports obligations.

1. Build fulcrum.interviews.begin_interview and persist it with atomic_write_record
   BEFORE unarchiving or sending. The record saves prior_archived, identities,
   one-hour reminder time, and two-hour finish deadline. These are bounded defaults,
   not a new scheduled task. Never overwrite an existing interview on retry.
2. If archived, persist advance_interview(event="reopening") before calling
   set_thread_archived(archived=false). Confirm success with `reopened` and a tool
   evidence reference. An uncertain result requires inspection, not another blind
   unarchive. Do not reopen a currently active interview subject twice.
3. Send via the shared handoff procedure: “Debrief only for this postmortem.
   Describe workflow friction, supporting evidence, and possible improvements.
   Do not resume implementation, select tasks, authorize candidates, or promote.”
   The subject's implementation progress/mandates remain unchanged; this is not
   a reactivated execution run or its stop-reminder sequence.
4. Read replies through supported task tools and record a response reference.
   On a later patrol at/after reminder_due_at, record reminder_attempted BEFORE
   sending the single reminder. Record sent/failed result; uncertain delivery
   is inspected. Never send a second reminder. At finish_due_at, conclude with
   available evidence even if the first request/reminder failed or no patrol ran.
5. On reply or timeout, rearchive only a task this Sage confirmed reopening,
   after verifying no unrelated work has since resumed. Previously unarchived
   subjects stay unarchived. Persist `restored` only after archive success (or
   inspection proves an uncertain reopen left it archived). Then `complete`
   records responded/missing_response. Do not mark complete with restoration pending.

interview_action is a read-only next-action helper, not an actor or clock daemon.
If archive state is uncertain or unrelated work appeared, finish the report with
missing-response/evidence limits and hand the unresolved obligation to Archon.
Retain the active interview for Watchman; no repeated debrief requests. Recovery
first resumes the owning Sage. Any ownership transfer is coordinated explicitly;
another role must not write the Sage's record as itself or erase saved prior state.
