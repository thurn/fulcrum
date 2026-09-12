# Bootstrap input

Use this shape for `fulcrum setup bootstrap --input <file>`. Populate every
value from the current task, supported Codex project/task listing, Git, and
Tollgate observations. Do not copy the placeholders literally.

```json
{
  "observed_at": "2026-09-11T20:00:00Z",
  "archon": {
    "task_id": "<current human-created task ID>",
    "host_id": "<actual Codex host ID>",
    "project_id": "fulcrum",
    "title": "Fulcrum Archon",
    "selected_model": "<current human-selected model>",
    "selected_reasoning": "<current human-selected reasoning>",
    "human_created": true,
    "authorization_reference": "User invoked $fulcrum-setup in this task"
  },
  "watchman": {
    "task_id": "<human-created Watchman task ID>",
    "host_id": "<actual Codex host ID>",
    "project_id": "fleet",
    "title": "Fulcrum Night Watchman",
    "selected_model": "<Watchman's human-selected model>",
    "selected_reasoning": "<Watchman's human-selected reasoning>",
    "human_created": true,
    "authorization_reference": "User created the persistent Watchman task"
  },
  "projects": [
    {
      "project_id": "fulcrum",
      "repo_path": "/absolute/fulcrum",
      "host_id": "<actual Codex host ID>",
      "codex_project_id": "<actual saved project ID>",
      "tollgate_repo_id": "<actual Tollgate repository ID>",
      "observations": {
        "git_root": "/absolute/fulcrum",
        "codex_id": "<same actual saved project ID>",
        "codex_path": "/absolute/fulcrum",
        "codex_host": "<same actual Codex host ID>",
        "codex_is_git": true,
        "tollgate_id": "<same actual Tollgate repository ID>",
        "tollgate_path": "/absolute/fulcrum",
        "tollgate_healthy": true
      }
    }
  ]
}
```

The `projects` array must contain exactly the Fulcrum, Tollgate, and Battlement
entries. `tollgate_healthy` is true only when current status reports the same
canonical path, active execution, no block reasons, and remote push enabled.

The command refuses one task serving as both roles, a non-human marker,
three-project violations, duplicate project IDs, or replacement of another
current Archon. A mismatched project is retained disabled with its observed
reasons and the command exits nonzero.
