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
