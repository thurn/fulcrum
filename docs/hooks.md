# Hooks

Fulcrum installs one advisory `SessionStart` hook for compaction. It reads the
caller's current action from SQLite and rebuilds the same complete, current brief
used by dispatch. If state is unavailable, it reports that limitation and grants
no authority.

There is no Stop hook enforcement, task-waiting denial, peer routing, archival,
or operational write in hooks. Missing outcomes are detected from runtime events
and handled once by the controller. Unrelated and Plan-mode conversations are not
intercepted.
