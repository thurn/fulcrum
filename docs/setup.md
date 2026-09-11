# Setup, verification, and removal

## Bootstrap boundary

The compatibility report was committed and pushed at
`6bec7288b39403d085d62da1417118ac08cc0c2c`. That clean source state was
registered with Tollgate as repository
`01a09277-71b8-7c93-9d80-04b8d8dbd072`, using `scripts/check` as its sole
voting step. Tollgate candidate `01a09278-b9c2-7831-b361-8e403cea9bf2`
correctly terminated as `baseline-failing`: the check script did not exist at
the anchor. This is retained bootstrap evidence, not a passing certificate.

Task 02's package scaffold is the first ordinary code candidate. It must pass
the same configured command from a clean Tollgate worktree before the task is
treated as certified. Registration by itself says nothing about code health.

## Local installation

1. Install CPython 3.12.
2. Clone the repository and verify that `origin` identifies the intended
   private GitHub repository.
3. Create a virtual environment and install `requirements-dev.lock`, followed
   by the editable package as shown in the README.
4. Run `scripts/check`, `fulcrum --help`, and `fulcrum version`.

The application will later resolve configuration in this order: explicit CLI
path, named Fulcrum environment variable, then per-user configuration. The
brain defaults to `~/brain`; local state defaults to
`~/Library/Application Support/Fulcrum`. Task 04 implements those rules. Do not
put credentials, Beads working files, logs, or real operational records in the
source checkout.

## Tollgate verification

```sh
tg repo list --json --no-launch
tg --repository 01a09277-71b8-7c93-9d80-04b8d8dbd072 config explain --json --no-launch
tg --repository 01a09277-71b8-7c93-9d80-04b8d8dbd072 status --json --no-launch
```

The repository identity must resolve to this checkout, the integration ref
must be `refs/heads/release`, and the voting step must run `scripts/check`.
Tollgate owns release advancement and candidate cleanup. When automatic source
push is disabled, finish a promoted change with `tg --repository <id>
--no-launch push` and verify the configured remote tip before closing work.

## Removal

Remove only the installation being decommissioned:

- Uninstall the `fulcrum` package from its virtual environment or delete that
  dedicated environment.
- Remove Fulcrum from Tollgate with `tg repo remove` only when intentionally
  discarding its validation history and configuration.
- Preserve the brain and local application state unless they are separately
  backed up and explicitly selected for removal. Package removal never implies
  database deletion.
