# Setup

This is a source-built macOS alpha. Tested runtime work used Claude Code 2.1.300. The original implementation was validated on Apple Silicon; a second physical Mac, Intel, Linux and Windows have not been accepted.

## Requirements

- macOS with a logged-in graphical user session for user launchd services.
- Python 3.11+, Git, Rustup with Rust 1.96.1, Node.js 22.12+ and npm for the source build.
- Native Claude Code 2.1.300+ installed and authenticated through its ordinary browser flow. The installer checks this floor; the tested version is 2.1.300.
- At least one authorized subscription identity; two independently provisioned accounts to test switching. The Native owner's user/organization identity must be configured too.
- Optional: Tailscale on the Mac and phone for private phone Cockpit.

Use only accounts and organization access you are authorized to operate. The software does not change a provider's subscription policies.

## 1. Build the pinned backend

From the root of the downloaded source:

```sh
rustup toolchain install 1.96.1 --profile minimal
python3 scripts/build_backend.py
```

This fetches the exact upstream revision recorded in `backend/upstream.json`, checks and applies `backend/shunt.patch`, builds its dashboard and compiles Shunt with the `ui` feature and recorded Rust toolchain. Rust compilation uses two jobs. The generated binary is local build output and is excluded from Git. Review the pin and patch before building. This alpha does not download an unpinned “latest” binary.

## 2. Install

```sh
python3 install.py --native-claude "$(command -v claude)"
```

Run this before adding a shell function named `claude`. If that name already resolves to a wrapper, supply the actual Native executable path instead. The installer refuses existing `claude-pool` and `claude-native` launchers and an existing application installation; it never silently overwrites them.

Use the updater-managed Native launcher path when available. The installer preserves that symlink so later Native updates are picked up by both `claude-pool` and `claude-native`; it does not silently pin the symlink's current versioned binary.

Defaults:

| Purpose | Location |
| --- | --- |
| Application | `~/.local/share/claude-pool` |
| Launchers | `~/.local/bin/claude-pool`, `~/.local/bin/claude-native` |
| Non-secret configuration | `~/.config/claude-pool/config.json` |
| Private subscription credentials | `~/.shunt/accounts/claude` |
| Runtime metadata and usage history | `~/.local/state/claude-pool` |
| Service label | `io.github.claude-pool` |
| Loopback backend / gateway / controls | `18340` / `18341` / `18342` |

Application, launcher and configuration locations can be selected with installer flags. Runtime, credential directory, ports and Native path can be changed in configuration before starting a service. `CLAUDE_POOL_CONFIG` selects another configuration file; the generated launcher and launchd definition retain that selection. Do not move credentials or change ports beneath running sessions.

Add `~/.local/bin` to PATH if it is not already present.

## 3. Connect your accounts

Run one command per subscription:

```sh
claude-pool account add you@example.com --name personal --label Personal
claude-pool account add you@company.example --name work --label Work --organization "Company"
```

Or run `claude-pool account add` and enter your email when prompted. The name and shortcut default to the part before `@`; use `--name` to distinguish accounts with the same email prefix. `--label` selects the readable display name. No configuration editing is needed.

The command opens browser authorization. Confirm the selected account and subscription organization, then paste the complete code into the private macOS dialog (a hidden terminal prompt on other supported terminals). Never paste it into chat or a command argument. The CLI verifies the returned email and organization identity, stores refreshable credentials privately, registers the account and creates its session shortcut. `--organization` additionally requires the selected organization name to match.

With a running pool, enrollment hot-reloads backend membership without restarting Claude or changing existing session pins. The command waits for the backend's first quota reading and reports the actual status, including `limited` if exhausted. This can take up to five minutes. A pending result means readiness is still unconfirmed; rerunning the same command resumes with the saved login instead of opening another browser. If the pool is stopped, the account is saved for its next normal start.

```sh
claude-pool account list
claude-pool work                 # independently pinned session
claude-pool                     # automatic routing across all accounts
```

The Native login stays separate and must also be among your connected subscriptions. A personal and Team subscription can share a user UUID but have different organizations. Duplicate aliases for the same user/organization do not create extra quota and are rejected. Existing credential files are reused, never silently overwritten. Revoked credentials need deliberate reauthentication with their owner; rerunning setup does not replace them.

Use one active backend owner for each refreshable store. Do not run independent backends or copy rotating refresh tokens across machines.

## 4. Optional configuration

The installer creates `~/.config/claude-pool/config.json` with the Native executable path and an empty account list. Account setup maintains names, labels, aliases and verified plan labels there. Names and aliases use lowercase letters, digits and hyphens; aliases cannot collide with pool commands.

Optional settings are `state_dir`, `credential_dir`, `gateway_port`, `backend_port`, `control_port` and an IANA `timezone` such as `Europe/London`. Defaults use the current OS timezone. Set these before starting the service. The guided command handles live account enrollment; other service settings still require a deliberate restart after active tasks finish. Creating a credential file alone does not enroll it.

## 5. Validate, then use

```sh
claude-pool doctor
claude-pool usage --once
claude-pool --model opus --effort high
```

These first two commands can start the pool but generate no inference. Open `/cockpit` inside the new session. Confirm the account list, serving account and Native identity. Missing weekly readings must say “not reported.” Check that your ordinary skills, MCP tools and authorized connectors are available.

For a two-account acceptance test, use a disposable project and public fixture. Keep the Native login fixed, change only that QA session's account through `/cockpit`, and check the actual response model/effort. Artifact acceptance should create, edit and read the same owner-only fixture artifact before and after the switch. Do not assume a successful model response proves connector access or artifact persistence.

## 6. Make `claude` use the pool

After validating the explicit launcher, add this to your shell configuration:

```sh
claude() { command claude-pool "$@"; }
```

Reload that shell configuration or open a new terminal. `claude-native` continues to call the original executable directly. The installer never edits your shell files or replaces the updater-managed Native binary. Other tools launching `claude` directly do not inherit shell functions; use the explicit `claude-pool` executable there.

## Updates and uninstall

No in-place update of active sessions is promised. Keep the installed directory until its tasks finish. Changing source files does not update an already-loaded Mod. User-managed `/reload-plugins` can load an updated Mod at a suitable checkpoint; it does not update the gateway process.

Once tasks have finished:

```sh
claude-pool --stop
launchctl bootout "gui/$(id -u)/io.github.claude-pool"
python3 install.py --uninstall
```

Run the uninstall command from the retained source checkout, using `--prefix` if you chose another application directory. It refuses while the service is loaded or if a launcher changed. Configuration, credentials, usage history and Native Claude are retained. Remove the shell function yourself. No user chat is restarted or replayed by uninstall.

After reinstalling, reuse the existing credential names and run `claude-pool --start`. The deliberate-stop marker is retained until that explicit start; logging in again is not required for valid retained credentials.

For private phone access and its extra configuration, see [phone controls](PHONE.md). For stale readings and authentication failures, see [troubleshooting](TROUBLESHOOTING.md).
