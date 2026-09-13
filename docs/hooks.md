# Hooks

Fulcrum installs one read-only `SessionStart` hook for compaction. For a managed
current action it supplies a brief role and action reminder and tells the agent to
continue from the conversation summary. It does not replay the role manual,
approved scope, history, or command schemas, and does not require a context-fetching
command. The actual request and necessary facts were delivered in the action message.

Unrelated, retired, archived, and actionless tasks receive no text, including
Plan-mode Weaver without a writable authoring action. A read failure for a known
managed action produces an advisory diagnostic, never invented authority.

There is no Stop hook enforcement, task-waiting denial, peer routing, archival,
or operational write in hooks. The controller sends one short missing-outcome
reminder without changing the original action's scope, question, or decision batch.
