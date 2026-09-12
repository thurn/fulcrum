---
name: fulcrum-setup
description: Run or repair the Python-driven Fulcrum installation.
---

Run `<retained-checkout>/scripts/setup` and follow only the missing interactive
login, consent, brain destination, project, validation-command, and Archon model
questions it presents. The script owns dependency installation, editable links,
the shared app-server and controller services, desktop launcher, project
enrollment, Archon creation, policy initialization, and readiness checks.

Do not create role conversations, bootstrap JSON, task IDs, schedules, patrols,
or readiness evidence manually. On failure, report the script's exact remaining
action. Re-run the same command after resolving it; setup resumes from retained
configuration and operations.
