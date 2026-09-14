---
name: fulcrum-vizier
description: Enter the Vizier role for an explicit human policy request.
---

Run `fulcrum enter vizier --description "$ARGUMENTS" --json`, adding an explicit `--bead ID` only when the human supplied one. The description is a literal argument representing the complete current request; do not reduce it to a tag. Do not act before registration returns. Follow the returned instructions and report degraded registration truthfully.
