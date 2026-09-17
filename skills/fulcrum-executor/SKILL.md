---
name: fulcrum-executor
description: Execute one exact registered implementation assignment.
---

Register the native task with `register_worker` using the marker's bead,
assignment token, task/session identity, exact Tollgate workspace, project, and
source. Do not edit before registration succeeds. Include that assignment token
with every later Fulcrum MCP mutation. Implement only the retained scope in that
workspace, report meaningful progress, run proportionate local
checks, commit the source, and call `finish` with the observed outcome and evidence.
Never broaden scope, poll native tasks, or invoke an unclaimed native effect.
