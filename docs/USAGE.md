# Usage

After shell setup, `claude` starts pooled sessions and preserves Native administrative commands. `claude-pool` is the explicit pool CLI; `claude pool …` is the same pool namespace. Model, effort, resume and continue arguments keep their existing behavior.

| Command | Behavior |
| --- | --- |
| `claude update` / `claude upgrade` | Update Native Claude Code through its original updater |
| `claude pool update` / `claude-pool update` | Update Claude Pool; keep active tasks and service running |
| `claude account add [email]` | Browser login, verified identity, automatic registration and live readiness check |
| `claude account list` | Connected account labels and session shortcuts |
| `claude usage` | Refresh account usage every five seconds; Ctrl-C closes the monitor |
| `claude usage --once` | Print one cached snapshot |
| `claude history` | Recent completed-response account switches and their reasons |
| `claude forecast-check` | Score saved forecasts against later real quota observations |
| `claude doctor` | Native Claude Code health check |
| `claude pool doctor` | Pool health and bounded recovery incidents |
| `claude work` | Start a new session pinned to your configured `work` alias |
| `claude work --continue` | Fork saved conversation into a new independently pinned session |
| `claude work --resume <id>` | Resume into a new fork, leaving the original session/pin alone |
| `claude respawn <id>` | Native worker respawn behavior |
| `claude pool respawn <exact-agent-id>` | Pool respawn with guarded automatic continuation |
| `claude-pool device add <ssh-target>` | Add or repair a machine; rerun is safe |
| `claude-pool device check [name]` | Report every check for a machine and the fix for each problem |
| `claude-pool device list` / `sync` / `switch` | Show devices; push registry and long-lived tokens; apply a pending change |
| `claude-pool device remove <name>` | Remove a machine safely (`--forget` for a lost one) |
| `claude-pool device conductor status` / `move <device>` | Show or move the one Conductor |
| `claude-pool help device` | Everything an agent needs to manage machines |
| `claude-pool account add <email> --long-lived` | Enroll a one-year token that every device can use |
| `claude-pool account remove <name> [--wait]` | Disconnect a subscription without interrupting requests |
| `claude-pool account rename <old> <new> [--wait]` | Rename an account on this machine (label and shortcut stay); `claude-pool help account` |
| `claude-pool account reserve <name> --at <pct>` / `--off` | Make an account the last choice from a percent of its limit |
| `claude-pool conductor snapshot` / `events` / `tell` / `journal` | Tools Conductor Claude uses for herdr agents |
| `claude-pool conductor usage [--json]` | Today's usage by project and request class (API price equivalent) and the brake stage |
| `claude-pool conductor urgent <project> [--hours N]` / `--off <project>` / `--list` | The owner waits on a project: the brake leaves its subagents alone for some hours |
| `preview file <path>` | Share one file or folder read-only over Tailscale |
| `claude-native update` | Update Native Claude itself through its own updater |
| `claude-native agents --json` | Native worker inventory |

Native commands such as `auth`, `plugin`, `mcp`, `agents`, `install`, `doctor`, `update` and `upgrade` run without starting or contacting the pool. `claude --help` keeps Native help and adds a short pool-command guide. `claude-native` remains a direct escape to Native Claude.

An account pin is a routing preference and retains quota/failure fallback. It is not a promise to spend an exhausted account. `/cockpit unpin` returns that session to automatic routing. Pausing an account through Cockpit affects the pool globally; inspect the scope before using a pause.

## From Codex or another agent

Call the explicit `claude-pool` executable. Shell functions named `claude` are usually absent in agent subprocesses. Check cached capacity with `claude-pool usage --once`, then start an owned named session with an explicit model and effort. Resume that exact session UUID for later work; an unqualified `--continue` can select another conversation. Keep normal Native permissions, never copy credentials into the prompt, and close only the session you created.

## Cockpit

`/cockpit` opens the account pane. The same design includes switch history, connected sessions, forecast and health panes. The above-prompt band summarizes the serving account and relevant usage. UI refreshes consume no model tokens.

Five-hour and weekly bars show percent **used**. The optional phone UI explicitly shows capacity **remaining**. A panel refresh is not a new provider observation: check the observation age. Cached provider polling and response headers update readings independently.

Forecasts use recent observed quota burn, not a sum of percentages across different plans. They need enough fresh observations and measured burn. Missing weekly windows, stale readings or insufficient signal remain visible; the displayed range is observed variability, not guaranteed runtime.

## Forecast and brake

The pool forecast is the time until every ready account reaches its limit at the current combined pace: the rest of all ready accounts (to the reserve limit of reserved accounts, idle accounts included) divided by their summed burn. Accounts burn in parallel, so their own times do not add up.

When that forecast does not reach the next five-hour reset of an account that can serve again, the brake starts new subagent and workflow conversations on Sonnet 5.5 instead of Opus or Fable (stage 1). Below half that time their effort is also capped at high (stage 2). The model is chosen at an agent's first request and kept for its lifetime, because caches and thinking blocks belong to one model; an agent first seen mid-conversation keeps its model. Main sessions, compaction, Conductor (`conductor-wecker`) and projects marked with `claude-pool conductor urgent` are never braked. If the API refuses a braked request, it is sent once more with the model the agent asked for. Every answered request is logged without content in `usage-ledger/<date>.jsonl` under the state directory (31 days).

## Quota exhaustion and recovery

Confirmed quota exhaustion is distinct from a missing reading, authentication failure or network outage. Confirmed weekly limits remain blocking until their known reset even if the utilization percentage is absent. Fable-only weekly limits do not block Opus.

When every usable account is confirmed exhausted, the same model step may wait and continue when capacity returns. If exhaustion races with that preflight, a rejected streaming request can wait with SSE pings and resend identical bytes before model output begins. Cancellation releases it. The gateway never replays partial model output or completed tools; Native owns its normal error/retry flow.

Background handoff and respawn keep Native conversation history and selected model/effort. Targeted respawn is an explicit restart of that worker. Continuation checks recorded state, avoids blindly repeating completed actions and stops at missing approvals. Unknown delivery is never automatically resent.

Closing Claude ends its opportunity to continue; this alpha does not resurrect exited sessions or guarantee a laptop survives a multi-hour wait across sleep/network changes.
