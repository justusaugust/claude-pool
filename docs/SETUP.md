# Setup

This is a source-built macOS and Linux alpha. Tested runtime work used Claude Code 2.1.300. The original implementation was validated on Apple Silicon; the Linux port was built and fixture-tested on Debian 13 (x86_64, headless). A second physical Mac, Intel Macs, other distributions and Windows have not been accepted.

## Requirements

- macOS with a logged-in graphical user session for user launchd services, or Linux with systemd and a user service manager (`systemctl --user status` answers). For a server that should keep the pool running after you log out, a human with root runs `loginctl enable-linger <user>` once.
- Python 3.11+, Git, Rustup with Rust 1.96.1, Node.js 22.12+ and npm for the source build. Linux additionally needs a C compiler and linker for the Rust build (Debian: `build-essential`) and `ps` (procps); the installer fetches rustup into `~/.cargo` itself when it is missing.
- Native Claude Code 2.1.300+ installed and authenticated through its ordinary browser flow. The installer checks this floor; the tested version is 2.1.300.
- At least one authorized subscription identity; two independently provisioned accounts to test switching. The Native owner's user/organization identity must be configured too.
- Optional: Tailscale on the host and phone for private phone Cockpit.

Use only accounts and organization access you are authorized to operate. The software does not change a provider's subscription policies.

## 1. Install in one command

With Native Claude Code and Git already installed on macOS or Linux:

```sh
curl -fsSL https://raw.githubusercontent.com/justusaugust/claude-pool/main/install.sh | sh
```

The command downloads public source, checks Native compatibility, installs Python 3.11+ through Homebrew if needed, and builds the exact pinned backend. On macOS, missing Node/Rustup build tools can also be installed through Homebrew; without Homebrew, install the listed requirements yourself and rerun. On Linux, Node and npm come from your distribution or nodejs.org, and a missing Rust toolchain is installed by the official `rustup-init` into `~/.cargo` and `~/.rustup` without touching your shell files or needing root. Native Claude is never installed, updated or logged in by this installer.

The first build may take several minutes. Rust uses two jobs and runs at lower scheduling priority. Updates reuse the existing backend only when its upstream pin and binary checksum match; an altered patch is rejected. A failed download, build or staged-code check leaves the installed version selected.

The same command updates a managed installation. Review the script before executing downloaded code. A local equivalent after downloading the source is:

```sh
python3 install.py --bootstrap
```

## 2. Manual build and installation

For a reviewed checkout or source archive:

```sh
rustup toolchain install 1.96.1 --profile minimal
python3 scripts/build_backend.py
python3 install.py --native-claude "$(command -v claude)"
```

The build fetches the revision in `backend/upstream.json`, checks and applies `backend/shunt.patch`, builds the dashboard, and compiles Shunt with its pinned toolchain. It never substitutes an unpinned latest binary.

Use the updater-managed Native launcher path. The installer retains that symlink so Native updates continue to work. It refuses launchers it does not own; supply the real Native path if `claude` resolves to a wrapper. Installation does not overwrite the Native executable or edit shell startup files.

Defaults:

| Purpose | Location |
| --- | --- |
| Application | `~/.local/share/claude-pool` |
| Launchers | `~/.local/bin/claude-pool`, `~/.local/bin/claude-native` |
| Non-secret configuration | `~/.config/claude-pool/config.json` |
| Private subscription credentials | `~/.shunt/accounts/claude` |
| Runtime metadata and usage history | `~/.local/state/claude-pool` |
| Service label | `io.github.claude-pool` (macOS: `~/Library/LaunchAgents/<label>.plist`, Linux: `~/.config/systemd/user/<label>.service`) |
| Loopback backend / gateway / controls | `18340` / `18341` / `18342` |

Application, launcher and configuration locations can be selected with installer flags. Runtime, credential directory, ports and Native path can be changed in configuration before starting a service. `CLAUDE_POOL_CONFIG` selects another configuration file; the generated launcher and service definition retain that selection. Do not move credentials or change ports beneath running sessions.

Add `~/.local/bin` to PATH if it is not already present.

## 3. Connect your accounts

Run one command per subscription:

```sh
claude-pool account add you@example.com --name personal --label Personal
claude-pool account add you@company.example --name work --label Work --organization "Company"
```

Or run `claude-pool account add` and enter your email when prompted. The name and shortcut default to the part before `@`; use `--name` to distinguish accounts with the same email prefix. `--label` selects the readable display name. No configuration editing is needed.

The command opens browser authorization. Confirm the selected account and subscription organization, then paste the complete code into the private macOS dialog (a hidden terminal prompt on Linux and other supported terminals). Never paste it into chat or a command argument.

On a headless Linux host (SSH, no browser) the command prints the authorization URL under "If the browser did not open". Open that URL in a browser on another machine, sign in, copy the complete code the page shows, and paste it into the hidden prompt in the SSH terminal. The code is bound to that one login attempt; nothing else needs to reach the server, and no port forwarding is required. The CLI verifies the returned email and organization identity, stores refreshable credentials privately, registers the account and creates its session shortcut. `--organization` additionally requires the selected organization name to match.

