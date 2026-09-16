# Bead `fc-2bbb5675` misdispatch and recovery postmortem

**Date:** 2026-09-16  
**Status:** Human gate cleared; original implementation remains open for Executor dispatch

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

During follow-up, Vizier resolved the mistaken human reason through operation
`fc-a3f6f787a10c415bad7ba2e88ec763bb`. The bead's title and outcome were rewritten
without executable skill syntax while preserving the behavioral requirement. It
returned to Marshal-owned backlog with no waiting reasons and `executor` as the
requested continuation. This unblocked work but did not itself fix the underlying
prompt, role-boundary, runtime-compatibility, or script defects.

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
7. **Weaver froze a stale implementation assumption into acceptance.** The
   first scope required the retired `fulcrum-weaver` path even though the human
   requirement was only to repair reconciliation for the completed Weaver
   rename. Repository paths and API names were routine implementation facts,
   not product requirements.
8. **Marshal crossed the technical-judgment boundary.** Instead of dispatching
   the unambiguous outcome, Marshal invoked Weaver instructions, inspected source
   constants and packaging, decided the first scope was technically wrong, and
   prescribed a replacement. Marshal owns backlog priority, readiness, policy,
   and dispatch—not implementation analysis. Its conclusion happened to be
   correct, but reaching it was outside the role.
9. **Routine correction was needlessly rescheduled.** Executor should have
   inspected current master, selected the current API and `skills/weaver` path,
   and implemented the unchanged behavioral requirement. Another Weaver turn
   was unnecessary because user intent, scope, and safety were not in question.
   Treating Weaver's technical notes as immutable added latency and exposed the
   bead to the role-trigger defect again.

## Correct path

For the original work, Marshal should have checked administrative readiness and
dispatched Executor. Executor should have resolved the obsolete path and API as
routine current-master facts, implemented the stable behavioral outcome, and
recorded the evidence for Warden review. Weaver clarification is appropriate
only when evidence changes user-visible behavior, scope, safety, or intent.

After the wrong role actually ran and corrupted workflow state, Marshal should
have recorded a bounded `recover` decision; Justiciar should have fenced the
bead, reconciled the mistaken dispatch, and restored a clean Executor handoff.
Human or Vizier involvement should be required only if recovery encounters a
genuinely irreducible decision.

## Corrective actions

### Role and prompt integrity

- Compile Executor and Warden prompts from the stable behavioral outcome and
  exact authorized scope revision. Keep raw intake as labeled, inert provenance;
  never concatenate it into downstream instructions.
- Make the explicitly authorized role dominant over skill-like text everywhere
  in titles, outcomes, evidence, comments, memory, command output, and quoted
  Markdown. Add adversarial tests for `$name`, skill links, role names, and
  instruction-shaped code blocks in each field.
- Record the dispatched role and compiled contract in the receipt, then reject
  role entry or finish if the observed role differs. Do not rely on model text
  claiming that it reconciled two roles.

### Technical responsibility

- Define Weaver output as an implementation proposal serving a stable behavioral
  requirement, not an immutable collection of repository assumptions. Label
  product requirements separately from suggested paths, symbols, and mechanisms.
- Instruct Executor to resolve routine current-source facts autonomously when
  behavior, scope, and safety remain unchanged. Executor should report the
  correction in progress/finish evidence; it should block only for a material
  requirement or safety decision.
- Restrict Marshal review to ownership, priority, capacity, dependencies,
  overlap, policy, recorded blockers, and presence of a usable outcome. Marshal
  must not invoke worker skills, inspect implementation files to adjudicate a
  scope, or prescribe technical corrections.
- Route genuine requirement ambiguity to Weaver and broad independent technical
  investigation to Sage. Do not create a clarification turn merely because a
  proposed implementation detail is stale.

### Recovery and human escalation

- Make `recover` the Marshal default when managed execution diverges from its
  authorized role or state. Recovery must fence the bead, stop conflicting work,
  preserve evidence, and transfer a bounded repair scope to Justiciar.
- Require a structured irreducibility reason before Marshal may select `human`:
  missing human intent, authority, credential, external approval, or policy.
  Ordinary diagnosis, stale source facts, failed commands, and unexpected task
  state are not qualifying reasons.
- Allow an explicit human reply associated with the bead/reason to satisfy the
  retained gate once, with an auditable receipt, instead of demanding a second
  semantically redundant resolution action.
- Add transition tests proving that a mistaken human escalation can be corrected
  by human or Vizier without losing scope and that continuation returns to
  Marshal for mechanical routing only.

### Script and reconciliation behavior

- Replace the removed `reconcile_skill_links` wrapper call with the current
  reconciliation API and consume its installed/unchanged/removed result.
- Test the standalone script under isolated `HOME` for missing and stale link
  repair, repeat-run idempotence, the canonical `weaver` path, retirement of
  `fulcrum-weaver`, preservation of unrelated entries and real directories, and
  clear status-2 failures for unsafe replacement.

### Runtime compatibility and observability

- Enforce one compatible schema across the designated CLI, controller binary,
  and authoritative configuration. Deployment activation must verify that the
  configured controller accepts every current config field before switching.
- Make service status prove socket responsiveness and report client/controller
  revision skew directly; a running process and existing socket are insufficient.
- Keep online and offline recovery parsers compatible with the same config, and
  add an end-to-end test covering controller refusal followed by offline
  Justiciar takeover.
- Correlate scope revision, Marshal decision, dispatched role, native task,
  recovery fence, and human resolution in one per-bead trace so each boundary
  failure is immediately attributable.
