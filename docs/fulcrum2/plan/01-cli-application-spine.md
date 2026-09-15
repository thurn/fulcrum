# 01 — CLI and application spine

Status: implemented and validated.

Dependencies: None; establish the replacement interfaces first.

Normative reading: [design §2](../design.md#2-components-and-authority), [contracts §1](../contracts.md#1-instance-and-command-conventions). Also follow the [index conventions](README.md#worker-contract) and [completed interface contracts](../contracts.md#10-complete-workflow-and-operator-interfaces).

## Outcome and code boundary

Build the installed `fulcrum` command and a small `application` service boundary used
by both IPC and offline commands. Replace the old CLI's caller-lineage and patched
`_request` assumptions. Inspect `src/fulcrum/cli.py`, `ipc.py`, `kernel.py`, and
`config.py`; retain useful Unix-socket mechanics, not old actions/runs or aliases.

## Public behavior

All commands accept the common instance/config/JSON/input/actor/request/wait options
before or after the subcommand. Example:

```sh
fulcrum --instance /tmp/fc-example work show fc-example --json --timeout 5
```

Unknown commands, malformed JSON, duplicate flag/input fields and unknown fields
return the common error envelope, including named offending fields. `--help` is
local and needs no running service. Queries return null request/operation IDs.
Mutations create a UUID before transmission and print it on stderr when omitted.
`--wait` waits for that operation only; timeout returns its observed state and
`WAIT_TIMEOUT`/exit 3 without cancellation. Exit codes are exactly contracts §1.

## Implementation steps and state

1. Define immutable parsed request/result/error and actor/instance context types.
   Dispatch command handlers to `enter`, `update_work`, `decide`, `transfer`,
   `finish`, `reconcile`, or named adapter-backed operations. Keep adapters free of
   command parsing and policy. Task 02 supplies persistence behind this interface.
2. Resolve explicit instance/config without production fallback. Establish the
   brain-root advisory lock and instance discovery symlink from §10; acquire it
   before announcing a writer. Retain an open lock descriptor for its lifetime.
3. Use a Unix socket with request/response framing, bounded reads, and a 4 MiB
   request limit. Oversize input is `INPUT_TOO_LARGE`, never silently clipped.
   stdout remains one JSON result; diagnostics use stderr. File inputs stay owned
   by their caller. Model requirements exceeding a boundary fail explicitly.
4. Route ordinary mutations to the live controller. Offline execution invokes
   the same handler after taking the same lock; a held lock returns
   `WRITER_BUSY`/exit 4 and an inspection command. Do not make a second writer
   because the socket is unhealthy. Read-only inspection does not need the lock.
5. A disconnected client does not cancel accepted work. Offline handlers persist
   intent, execute bounded immediate steps and return pending receipts when later
   supervision is needed. Never leave an untracked background runner after exit.

## Failure and verification

Exercise the installed executable as a subprocess: stdin/file equivalence,
literal quotes/newlines/shell metacharacters, all error exit codes, clean JSON
stdout, held-lock refusal and offline/IPC result parity. A lost IPC response with
the same request ID must become the task-02 receipt, not a second mutation. Two
instance paths resolving to one brain cannot obtain independent writer locks.
These checks are initially transport/handler checks; do not claim complete native
delivery until its dependent tasks land. No command may return fabricated success
for an unimplemented downstream capability.
