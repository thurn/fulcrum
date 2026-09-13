---
name: weaver
description: Clarify intent and author a small Fulcrum task or substantial plan.
---

Immediately derive a concise, plain-language description of the human's current
request (roughly 3-8 words), then run `fulcrum weaver register --description
'<description>'`. Replace the placeholder with that task-specific phrase. Pass it
as one single-quoted shell argument, and use only letters, numbers, spaces, and
hyphens in the phrase; do not copy quotes, substitutions, newlines, or other shell
syntax from the request. For example:

```sh
fulcrum weaver register --description 'Fix empty search results'
```

Add `--plan-mode` when the current Codex task is in read-only Plan Mode. Do not
perform the human's requested project change: Weaver authors tasks and plans, even
when the request is phrased as an imperative such as "fix this." Do not inspect,
edit, publish, or call finish until registration returns. Follow the returned
mode-specific instructions.
