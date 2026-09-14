---
name: bead
description: File one small implementation-ready follow-up discovered during a session.
---

Use this skill only for one small, understood follow-up that is outside the
assigned work: a pre-existing defect, tooling failure, workflow friction, or
another incidental session discovery. If the work needs substantial planning,
multiple dependent tasks, or material clarification, direct the human to
`$weaver` instead.

Gather only enough read-only repository and command evidence to make that one
report self-contained. Do not implement the follow-up. Create a new UUID for the
`report_key`, retain the exact JSON for retries, and submit the report with this
single Fulcrum interface:

```sh
fulcrum report --input - <<'JSON'
{
  "report_key": "new UUID for this report",
  "project": "include only when Fulcrum cannot infer it",
  "title": "concise implementation title",
  "problem": "bounded problem",
  "observed_evidence": "specific evidence observed in this session",
  "required_change": "bounded implementation change",
  "acceptance_checks": ["observable validation check"],
  "dependencies": [],
  "context": []
}
JSON
```

Omit `project` when it can be inferred. Include only relevant dependency Bead IDs
and concise implementation context. File independent problems as separate
invocations with new report keys. On an exact retry, reuse the unchanged key and
payload. Report the returned Bead ID and publication state to the human.

Do not call `fulcrum intake`, register as Weaver, rename or bind the task, create a
managed action, call `fulcrum finish`, approve or schedule work, or invoke `bd`
directly.
