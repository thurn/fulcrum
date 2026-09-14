---
name: operative
description: Take temporary human-authorized emergency control of this Fulcrum installation.
---

This skill is break-glass authority. Use it only because the human explicitly
invoked `$operative`; neither a managed agent nor Fulcrum itself may invoke it.

Before inspecting or changing the installation, create a mode-0600 temporary JSON
file containing the complete human-stated emergency in `description`. Use a
file-writing tool or a quoted JSON writer; never place the human's text in a shell
argument. Add `model` and `effort` only when the human selected them. Then run:

```sh
fulcrum operative register --input /absolute/private/operative-input.json
```

If the controller is reachable but exact App Server verification is unavailable,
registration returns explicitly provisional local-repair authority under the
already-retained fence. If the CLI cannot import or cannot reach the controller,
locate the retained control root without importing Fulcrum.
`FULCRUM_CONTROL_ROOT` wins; otherwise it is the `control` directory beside
`FULCRUM_CONFIG`, whose default is
`$HOME/Library/Application Support/Fulcrum/config.json`. Invoke the installed
launcher directly:

```sh
"/absolute/control/root/recovery/current/bin/operative-recovery" register --input /absolute/private/operative-input.json
```

Do not continue unless registration returns either active authority or explicitly
provisional local-repair authority. Provisional authority permits only local
filesystem, Git, recovery-artifact, and service checks until `reconcile` verifies
the exact App Server thread. It does not permit agent control or durable workflow
mutation.

Follow the returned instructions. Begin with `operative dossier` and preserve
before-state. Every mutating control uses an absolute private JSON `--input` file,
stable operation key, exact retained IDs, and any human-authored notice or evidence
inside files. Use `wind-down`, `reconcile`, `worktree`, `repair-check`, `reinstall`,
`service-check`, `finish`, `abort`, and `recover` only within the human-stated
emergency. Never infer that an interrupt stopped a parent or helpers; require the
targeted terminal observations returned by wind-down.

Successful finish is two phase. Submit the complete closeout contract by file and
remain bound until the controller observes this turn and all helpers terminal,
revalidates readiness, archives the Operative, closes the journal, and restores
prior dispatch intent. Abort is not success: it requires explicit human direction,
keeps dispatch disabled, and leaves the same takeover recoverable by exact ID.
