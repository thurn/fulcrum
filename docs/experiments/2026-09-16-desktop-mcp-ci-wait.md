# Five-minute blocking MCP wait in Codex Desktop

On 2026-09-16, a disposable native Desktop task made one blocking local MCP
request, received a synthetic CI failure after five minutes, and completed the
same turn. No model response or generated tokens were attributable to the
waiting interval. This validates the basic blocking-call mechanism, not a
production CI integration or an unlimited wait.

## Method and observed result

The task was `01a0ac20-f948-70c0-aa10-94235d0b9a8f` on host `local`, titled
**Disposable five-minute MCP CI wait experiment**. Its one native turn was
`01a0ac20-faef-7a23-88bd-1dbe092ddce0`.

A temporary stdio MCP server exposed `wait_for_ci_results`. The task called it
once with `{"run_id":"five-minute-failure","delay_seconds":300}`. The server
blocked on an event released by one timer, then returned a synthetic failure.
It sent no progress notifications and performed no polling. The MCP connection
used `tool_timeout_sec = 420`; the code-mode call requested
`yield_time_ms = 360000` so its wrapper would not yield during the test. The
[documented MCP default](https://learn.chatgpt.com/docs/extend/mcp) is 60 seconds,
so production configuration cannot rely on that default for this use case.

| Observation | Result |
| --- | --- |
| Server wait started | 2026-09-16 21:30:47.657323 UTC |
| Server returned failure | 2026-09-16 21:35:47.661745 UTC |
| Monotonic elapsed time | 300.001570 seconds |
| Native tool duration | 300,002 milliseconds |
| Native turn duration | 312,587 milliseconds |
| CI tool calls | One |
| Model responses | Three: tool discovery, emitting the call, reporting the result |
| Model responses during the wait | Zero |
| Wrapper wait/resume calls by the measured worker | Zero |
| Follow-up native messages or additional assignments | Zero |
| Final native status | Completed; task idle |

The final response reported
`CI_PROBE_FAILURE_OBSERVED run_id=five-minute-failure status=failed elapsed_seconds=300.002 wrapper_wait_or_continuation_required=false`.
An independent audit compared the server log, exact worker rollout, native
status, and usage records and confirmed the result.

## Token accounting

Across all three measured worker responses, usage was **80,968 input tokens**,
including **72,832 cached** and **8,136 uncached**, plus **175 output tokens** and
**zero reasoning tokens**. This is setup/call/result overhead, not zero total
cost. No additional response or generated tokens were caused by the five-minute
waiting interval. A usage record written 35.677 milliseconds after server wait
start belongs to the response that had already emitted the tool call.

Observer, setup, and auditor usage are excluded. The observer used bounded
native task waits to monitor this experiment; the measured worker did not.
Those observer calls are not proposed production orchestrator behavior.

## Scope and retained evidence

The experiment did not test a real CI provider, longer durations, default
timeouts, cancellation, disconnection, app/broker restart, concurrent message
senders, or mandatory turn-exit enforcement. It did not prove a background
interface for waking an ended task. A pending MCP call needs no such wake.

Local evidence is retained at
`/Users/dthurn/.codex/tmp/fulcrum-ci-wait-probe-20260916/`, including
`server.py`, `server-events.jsonl`, `probe-config.json`,
`measurement-baseline.json`, `measurement-midwait.json`,
`measurement-final.json`, `independent-verdict.md`, and `cleanup.json`.
The exact rollout is
`/Users/dthurn/.codex/sessions/2026/09/16/rollout-2026-09-16T14-30-37-01a0ac20-f948-70c0-aa10-94235d0b9a8f.jsonl`.
These are diagnostic evidence, not runtime state or dependencies of the design.

The temporary MCP configuration was removed and only the exact probe server
processes were terminated. The disposable native task remains as evidence.
Production workflow state and services were not changed.
