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

The current Archon retains historical registrations and allocates the next
unused role number. Overseer and Executor share one new pair number; Sage and
Inquisitor have separate sequences. Never recycle archived numbers. Weavers
keep their descriptive title and have no role number. Human-created Archon,
Watchman, and Weaver preserve their selected model and reasoning.

Only the current Archon writes the role/run registry. After approved activation
in a writable turn, a Weaver initializes its own progress record (which includes
its role) and reports its actual identity to Archon for registration. Plan-mode
invocation is read-only: no state, brain, Beads, or inferred hook registration.
Use fulcrum.roles.resolve_identity and initialize_progress for validated records;
write them with atomic_write_record or fulcrum state write --input.
