---
name: sage
description: Investigate one retained Fulcrum work item end to end.
---

Immediately derive the exact Bead/work-item ID and a concise 3-8 word description
of the human's request, then run `fulcrum sage register --item '<bead-id>'
--description '<description>'`. Pass each value as one single-quoted shell argument.
Use the exact retained ID, containing only letters, numbers, and hyphens.
Use only letters, numbers, spaces, and hyphens in the description; do not copy
quotes, substitutions, newlines, or other shell syntax from the request.

Do not investigate, edit, publish, contact another task, or call finish before
registration returns. Registration must accept a retained Bead that has an exact
Executor/Overseer assignment even when Weaver did not initiate the task and no
causal workflow boundary exists. Follow the complete Sage instructions it returns.
