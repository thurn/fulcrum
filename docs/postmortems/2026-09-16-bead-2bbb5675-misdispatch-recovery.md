# Bead `fc-2bbb5675` misdispatch and recovery postmortem

**Date:** 2026-09-16  
**Status:** Open; the original implementation and workflow recovery were not completed during this investigation

## Summary

`fc-2bbb5675` began as a routine fix to `scripts/reconcile_skills`: replace an
obsolete import, reconcile the renamed `weaver` skill, and preserve unrelated
user skill entries. Weaver prepared an implementation-ready scope and Marshal
authorized Executor work. The dispatched prompt still exposed skill-invocation
syntax from the original user text, so the worker invoked Weaver instead of
acting as Executor. Marshal then escalated the resulting workflow defect to
`HUMAN` rather than selecting scoped recovery. That created a human-only gate
which Marshal could not subsequently clear.

These are separate bugs and should not be collapsed into one escaping issue.

## Bugs

1. **The original script is broken.** `scripts/reconcile_skills` imports the
   removed `reconcile_skill_links` API and does not correctly implement the
   `fulcrum-weaver` to `weaver` rename.
2. **Downstream role prompts leak raw intake.** Executor received executable
   skill-invocation text from the original request instead of only the exact
   Marshal-authorized Weaver scope. A literal skill reference therefore
   overrode the authorized role. This is a contract-selection defect; merely
   shell-escaping the text is not a sufficient fix.
3. **Role authorization is not dominant.** A worker authorized as Executor was
   able to follow an incidental skill trigger embedded in retained evidence.
   Retained user text must be clearly inert for downstream roles.
4. **Marshal used `HUMAN` for routine recovery.** The unexpected role and bead
   state were diagnosable system failures. Marshal had a first-class `recover`
   action and should have assigned a scoped Justiciar. Human routing is a last
   resort for irreducible authority, intent, credential, or policy questions.
5. **A mistaken human gate is self-locking for Marshal.** Once Marshal chose the
   `human` action, only a human or Vizier could run `human resolve`. The refusal
   to impersonate human authority was correct, but the transition that created
   the gate was not. The system also failed to turn the human's direct reply
   into the retained resolution needed to resume Marshal review.
6. **Recovery registration was degraded.** A scoped Justiciar entry failed
   online because the controller socket refused connections. Its offline
   fallback failed because the designated installed client rejected the current
   authoritative config's `source` field. Service status simultaneously showed
   a different deployment binary configured for the controller, leaving client,
   controller, and configuration compatibility unclear.

## Correct path

Marshal should record a bounded `recover` decision; Justiciar should fence the
bead, reconcile the mistaken dispatch, and restore a clean implementation
handoff; Executor should then implement the prepared `reconcile_skills` scope.
Human or Vizier involvement should be required only if recovery encounters a
genuinely irreducible decision.

## Required follow-up

- Compile Executor and Warden prompts exclusively from the exact authorized
  scope, with raw intake retained only as inert provenance.
- Add a regression test where intake contains a conflicting skill invocation.
- Make Marshal recovery the default for unexpected managed state and test that
  routine failures cannot be escalated to `HUMAN` without an irreducible reason.
- Restore compatibility between the designated CLI, controller deployment, and
  authoritative configuration before retrying Justiciar recovery.
