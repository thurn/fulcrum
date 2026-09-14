# Controller contracts

An unfinished `operative.json` is the installation-wide authority and dispatch
fence; SQLite is its queryable mirror. The only exception to normal role
separation is a human-created `$operative` bound to the exact current native
thread. Managed roles cannot self-promote, and a different thread cannot replace
or steal it. Every bypass retains intent, exact target, before-state, observed
result or uncertainty, after-state, evidence, and correlation.

Static installation configuration is ordinary JSON without a format or schema
version. Resettable operational state is SQLite and has no generic record-write
CLI. Native external IDs remain strings; Fulcrum entities use SQLite integer IDs.

Canonical Weaver lineages are allocated atomically. A Weaver and its primary
Executor and Overseer share the Weaver number; specialists retain independent
role counters:

```text
👑 ARCHON 👑
🧵 [WVR0001] Description
⚒️ [EXE0001] Description
🔎 [OVR0001] Description
📖 [SAGE0001] Description
🛡️ [INQ0001] Description
```

Every bead filed by a managed Weaver records both the originating Weaver task and
its lineage number. Graph intake applies that identity to every graph member.
Archon approval may group only beads from one lineage in a run and copies the
origin onto the run and each assignment. Human intake outside a registered Weaver
has no managed lineage.

The unsuffixed Executor and Overseer are the lineage's primary conversations.
Sequential assignments, later runs, exact retries, controller restarts, and tasks
filed by later turns in the same Weaver conversation reuse those native threads
when each is idle, terminal, unarchived, still available to the controller, bound
to the same project/model/effort, and not owned by incompatible unfinished work.
Pending completion archival is canceled when new lineage work is filed or a thread
is claimed, and archival is deferred while the lineage has pending or active work.
When the final lineage assignment closes, canceled obligations are re-armed and
every eligible Weaver, Executor, and Overseer conversation in the lineage receives
one archive obligation and the normal idle delay.

If reuse is unsafe because the retained conversation is archived, retired,
uncertain, active, has nonterminal helpers, has incompatible authority or work, or
is otherwise unavailable, the controller preserves the lineage number and creates
the next unused uppercase suffix. Overflow begins at `B` (`EXE0001B` or
`OVR0001B`), then `C`, and continues deterministically; the unsuffixed identity is
never renamed. Persisted thread-creation intent reserves its selected suffix, and
reconciliation or an exact retry resolves that intent before any later suffix can
be allocated. Canonical lineage identities are unique even after archival, so an
old identity cannot be duplicated.

Retained pre-lineage Executor and Overseer names, including archived records,
reserve their visible canonical identity. Lineage allocation therefore skips an
occupied unsuffixed or suffixed name, while ordinary unlineaged provisioning skips
internal role numbers already used by lineage tasks. Either allocation order
remains valid and cannot produce two visible role identities with the same code.

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
