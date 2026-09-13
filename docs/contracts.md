# Controller contracts

Static installation configuration is ordinary JSON without a format or schema
version. Resettable operational state is SQLite and has no generic record-write
CLI. Native external IDs remain strings; Fulcrum entities use SQLite integer IDs.

Canonical task names are allocated atomically per role:

```text
👑 ARCHON 👑
🧵 [WVR0001] Description
⚒️ [EXE0001] Description
🔎 [OVR0001] Description
📖 [SAGE0001] Description
🛡️ [INQ0001] Description
```

Finish commands resolve the caller's native thread to one current action. Exact
retries return the retained result; incompatible, retired, unregistered, or
conflicting finishes fail. The accepted forms are implemented and documented by
`fulcrum.outcomes`; managed prompts render those same definitions.

Assignment stages are `queued`, `preparing`, `implementing`, `review_pending`,
`reviewing`, `correcting`, `delivering`, `recovering`, `completed`, and `canceled`.
A hold preserves the stage. Completion requires promotion, configured source
synchronization, retained certification, owned cleanup, and Beads closure—not merely
a green check. Promotion already effected with a pending synchronization, cleanup, or
certificate postcondition remains delivery reconciliation; post-promotion attention is
delivery recovery, never a request to replace the promoted source.
