# Codex hooks

Bootstrap configures exactly five Fulcrum command hooks in the Codex profile:
`SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse`, and `Stop`.
Interrupt is deliberately not installed: transcript `turn_aborted` evidence and
client-disconnect cancellation provide the authoritative interruption path.
Unrelated hook configuration is preserved. Each command reads one JSON
event from stdin, emits only event-specific hook JSON on stdout, and writes
diagnostics to stderr. Hooks have a ten-second timeout and compact added context
is capped at 2,000 characters.

Hooks are cooperative evidence and admission checks, not a universal execution
sandbox. The CLI independently validates every durable transition. `PreToolUse`
denies an unclaimed native effect with `hookSpecificOutput.permissionDecision =
"deny"`; it does not use unsupported `continue: false` behavior. Post-tool evidence
settles the same action attempt as agent reporting.

Bindings come from retained standing identities, exact assignment markers, and
trusted hook session/task context. Caller-supplied role labels alone never grant
authority. Hooks ignore unrelated tasks and other instances. Session and prompt
hooks restore current role, assignment, workspace, pause, and outstanding action
context without curated memory.

The returned hook status reports configuration separately from operational
confirmation. Bootstrap does not treat writing `hooks.json` as proof that required
callbacks are trusted, enabled, or observed. After the operator explicitly confirms
that all five exact definitions are trusted and enabled, bootstrap retains that
confirmation only while the installed definitions remain unchanged. This clears the
operator gate without claiming the later `hook_identity` acceptance check; that check
still requires callbacks bound to the retained task and session identities.

Transcript paths are unstable input. The scoped collector reads only registered
task transcripts, retains lifecycle/turn/response identities and token records,
advances a byte cursor, and records delayed, malformed, or incomplete evidence as
gaps. Native `turn_aborted` is terminal interruption evidence and cancels an
outstanding wait for that exact task. Stop or final text alone never releases
ownership or proves completion.

Standing Steward waits use one long-yield execution cell and never poll that cell
with repeated model turns. A stopped client closes the broker connection and
cancels its active evaluation; transcript evidence then settles the durable wait.
When a registered Steward tries to finish while admission is running and its turn
has no valid idle or protocol stop, the `Stop` hook returns one `decision: block`
continuation telling that same turn to resume the instruction loop. Codex's
`stop_hook_active` flag prevents a repeated continuation. Interruptions remain
observational and are recovered independently by Marshal.

Hook commands resolve the current committed checkout on every invocation. Existing
agent turns keep already-delivered instructions, while the next hook callback sees
current policy and skill text without reinstalling or restarting anything.
