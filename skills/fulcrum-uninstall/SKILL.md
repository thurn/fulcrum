---
name: fulcrum-uninstall
description: Completely uninstall Fulcrum, its local state, owned Codex configuration, services, automations, role tasks, links, and optionally its canonical source checkout.
---

Use this skill only after the human explicitly requests a Fulcrum uninstall or a
complete clean-install reset. The request authorizes Fulcrum-owned removal, not
unrelated Codex tasks, configuration, skills, projects, or user files.

First use Codex native tools while the retained installation evidence is still
available. Delete the automation named `Fulcrum Marshal check`, using its exact
installed automation ID when available. Archive only Fulcrum role tasks with the
exact titles `🧰 STEWARD 🧰`, `🧭 MARSHAL 🧭`, and `🔮 VIZIER 🔮`, including
retained replacements. Do not archive ordinary tasks in the Fulcrum project.

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
files rather than guessing ownership. After application, verify that the result
has `ok: true`, no `dev.fulcrum` launch jobs or Fulcrum MCP processes remain, the
owned paths are absent, and unrelated Codex configuration remains. A Desktop
restart may be needed to make the already-running UI forget its removed MCP
configuration; never restart Desktop without the human requesting it.