With a running pool, enrollment hot-reloads backend membership without restarting Claude or changing existing session pins. The command waits for the backend's first quota reading and reports the actual status, including `limited` if exhausted. This can take up to five minutes. A pending result means readiness is still unconfirmed; rerunning the same command resumes with the saved login instead of opening another browser. If the pool is stopped, the account is saved for its next normal start.

```sh
claude-pool account list
claude-pool work                 # independently pinned session
claude-pool                     # automatic routing across all accounts
```

The Native login stays separate and must also be among your connected subscriptions. A personal and Team subscription can share a user UUID but have different organizations. Duplicate aliases for the same user/organization do not create extra quota and are rejected. Existing credential files are reused, never silently overwritten. Revoked credentials need deliberate reauthentication with their owner; rerunning setup does not replace them.

Use one active backend owner for each refreshable store. Do not run independent backends or copy rotating refresh tokens across machines.

## 4. Optional configuration

The installer creates `~/.config/claude-pool/config.json` with the Native executable path and an empty account list. Account setup maintains names, labels, aliases and verified plan labels there. Names and aliases use lowercase letters, digits and hyphens; aliases cannot collide with Native or pool commands.

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
claude() { command claude-pool exec "$@"; }
```

If you used the earlier function without `exec`, replace it with the function above. Reload that shell configuration or open a new terminal. `claude-native` continues to call the original executable directly. The installer never edits your shell files or replaces the updater-managed Native binary. Other tools launching `claude` directly do not inherit shell functions; use the explicit `claude-pool` executable there.

## Updates without interrupting tasks

`claude update` and `claude upgrade` retain their Native meaning: update Claude Code. Update the pool explicitly:

```sh
claude-pool update
# Or, after shell setup: claude pool update
```

Or rerun the one-command installer. Updates download source, reuse or build the backend, validate the staged files, and atomically select the new version. Config, credentials, history, session pins and Native login stay outside the versioned application files. A second concurrent update fails cleanly. Modified launchers are preserved and reported.

New commands and sessions use the selected CLI and Mods. Already-running sessions retain their code and files; no plugin reload, Native restart, drain, model call or service restart is requested. A gateway/backend change is **installed but not active** until the service next starts. The updater does not force that boundary. Retained versions consume disk space and are not automatically deleted while tasks may reference them.

New installations use a stable service entry point, so later natural starts select the new backend. An early alpha installation may still have a loaded launchd definition pointing at its original directory. The updater prepares its on-disk definition for the stable entry point but leaves the loaded job alone. The next login adopts it; to adopt it earlier, finish tasks and deliberately run `claude-pool --stop`, `launchctl bootout "gui/$(id -u)/io.github.claude-pool"` (Linux: `systemctl --user disable --now io.github.claude-pool.service`), then `claude-pool --start` once. Unmanaged development wrappers are never silently converted or overwritten.

To inspect the selected source version, read `build-info.json` in the application root or its `current` release. Source checkouts need the bootstrap installer rather than `claude-pool update`. A new version cannot change code already loaded by a Native session.

## Uninstall

Once tasks have finished:

```sh
claude-pool --stop
launchctl bootout "gui/$(id -u)/io.github.claude-pool"      # macOS
systemctl --user disable --now io.github.claude-pool.service  # Linux
python3 install.py --uninstall
```

Run the uninstall command from the retained source checkout, using `--prefix` if you chose another application directory. It refuses while the service is loaded or if a launcher changed. Configuration, credentials, usage history and Native Claude are retained. Remove the shell function yourself. No user chat is restarted or replayed by uninstall.

After reinstalling, reuse the existing credential names and run `claude-pool --start`. The deliberate-stop marker is retained until that explicit start; logging in again is not required for valid retained credentials.

## Linux notes

The pool runs as your ordinary user through `systemd --user`: the first `claude-pool` start writes `~/.config/systemd/user/io.github.claude-pool.service`, enables it and starts it; `claude-pool --stop` drains it and the unit does not restart after a deliberate stop (`Restart=on-failure`). The daemon receives SIGTERM alone (`KillMode=mixed`) so it can finish active responses before stopping the backend. Inspect it with `systemctl --user status io.github.claude-pool.service`; the service writes no log of its own, `claude-pool doctor` reports health. Without linger the user manager, and with it the pool, stops when your last session ends; running Claude tasks are children of your terminal, not of the service, and keep running until the pool is needed again. Credentials stay in `~/.shunt/accounts/claude` as 0600 files inside a 0700 directory, exactly as on macOS; no keyring is used. The Cockpit `/machine` pane reads `/proc` and needs no `lsof`.

For private phone access and its extra configuration, see [phone controls](PHONE.md). For stale readings and authentication failures, see [troubleshooting](TROUBLESHOOTING.md).
