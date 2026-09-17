# Codex hooks

Bootstrap installs exactly six Fulcrum-owned command hooks in the Codex profile:
`SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse`, `Stop`, and
`Interrupt`. Unrelated hook configuration is preserved. Each command reads one JSON
event from stdin, emits only event-specific hook JSON on stdout, and writes
diagnostics to stderr. Interrupt has a three-second timeout; other hooks have a
ten-second timeout and compact added context is capped at 2,000 characters.

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

Transcript paths are unstable input. The scoped collector reads only registered
task transcripts, retains lifecycle/turn/response identities and token records,
advances a byte cursor, and records delayed, malformed, or incomplete evidence as
gaps. Stop or final text alone never releases ownership or proves completion.

Hook commands resolve the current committed checkout on every invocation. Existing
agent turns keep already-delivered instructions, while the next hook callback sees
current policy and skill text without reinstalling or restarting anything.
