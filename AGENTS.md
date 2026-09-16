When working within a fulcrum workflow, follow its provided instructions. When
working one-off outside fulcrum, immediately commit & push all changes in this
repo to remote.

Use Conventional Commits commit message format

Do not preserve backwards compatibility ever. Do not version things.

If you feel like you should store a hash of something, maybe just don't?

After changing pyproject.toml or requirements-dev.lock, reinstall requirements
and the editable package in .venv.

Before changing execution, installation, or runtime ownership, read
[the live-iteration architecture](docs/architecture/live-iteration.md).
Preserve its no-restart, source-pinning, and connection-continuity invariants.

Fulcrum behavior comes from local master in ~/fulcrum. Commit ordinary code,
formula, and instruction changes to master; the next operation automatically
prepares and uses that commit. Never add an installation, manual activation,
remote-publication wait, or controller restart to the ordinary editing workflow.
Per-operation immutable snapshots exist only to keep delayed imports and asset
reads consistent. Existing operations and agent turns continue undisturbed.
Skills are direct links into ~/fulcrum/skills and subsequent reads see edits
immediately. See the architecture for exceptional dependency/state/host changes.
