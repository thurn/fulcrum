---
name: fulcrum-uninstall
description: Completely uninstall Fulcrum, its local state, owned Codex configuration, services, automations, role tasks, links, and optionally its canonical source checkout.
---

Use this skill only after the human explicitly requests a Fulcrum uninstall or a
complete clean-install reset. The request authorizes Fulcrum-owned removal, not
unrelated Codex tasks, configuration, skills, projects, or user files.

First use Codex native tools while the retained installation evidence is still
available. Resolve the Codex root from `CODEX_HOME`, falling back to `~/.codex`.
Inspect its `automations/*/automation.toml` files and delete only an automation
whose name is exactly `Fulcrum Marshal check`, using the installed automation ID.
If none exists, do nothing. List active tasks with a maximum limit of 50 and page
through archived tasks with `nextCursor`. Archive only active Fulcrum role tasks
with the exact titles `🧰 STEWARD 🧰`, `🧭 MARSHAL 🧭`, and `🔮 VIZIER 🔮`,
including retained replacements. Already archived role tasks need no action. Do
not archive ordinary tasks in the Fulcrum project or unrelated historical tasks.

Before including `--remove-source`, verify that the source checkout has no
uncommitted changes and no commits ahead of its configured upstream. Stop rather
than discard local work unless the human explicitly authorizes that loss.

Run the deterministic preview and inspect every target:

```sh
~/fulcrum/scripts/uninstall --remove-source --json
```

For a full uninstall, apply that exact plan with:

```sh
~/fulcrum/scripts/uninstall --yes --remove-source --json
```

If the human explicitly wants to retain the clone for a clean reinstall, omit
`--remove-source`; the script still removes `.venv`, all runtime data, owned
services, launchers, skill links, MCP configuration, hook handlers, caches, and
temporary state. Pass explicit `--instance`, `--config`, `--brain`, or
`--codex-root` paths only when installation evidence proves non-default paths.

Stop if the preview reports an unsafe path, malformed Codex configuration, or a
foreign real file at an owned link location. The script preserves such foreign
files rather than guessing ownership. Its `native_cleanup_checklist` is a
reminder to perform the checks above, not evidence that matching tasks or an
automation exist.

When `--remove-source` is used, change to a stable directory such as the home
directory before post-removal checks because the original working directory no
longer exists. Verify that the result has `ok: true`, no `dev.fulcrum` launch jobs
or Fulcrum MCP processes remain, the owned paths are absent, Codex configuration
still parses, and unrelated Codex configuration remains. Identify processes from
their executable and arguments; do not rely on a bare `pgrep -f` whose search can
match the verifier itself. Existing tasks may retain their original MCP tool
catalog, but fresh tasks read the updated configuration. Do not restart, quit, or
toggle Codex Desktop as part of uninstall.
