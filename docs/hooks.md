# Compaction hook

Fulcrum installs one advisory `SessionStart` hook for `source=compact`. The installed
command is equivalent to:

```sh
fulcrum hook context --input - --instance INSTANCE
```

The hook reads the native envelope from stdin. For a current managed task whose
thread still owns its work, it returns:

```json
{"continue":true,"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":"..."}}
```

Unrelated, inactive, missing, or unreadable state returns `{"continue":true}`. The
hook never starts a turn, changes Beads, denies a tool, enforces a stop, or invents
ownership. Marshal context is rebuilt from current Beads/YAML decisions and
rationale, not replayed notification history.

`scripts/setup` and `fulcrum skills reconcile` replace only hook handlers marked
`Fulcrum:` and preserve all unrelated hook groups. They also refuse to replace real
user skill directories. The hook invokes the instance's installed CLI, not the
development checkout or a superseded private-state module.
