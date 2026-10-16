# Usage

After shell setup, `claude` and `claude-pool` are equivalent. Native arguments, including model, effort, resume and continue, are forwarded.

| Command | Behavior |
| --- | --- |
| `claude update` | Stage and validate the latest source update; keep active tasks and service running |
| `claude account add [email]` | Browser login, verified identity, automatic registration and live readiness check |
| `claude account list` | Connected account labels and session shortcuts |
| `claude usage` | Refresh account usage every five seconds; Ctrl-C closes the monitor |
| `claude usage --once` | Print one cached snapshot |
| `claude history` | Recent completed-response account switches and their reasons |
| `claude forecast-check` | Score saved forecasts against later real quota observations |
| `claude doctor` | Pool health and bounded recovery incidents |
| `claude work` | Start a new session pinned to your configured `work` alias |
| `claude work --continue` | Fork saved conversation into a new independently pinned session |
| `claude work --resume <id>` | Resume into a new fork, leaving the original session/pin alone |
| `claude respawn <exact-agent-id>` | Target one background worker and queue guarded continuation |
| `claude-native agents --json` | Native worker inventory |

An account pin is a routing preference and retains quota/failure fallback. It is not a promise to spend an exhausted account. `/cockpit unpin` returns that session to automatic routing. Pausing an account through Cockpit affects the pool globally; inspect the scope before using a pause.

## From Codex or another agent

Call the explicit `claude-pool` executable. Shell functions named `claude` are usually absent in agent subprocesses. Check cached capacity with `claude-pool usage --once`, then start an owned named session with an explicit model and effort. Resume that exact session UUID for later work; an unqualified `--continue` can select another conversation. Keep normal Native permissions, never copy credentials into the prompt, and close only the session you created.

## Cockpit

`/cockpit` opens the account pane. The same design includes switch history, connected sessions, forecast and health panes. The above-prompt band summarizes the serving account and relevant usage. UI refreshes consume no model tokens.

Five-hour and weekly bars show percent **used**. The optional phone UI explicitly shows capacity **remaining**. A panel refresh is not a new provider observation: check the observation age. Cached provider polling and response headers update readings independently.

Forecasts use recent observed quota burn, not a sum of percentages across different plans. They need enough fresh observations and measured burn. Missing weekly windows, stale readings or insufficient signal remain visible; the displayed range is observed variability, not guaranteed runtime.

## Quota exhaustion and recovery

Confirmed quota exhaustion is distinct from a missing reading, authentication failure or network outage. Confirmed weekly limits remain blocking until their known reset even if the utilization percentage is absent. Fable-only weekly limits do not block Opus.

When every usable account is confirmed exhausted, the same model step may wait and continue when capacity returns. If exhaustion races with that preflight, a rejected streaming request can wait with SSE pings and resend identical bytes before model output begins. Cancellation releases it. The gateway never replays partial model output or completed tools; Native owns its normal error/retry flow.

Background handoff and respawn keep Native conversation history and selected model/effort. Targeted respawn is an explicit restart of that worker. Continuation checks recorded state, avoids blindly repeating completed actions and stops at missing approvals. Unknown delivery is never automatically resent.

Closing Claude ends its opportunity to continue; this alpha does not resurrect exited sessions or guarantee a laptop survives a multi-hour wait across sleep/network changes.
