# claude-pool

One Claude Code workflow, backed by your connected subscription accounts.

Keep using the normal Claude Code terminal, tools and conversation. The local pool selects a serving subscription, keeps requests on it while it is eligible, and routes to another account when necessary. Cockpit shows account usage inside Claude; `claude usage` opens a live terminal monitor.

**macOS alpha.** The private implementation has real task-level validation. This package has also been source-built and installed from its exported archive in an isolated environment on the maintainer's Mac, using empty credential storage and no inference. A full second-machine OAuth acceptance run remains outstanding. There are no prebuilt downloads. Claude Code is installed separately.

## What it does

- Automatic, session-aware inference routing across explicitly configured accounts, with optional per-session account shortcuts.
- A Cockpit Mod with per-account five-hour, all-model weekly and separate Fable weekly readings, reset times, routing explanations, session activity, switch history and partial forecasts.
- A live `claude usage` view that refreshes every five seconds. `--once` prints a snapshot; redirected output stays a snapshot. These commands do not call a model.
- Stable Native claude.ai identity for connectors and artifacts, independent of the serving inference account.
- Background handoff and targeted worker respawn, with model/effort preservation and guarded automatic continuation.
- Waiting for confirmed quota resets while the same streaming request remains connected, before model output begins. A closed Claude process cannot be revived by this mechanism.
- Optional private phone Cockpit for supported approvals and targeted instructions, over Tailscale. It is a separate web interface; it does not enable Native Remote Control on a custom gateway.

## Start here

```sh
git clone https://github.com/justusaugust/claude-pool.git
cd claude-pool
```

Read [setup](docs/SETUP.md) to build the pinned backend, install without overwriting Native Claude, connect accounts and enable the `claude` shell function. Then read [usage](docs/USAGE.md), [compatibility](docs/COMPATIBILITY.md) and [security](docs/SECURITY.md).

```sh
claude                             # automatic routing, after shell setup
claude usage                       # live account monitor; Ctrl-C closes it
claude usage --once                 # one cached snapshot
claude work --model opus --effort high
claude work --resume <session-id>   # forks a new session with its own pin
claude-native                      # original Native Claude
```

`work` is an example alias you configure, not a built-in account. No paid API-key fallback is configured.

## Identity is separate from inference

Artifacts, connectors and their permissions belong to the Native login. Switching the inference subscription does not move an artifact or grant access to another account's mail or Drive. Intentionally changing the Native login can make the previous login's artifacts unavailable. Keep the Native owner's user/organization identity among your configured accounts so the gateway can validate it.

Plugins, skills and MCP tools still run in Native Claude. Their own authentication, permissions, organization restrictions and version requirements still apply. See the [tested compatibility boundaries](docs/COMPATIBILITY.md); support for every plugin is not claimed.

## Quota readings are observations

Usage bars show **percent used**. “Not reported” means the provider did not supply a reading; it means neither zero usage nor unlimited capacity. Confirmed weekly rejections are tracked separately even when the percentage is absent. Fable's weekly window is never substituted for all-model weekly capacity. The forecast is an estimate at recent pace, excludes unmeasured capacity and labels missing coverage.

## Development

Python uses the standard library. The backend is built from a pinned [Shunt](https://github.com/pleaseai/shunt) revision plus the included patch; see [upstream provenance](docs/UPSTREAM.md). Claude's own runtime generates the Mod SDK declarations locally.

```sh
python3 scripts/test.py
```

Loopback-server tests use temporary ports and fixture credentials. Do not run model or artifact acceptance tests against unrelated active tasks.

The Mods also have Native fixture tests. After installing the launchers, run `claude-native plugin test gateway/cockpit` from this source checkout, and similarly for `background-bootstrap`, `session-control`, `phone-hub` and `respawn-continuation`. These tests do not require a model request. Native generates its version-specific SDK declarations when a Mod loads interactively; those generated files are excluded from Git and source exports.

MIT for this integration. Shunt retains its upstream MIT/Apache-2.0 license notices. This is an independent project, not an Anthropic product.
