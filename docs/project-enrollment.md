# Project enrollment for agents

Use this procedure instead of inspecting Fulcrum implementation source.

## Authority and discovery

`project add` may be run only by the human or the retained Vizier. A native task
must never pass `--actor human`. In the retained Vizier task, omit `--actor` and
`--thread-id`; the CLI binds `CODEX_THREAD_ID` and verifies the standing identity.
A different task should send one exact enrollment request to the retained Vizier.

Before enrollment:

1. Call native `list_projects` and select the single saved local project whose
   resolved `path` exactly matches the requested root. Retain its exact
   `projectId`; never invent or recreate it.
2. Run `fulcrum project list --json`. If the requested project is already
   enrolled with the same root and Codex project ID, treat enrollment as complete.
   If either value conflicts, stop and report the conflict rather than overwriting
   it.
3. Obtain any non-observable policy fields from the request. Do not invent an
   integration branch or repository-specific prepare and validation commands.

For the canonical Fulcrum checkout, the enrollment is fixed:

```json
{
  "id": "fulcrum",
  "root": "/Users/dthurn/fulcrum",
  "codex_project_id": "<exact list_projects projectId>",
  "integration_branch": "master",
  "prepare_argv": ["scripts/prepare-check"],
  "validate_argv": ["scripts/check"],
  "enabled": true
}
```

## Enroll once

Generate a new UUID and attach the JSON to stdin before starting the command. Do
not create a temporary input file, manually initialize Beads, register Tollgate,
restart a service, or read Python source to discover this call shape.

```sh
enrollment_request_id=$(uuidgen)
fulcrum project add --json --wait \
  --request-id "$enrollment_request_id" --input - <<'JSON'
{
  "id": "fulcrum",
  "root": "/Users/dthurn/fulcrum",
  "codex_project_id": "<exact list_projects projectId>",
  "integration_branch": "master",
  "prepare_argv": ["scripts/prepare-check"],
  "validate_argv": ["scripts/check"],
  "enabled": true
}
JSON
```

The command discovers or creates the exact Tollgate repository and connects the
project to Fulcrum's shared Beads backend. On a client timeout, resume only the
same command with the same request UUID. Never retry an uncertain enrollment with
a new UUID.

Verify the terminal result with `fulcrum project show <id> --json`. Enrollment is
complete only when the returned root, Codex project ID, integration branch,
commands, and enabled state match the request.

## Additional projects

After bootstrap, list saved local projects that are not enrolled and ask once
whether the user wants any of them enrolled. Do not enroll additional projects
until the user selects them. For each selection, retain the native path and Codex
project ID and ask only for non-observable fields that were not already supplied:
the Fulcrum project ID, integration branch, and prepare/validation argument
arrays. Empty arrays are valid only when the user confirms that no project-specific
commands are required.
