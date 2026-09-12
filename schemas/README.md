# Local record ownership

`records-v1.schema.json` is the versioned contract for untracked Fulcrum local
state. Every record declares its `record_kind`, `schema_version`, `writer_id`,
and UTC `updated_at`. Unknown fields and unsupported versions are rejected.

| Record kind | Mutable-file owner | Path family |
| --- | --- | --- |
| `installation` | Setup operator or human | `configuration/installation.json` |
| `project_registry` | Current Archon | `registry/projects.json` |
| `role_run_registry` | Current Archon | `registry/roles.json` |
| `holds_jobs` | Current Archon | `registry/holds-jobs.json` |
| `assignment` | The assignment's Overseer | `assignments/<assignment-id>.json` |
| `progress` | The role task named by `role_task_id` | `progress/<task-id>.json` |
| `executor_evidence` | The Executor named by `executor_task_id` | `evidence/<task-id>.json` |
| `interview` | The Sage named by `sage_task_id` | `interviews/<run-id>.json` |

Ownership is cooperative: it prevents accidental writes through Fulcrum's
helper, not arbitrary filesystem access. Beads remains the source of mutable
task status. Large logs and conversations remain in their owning tools.

The Archon-owned `holds_jobs` record stores UTC recurrence anchors, next-due
times, and the actual active task per role/scope. Watchman anomaly fingerprints
live in the Watchman's own `progress` record, so patrol deduplication does not
grant it registry-write or dispatch authority.
