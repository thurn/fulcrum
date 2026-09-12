# Infrastructure readiness gate

The 2026-09-11 gate is **FAIL**. Tasks 1–19 produced the required implementation
and test evidence, but Task 21 must not start until a human creates and enrolls
the persistent Archon and Night Watchman, installs the single hourly Watchman
heartbeat, and the Archon persists the already-resolved three-project registry.

The canonical machine-readable matrix is
[`readiness-evidence.json`](readiness-evidence.json). Evaluate it with:

```sh
fulcrum readiness --matrix docs/readiness-evidence.json
test "$?" -eq 2
```

Exit 2 is the expected result while required blockers remain. Change evidence
to pass only after repeating the cited exercise; do not edit a status merely to
admit Dashboard work. Optional `unsupported` results are allowed only where the
matrix names the active fallback.

## Gate matrix

| Boundary | Result | Evidence |
| --- | --- | --- |
| Package and Tollgate | PASS | `scripts/check`; Task 18/19 certificates in [validation](validation.md) |
| Beads/Dolt lifecycle, sync, backup, restore | PASS | Tasks 05–06 in [validation](validation.md); [setup](setup.md#shared-brain-server) |
| Seven skills and role boundaries | PASS | Tasks 09–10 and 16; live 0.2.0 install |
| Delivery, holds, review, promotion, recovery | PASS | Tasks 08 and 12–14 |
| Patrol, cadence, interviews, findings/NEWS | PASS | Tasks 15–16 |
| Desktop hook delivery | UNSUPPORTED (optional) | Task 18; skills/patrol fallback remains active |
| Runtime observation feed | UNSUPPORTED (optional) | Task 15; unavailable remains distinct from idle |
| Human Archon/Watchman and hourly wake | **FAIL (required)** | Live task listing and `fulcrum doctor` |
| Persisted three-project registry | **FAIL (required)** | Live mappings resolved; Archon-owned record absent |
| Isolated test scope/no premature Dashboard | PASS | Temporary fixtures and repository inventory |

## Reproduction and completion

Run all commands from `/Users/dthurn/fulcrum`, using the installed 0.2.0
environment and the real brain only for read-only health checks:

```sh
scripts/check
bd --directory /Users/dthurn/brain where --json
bd --directory /Users/dthurn/brain dolt status --json
bd --directory /Users/dthurn/brain dolt test --json
fulcrum doctor \
  --expected-brain-remote git@github.com:thurn/brain.git \
  --skills-root /Users/dthurn/.codex/skills \
  --hooks-config /Users/dthurn/.codex/hooks.json
```

The three observed mappings are:

| Project | Git root | Codex project | Tollgate repository |
| --- | --- | --- | --- |
| Fulcrum | `/Users/dthurn/fulcrum` | `6a2c98a4-8bd9-45e8-a46c-efdce987d83f` | `01a092d0-d54b-7fe3-86d2-6659645f7d43` |
| Tollgate | `/Users/dthurn/tollgate` | `38962730-e02f-4a1d-aee5-db6579af1349` | `019ff669-c120-73a3-8619-e4bbff6d45fc` |
| Battlement | `/Users/dthurn/battlement` | `6a322457-e047-4865-8605-f8c7d6149442` | `01a00ae2-9411-7c00-b742-50f3abde5e7c` |

All three are local Git projects; their Tollgate registrations are active,
push-enabled, rooted correctly, and have no repository block reasons. The
current failure is persistence/ownership, not an unhealthy integration.

To clear the gate, the user creates the two persistent tasks. Activate their
installed skills, record the actual IDs with human model authorization, create
or update one hourly heartbeat attached to the Watchman, and have the current
Archon enroll the table above through the ownership-safe writer. Rerun install
with the actual schedule ID, rerun doctor, and update the matrix with the new
evidence. Desktop hook and runtime visibility may remain unsupported only with
their documented fallbacks; every required row must pass.

No production pilot, Dashboard source, UI fork, Dashboard service, or Dashboard
bead is part of this gate. Task 21 begins only after this matrix evaluates ready.
