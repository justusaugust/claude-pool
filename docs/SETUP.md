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

## 3. Name the accounts

Edit `~/.config/claude-pool/config.json`. The installer creates this example with the resolved Native executable path:

```json
{
  "native_claude": "/absolute/path/to/native/claude",
  "accounts": [
    {"name": "personal", "label": "Personal", "alias": "personal"},
    {"name": "work", "label": "Work", "alias": "work"}
  ]
}
```

Names and aliases use lowercase letters, digits and hyphens. Labels are display text. Names must match credential filenames; aliases cannot collide with pool commands. Account count is configurable. Optional fields are `state_dir`, `credential_dir`, `gateway_port`, `backend_port`, `control_port` and an IANA `timezone` such as `Europe/London`. Defaults use the current OS timezone.

Configuration is loaded when processes start. Changing pool membership requires a deliberate restart after active tasks finish; adding a credential file does not automatically enroll a new account in an already-running gateway.

## 4. Connect each subscription

```sh
claude-pool login personal
claude-pool login work
```

Each command starts Shunt's refreshable browser OAuth flow into the selected private folder. Confirm the actual account and organization in the browser. Do not paste credentials into an issue, command argument or chat. Login refuses an existing account filename rather than overwriting its refresh token. Reauthentication of an existing store should be handled deliberately with the original credential owner after its tasks finish.

Your Native login stays separate. A personal and Team subscription can share a user UUID but have different organizations; check the selected organization, not just the email address. Multiple aliases for the same real user/organization do not create additional quota.

Use one active backend owner for each refreshable store. Do not run independent backends or copy rotating refresh tokens across machines.

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
