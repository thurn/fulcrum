# Identity and registration

Read this before provisioning or registering a role. All references in this
bundle are relative; install the whole skills tree, including shared/.
Use configured brain/state roots, never a developer home directory.

Use actual task and host IDs returned by supported Codex tools. A creation
result with only clientThreadId is pending: retain it as a correlation hint,
leave task_id null, and do not message it. Resolve through list_threads or
completion results, checking project, host, run, and unique numbered role tag.
Inspect both pinned and ordinary tasks and retained registrations. Multiple
matches are an identity conflict; none after an uncertain create is still
uncertain, not permission to create a duplicate. Inspect task history and
creation status before a deliberate retry. Titles are discovery aids only.

The current Archon retains registrations for persistent and implementation
roles and allocates the next unused role number. Overseer and Executor share
one new pair number; Sage and Inquisitor have separate sequences. Never recycle
archived numbers. Weavers are ephemeral and unregistered: keep their
descriptive title, but do not write a role registration or Weaver progress
record. Human-created Archon and Watchman preserve their selected model and
reasoning.

Only the current Archon writes the role/run registry. Persistent and
implementation roles initialize their own progress after identity resolution.
Plan-mode Weaver invocation is read-only: it writes no state, brain, Beads, or
inferred role registration. After approval, a Weaver can author plans and beads
in a writable turn without registration; it may send one completion report to
the current Archon without waiting for acknowledgement.
Use fulcrum.roles.resolve_identity and initialize_progress for validated records;
write registered-role records with atomic_write_record or fulcrum state write
--input.
