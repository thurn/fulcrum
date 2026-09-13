# Hooks

Fulcrum installs one read-only `SessionStart` hook for compaction. For a managed
current action it supplies a brief role and action reminder and tells the agent to
continue from the conversation summary. It does not replay the role manual,
approved scope, history, or command schemas. If exact facts were lost during
compaction, the reminder points the agent to the optional, read-only `fulcrum context`
command. That command identifies the caller from its managed Codex task and renders
only the authoritative current action; it cannot select another task or a past action.

Unrelated, retired, archived, and actionless tasks receive no text, including
Plan-mode Weaver without a writable authoring action. A read failure for a known
managed action produces an advisory diagnostic, never invented authority.

There is no Stop hook enforcement, task-waiting denial, peer routing, archival,
or operational write in hooks. The controller sends one short missing-outcome
reminder without changing the original action's scope, question, or decision batch.
