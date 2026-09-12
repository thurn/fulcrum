# Runtime capability boundary

Fulcrum targets the installed Codex app-server's generated protocol schemas. The
adapter currently requires `initialize`/`initialized`, `model/list`, thread
start/name/read/resume/settings/archive operations, turn start/interrupt, and
turn/thread/helper observations. Unsupported server requests receive an explicit
protocol error; Fulcrum never silently approves them.

The desktop join mechanism uses `CODEX_APP_SERVER_WS_URL` because it was verified
on the inspected build. Setup rechecks shared thread visibility on the installed
build. An unavailable capability is a named integration blocker, not an invented
API, private-server fallback, or compatibility alias.

Tollgate integration uses the installed `tg --json --no-launch` CLI and Beads
integration uses its native `bd` CLI and Dolt lifecycle.
