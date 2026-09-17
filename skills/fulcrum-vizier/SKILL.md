---
name: fulcrum-vizier
description: Present and record exact retained human decisions.
---

This skill applies only to the one bootstrap-registered Vizier task. Present the
retained decision, evidence, and bounded consequences without inventing a broader
choice. Record only the human's explicit authority through `decision_respond`.
Vizier may pause or resume durable admission when explicitly authorized; native
Desktop Stop is not a Fulcrum pause. Do not curate memory, replace fleets, or act as
a routine dispatch gate.

When the human or bootstrap explicitly requests project enrollment, read and
follow `~/fulcrum/docs/project-enrollment.md`. Do not inspect implementation
source to rediscover the command. In this retained Vizier task, omit `--actor`
and `--thread-id`; the CLI binds and verifies the current `CODEX_THREAD_ID`. Pass
JSON through an already-attached stdin stream, use one new request UUID with
`--wait`, and verify the result with `fulcrum project show`. Do not create a
temporary input file or retry an uncertain enrollment.
