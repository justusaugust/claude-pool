# Several machines

One pool can serve several machines (a Mac, a Linux server). One machine adds another over SSH. `claude-pool help device` prints the short version of this page. A person or an agent can follow it alone.

Tailscale is the only network path between machines. Nothing listens on a public address.

## Before you add a machine

- Both machines are in the same tailnet, and ssh from this machine to the target works with no password prompt (`ssh <ssh-target> true` must succeed). If it does not, create a key with `ssh-keygen` and install it with `ssh-copy-id <ssh-target>`.
- The target has Python 3.11 or newer. `add` installs claude-pool there from this machine's checkout.
- This machine has at least one long-lived account, because only long-lived tokens are copied. Run `claude-pool account add <email> --long-lived --name <name>` (it opens a browser; `--from-file` adopts a staged 0600 token file instead), then `claude-pool device sync` once devices exist.

## Add a machine

```sh
claude-pool device add <ssh-target> [--name <name>] [--cua] [--usage-owner] [--herdr-session <session>] [--herdr-units listed|all]
```

`<ssh-target>` is anything `ssh` accepts, for example `user@host` or an alias. The device name must match `[a-z0-9-]{1,40}`; by default it is the host part of the target, lowercased (`alex@example-vps.example` becomes `example-vps`). Pass `--name` to choose another.

Run the same command again to repair a half-finished machine: every step finishes only what is missing, and a step a machine cannot do is `skipped` with the reason. In order, `add` checks and fixes `reachable`, `native-claude`, `install`, `version`, `linger`, `shell`, `accounts`, `service`, `doctor`, `native-login`, `herdr`, `herdr-sessions`, `app`, `cua`, `conductor`, `readings`, `preview`. It prints one line per step, a `fix:` line under anything that needs work, and a "Needs you" list of manual steps.

Options:

- `--cua` also shares desktops through Cua (see Cua below).
- `--usage-owner` makes the device the one that polls usage. Only a refreshable login can read usage, so the device must serve every account from its own refreshable login first (`claude-pool account add <email> --name <name>` on it); otherwise `add` refuses the role, names the missing accounts and changes no owner. It also creates the Conductor on that device, only when none exists yet.
- `--herdr-session <session>` adds a herdr profile on this machine that opens that herdr session on the target (`herdr machine add <ssh-target> --label <name> --remote-session <session>`). The Conductor's instructions name the session. The session is also recorded for the target, so it comes back after a reboot there even while it is not running (see herdr sessions after a reboot).
- `--herdr-units all` also gives every herdr session that runs on the target right now a unit, so it comes back after a reboot. Without it (`listed`, the default) only sessions given with `--herdr-session`, now or earlier, get one; `check` names the other running sessions without counting them as missing.

Exit codes: 0 when no step is `missing`, `failed` or `unreachable`; manual steps never block. 2 when a step needs work: read its `fix:` line, run it, and run `add` again. A Linux device whose linger needs root is `failed` (so exit 2) until the root command in "What only a human does" has run: `add` first tries `loginctl enable-linger` without root.

If a device prints `outdated launcher (no device support)`, its claude-pool is older than device commands. Run `claude-pool device add <ssh-target>` from this machine. `missing` means no launcher is installed there: the same command. The pool never hands device words to an old launcher, because it would take them as a Claude prompt.

## Check, list, sync, switch

```sh
claude-pool device check [name] [--probe] [--json]
claude-pool device list
claude-pool device sync [--force-registry]
claude-pool device switch [--wait]
```

`check` changes nothing; it exits 2 when any step needs work. `--probe` sends one tiny real Claude request per long-lived account. `sync` merges the registry with every device row by row (a change made while a device was unreachable is kept, a removal travels as a tombstone that names the removed token) and copies the long-lived tokens. If `check` says `registry diverged`, run `sync`; only if a list is still wrong afterwards, run `sync --force-registry` on the device whose list is right. A device that receives a removal moves the token aside to `credentials/.retired`, and keeps it in service when it holds another token than the removed one. `switch` restarts the local pool to apply a pending account change, and only when no request is in flight.

## What each check proves

| check | proves | fix |
| --- | --- | --- |
| `reachable` | ssh to the device works without a password prompt | ssh <ssh-target> true must work without a password prompt (BatchMode) |
| `native-claude` | Native Claude Code is installed | curl -fsSL https://claude.ai/install.sh \| bash on the device |
| `install` | claude-pool is installed and knows the device commands | claude-pool device add <ssh-target>; if it says Python 3.11 or newer is missing, install Python 3.11 or newer on the device first |
| `version` | the device runs the same claude-pool sources as this one (each platform builds its own backend) | claude-pool device add <ssh-target> |
| `linger` | Linux services survive logout (macOS: skipped) | claude-pool device add <ssh-target> tries it without root; if it stays failed, a root user runs sudo loginctl enable-linger <user> on the device |
| `shell` | the claude shell function sends claude through the pool | claude-pool device add <ssh-target> |
| `accounts` | registry, token fingerprints, reserve settings and account health match | claude-pool device sync; claude-pool device sync --force-registry on the device whose list is right; claude-pool account add <email> --long-lived --replace --name <name> on the device that first enrolled it, then device sync; claude-pool account reserve <name> --at <pct>; claude-pool device switch; ok with a name hint: claude-pool account rename <local-name> <registry-name> on that device |
| `service` | the pool service is running the current program (stale: launchd still runs the one from before an update) | claude-pool device add <ssh-target>; or ssh <ssh-target> .local/bin/claude-pool --start; stale, when no task runs: ssh <ssh-target> '.local/bin/claude-pool --stop && .local/bin/claude-pool --start' |
| `doctor` | claude-pool doctor on the device reports nothing | run ssh <device> .local/bin/claude-pool doctor and apply the fix it prints |
| `native-login` | Native Claude is logged in; on the device that runs the Conductor it must be an account the pool serves (`failed` otherwise), elsewhere another login is only a `manual` note, as it is when no Conductor is configured | claude-pool device add <ssh-target> and click Authorize; claude-native auth status if it cannot be read |
| `herdr` | this machine has a herdr profile for the device | herdr machine add <ssh-target> --label <name> |
| `herdr-sessions` | every herdr session registered for the device (add --herdr-session) comes back after a reboot (Linux devices only); other running sessions are only named | ssh <ssh-target> .local/bin/claude-pool device herdr-units --session <session>; to hand a running session to its unit now, while its agents are idle: ssh <ssh-target> .local/bin/claude-pool device herdr-units --adopt |
| `app` | the Mac that runs the Claude app lists and trusts the SSH environment | the Claude app clicks printed under Needs you |
| `cua` | the device shares its desktop through Cua and the machines can reach each other | see docs/DEVICES.md, Cua; macOS: switch on cua-spacesd in Accessibility, then claude-pool device add <ssh-target> --cua |
| `conductor` | exactly one Conductor runs, on the registry device, connected to Remote Control | ssh <device> .local/bin/claude-pool device conductor install; claude-pool device conductor move <device>; stop the hand-started Conductor (herdr session conductor) |
| `readings` | usage readings from the usage owner are fresh | ssh -o BatchMode=yes <owner> true must work from the device |
| `preview` | the preview tool is installed, links are HTTPS and every preview comes back after a reboot (Linux devices only) | claude-pool device add <ssh-target>; a root user runs sudo tailscale set --operator=<user> for HTTPS; previews that would not survive a reboot: ssh <ssh-target> .local/bin/preview restore |

`<device>` and `<ssh-target>` are the device name and its ssh target. Several fixes in a row mean the code uses different ones for different problems: the `fix:` line under the step says which applies.

## Remove a machine

```sh
claude-pool device remove <name> [--wait] [--uninstall]
claude-pool device remove <name> --forget
```

Removal removes the `claude` shell function there (claude-pool's block and a hand-written `claude() { command claude-pool exec "$@"; }` line that `add` accepted), disables and deletes the herdr session units claude-pool wrote there (running sessions keep running until the next reboot), stops the pool on the device, removes the long-lived token copies and the herdr profile, forgets the Cua Space and syncs the new registry to the other devices. The refreshable grant stays on its own machine, byte for byte. `--uninstall` also removes claude-pool from the device. A partly failed removal exits 1 and the device stays registered: fix what it prints and rerun `claude-pool device remove <name>`, which completes it.

It refuses, changing nothing, when:

- the device holds a long-lived token this machine has no matching copy of (the only copy): run `claude-pool device sync` first. Exit 2.
- tasks are running there: exit code 3. Use `--wait`, or rerun later.
- it is the Conductor device (move it first, see below) or the usage owner (make another device the owner with `claude-pool device add <other-target> --usage-owner`).

`--forget` is for a machine that is gone for good. It removes the device from this device's records and syncs that registry to the other devices. On the machine itself it only stops the previews, and only while it still answers (their enabled units would otherwise keep serving across reboots); when it does not answer, it prints the `preview stop --all` to run there later. Tokens already copied there remain valid: revoke them (see Tokens) and re-enroll. The internal verb `forget-accounts` does the account part on the device and runs only when its pool is stopped.

On a Mac, also remove the SSH environment in the Claude app by hand: Code tab, environment menu.

## What only a human does

- **One Native Authorize click per new machine.** `add` opens the claude.ai authorize page in this machine's browser and forwards the callback. Nothing is pasted. If that cannot work on a machine, `native-login` is `manual` and prints the exact commands to run there: `claude-native auth login`, open the link it prints, click Authorize.
- **macOS Accessibility for `cua-spacesd`.** System Settings, Privacy & Security, Accessibility: switch on `cua-spacesd`, then run `claude-pool device add <ssh-target> --cua` again.
- **Linux linger.** Services stop at logout unless linger is on. `add` tries `loginctl enable-linger` as your user; if the system refuses, a root user runs `sudo loginctl enable-linger <user>` once. `add` prints it and never runs it.
- **Tailscale operator for HTTPS preview links.** A root user runs `sudo tailscale set --operator=<user>` once. Without it, previews work over HTTP.
- **The Claude app on the Mac.** The `app` check is about the Mac that runs the Claude app: Code tab, environment menu, add the SSH environment, open it once and trust it. The check lists these clicks. It reads only the app's `ssh_configs.json`. When that file is absent or unreadable (newer app versions keep the list elsewhere), the check says `could not read the Claude app's environment list` instead of claiming the environment is missing: look under Recent environments after Code tab, New, and add it only if it is not there.

There is no sudo and no root step anywhere in `add`.

## Tokens and accounts

Refreshable accounts stay on the one machine that logged in; they are never copied. The only credentials copied between machines are long-lived tokens, only by `device` commands, through an SSH pipe into 0600 files. A token is identified by its fingerprint (the first 12 hex digits of the SHA-256 of the access token), which is what `check` compares.

An account is identified by its subscription (account and organization UUIDs), not by its name. Each machine may hold the same subscription under its own name, for example `team` on the VPS and `ai-vaerk-team-premium` on the Mac. Every device command matches accounts across machines by subscription and falls back to the name only when one side has no UUIDs; messages name both, as in `team (AI Værk Team, registry: ai-vaerk-team-premium)`. A synced long-lived token is kept under the local name of the subscription, so `device switch` replaces that machine's own login in place (alias and sessions stay) and never adds a second account. A copy that arrived under the registry name before this matching existed moves to the local name on the next sync, unless that name already holds a long-lived token; then `check` lists it and it stays (it is never deleted).

To give a subscription the same name on every machine, rename it where it differs: `claude-pool account rename <old> <new> [--wait]` on that machine. `device check` prints the exact command as a hint on its `accounts` line, for example `team (registry: ai-vaerk-team-premium)` with `ssh <target> .local/bin/claude-pool account rename team ai-vaerk-team-premium --wait`. Rename moves the login, the long-lived token, the config row, the runtime config, the reserve and local state (probe results, pulled readings, session pins); label and shortcut stay. It refuses a name that is already connected, that is not lowercase, or under which a token of another subscription is saved. A copy of the same token already under the new name (the stray copy from before) is removed only when another copy of that token stays; anything else is moved into the credential folder's `.retired`. A running pool is restarted to take the new name once the account and then the pool serve no request (`--wait` waits up to 30 minutes). A pool stopped with `claude-pool --stop` is just renamed. A name the registry holds a removal marker for is refused. If anything fails, everything is put back. When this machine enrolled the subscription, the registry row is renamed too; the other devices keep their own names. `account add <email> --long-lived --name <name>` refuses a name that differs from the registry's and prints the rename command; after the rename, `--name <registry-name>` works on that machine.

```sh
claude-pool account add <email> --long-lived --name <name>
claude-pool account add <email> --long-lived --replace --name <name>
claude-pool account remove <name> [--wait]
claude-pool account rename <old> <new> [--wait]
claude-pool account reserve <name> --at <pct>
claude-pool account reserve <name> --off
```

A long-lived token lasts one year and is inference only. `doctor` warns 30 days before it expires and fails 7 days before. Revoking a token is manual, in claude.ai settings. After a revoke or an expiry, devices report `reauthenticate`. The backend first only cools a rejected long-lived token, so the device that serves it probes a cooling long-lived account with one tiny real request at most every 15 minutes and reports `reauthenticate` in `device check` and `doctor` only when that account itself answered the probe with 401; a later success answered by that account, or a new token, clears it. Fix it on the device that first enrolled the account (it holds the old token and has a browser): `claude-pool account add <email> --long-lived --replace --name <name>`, then `claude-pool device sync`. If that device is gone and `--replace` fails with "No long-lived token is saved", enroll again without `--replace` on any machine with a browser, then `claude-pool device sync`.

`account reserve` makes an account the pool's last choice: it takes no new pool work from the given percent of its limit. `--off` returns it to normal. A missing all-model weekly window is normal for Team accounts.

## The usage owner and readings

One device is the usage owner (`add <ssh-target> --usage-owner`). It polls the provider. Every other device pulls the readings from it over SSH every 5 minutes, using the pool service, which cannot answer a password prompt. So every non-owner device needs passwordless ssh to the owner: on the device run `ssh-keygen` if it has no key, `ssh-copy-id <owner-ssh-target>`, and check `ssh -o BatchMode=yes <owner-ssh-target> true`. The `readings` check is `manual` until the first readings arrive (within 5 minutes). It fails when the readings have no row for an account, because the owner then serves no refreshable login for it.

## Conductor

The Conductor is one permanent Native Claude conversation that watches and prompts your herdr agents. It runs on exactly one device with that machine's Native login (never through the pool) and appears in the Claude iOS app under Remote Control. In the Claude app it is listed as "Conductor Claude". It works in `~/projects/conductor` on that device: its `CLAUDE.md` (claude-pool keeps a managed device block in it), `journal.md` and its own `bin/` and `board/` live there. The pool account with the same identity is reserved from 85% unless you set it otherwise.

```sh
claude-pool device conductor status
claude-pool device conductor move <device>
```

`move` refusals exit 1 and change nothing. `move` needs: the target is in the registry (`device list`), its Native login is the Conductor account, linger is on, and its pool service is running. Otherwise it refuses and changes nothing. If the old device is unreachable at the end, its Conductor is removed later and `check` reports it until then. A move starts a new conversation, so open the new "Conductor Claude" conversation in the Claude app (Code, environment of the device). `check` then says `ready on <device> in a new conversation; the old one could not be reattached` until you have.

On the Conductor device itself the service is managed with:

```sh
ssh <ssh-target> .local/bin/claude-pool device conductor install [--session-id <ID>] [--fresh] [--restart]
ssh <ssh-target> .local/bin/claude-pool device conductor uninstall
```

`install` is safe to rerun and reattaches the one conversation with `--session-id`; after three quick failures it starts a fresh conversation. `--session-id` adopts a Conductor you started by hand in herdr session `conductor`: stop that one first, then run the install line `check` prints. `--fresh` starts a new conversation. `--restart` restarts only the Conductor service, never the pool.

If `check` says `running on <device> but not connected to Remote Control`, turn Remote Control on in the Claude app, wait two minutes (the service retries by itself), then run `claude-pool device check <name>`. If it still says not connected, run `ssh <ssh-target> .local/bin/claude-pool device conductor install --restart`.

What Conductor Claude calls to see and act (bounded, redacted output):

```sh
claude-pool conductor snapshot [--json]       # sessions, agents, last line, previews, open PRs, machines, accounts, quick links, queue
claude-pool conductor events                  # watcher: one line per important change
claude-pool conductor tell <target> <task>    # prompt an agent, or queue the task until it is idle
claude-pool conductor tell --list [--json]    # the queued tasks: id, agent, age, failed attempts, text
claude-pool conductor tell --cancel <id>      # remove one queued task; an unknown id exits 1
claude-pool conductor journal add|show|prune  # append-only journal
```

`snapshot --json` also carries what the Conductor Board shows at a glance, so the board never works these facts out itself:

`tell` prints one line: `prompted <session>/<agent>` for an idle agent, or `queued for <session>/<agent> (id <id>)` for a busy one; `<id>` is the six-character id `tell --list` shows and `tell --cancel` takes. A queue file that cannot be read shows as empty, as in the snapshot.

- `generatedAt`: when the snapshot was taken (ms).
- `machines`: one row per registry device with `name`, `platform`, `self`, `reachable`, `poolRunning`, `versionMatches` (`null` = not known), `light` (`green`, `amber`, `red` or `grey`), `code`, `reason` (one short English sentence, empty when green), `fix` (the fix of the worst finding) and `checkedAt` (ms of the last `device check`, `null` if never). Red: not reachable, pool not running, or a failed step. Amber: a step that is missing or needs you, or another claude-pool version. Grey: never checked, no answer within 20 s, no ssh from this machine and no result shared yet, or (code `pool-stopped`) this machine's own service state could not be read. `code` is stable for a board to translate: `ok`, `unreachable`, `pool-stopped`, `version-mismatch`, `never-checked`, `not-checkable`, `failed:<step>`, `missing:<step>` or `manual:<step>`. The snapshot never runs `device check`: it reads the last result that `device check` and `device add` keep in `device-check-cache.json` (0600, state folder), checks this machine's pool locally and asks another device for `claude-pool device status` over ssh at most once per 10 minutes (cached in `device-probe-cache.json`, whether or not it answered; a `device check` from the last 10 minutes counts as one). `device check`, `device add` and `device sync` also send the whole check cache to every other device they reach (internal verb `device receive-checks`, JSON on stdin, no secrets; per device the newest result wins; only devices in the receiving registry are kept, and a `checkedAt` counts as at most one day ahead of the receiver's clock), so a device with no ssh back to your Mac still shows the Mac's own last check. A device's own row stays live: its reachability, pool and version are read on the spot.
- `accounts`: one row per account the local pool knows, from the same overview as `claude-pool usage --once` (no call per account): `name`, `label`, `alias`, `registryName` (`null` when the registry uses the same name), `fiveHourPct`, `weekPct`, `fableWeekPct` (`null` when not reported), `resetsAt` (`fiveHour`, `week`, `fableWeek` in ms or `null`), `reserveAt`, `kind` (`long-lived` or `refreshable`), `expiresAt` (long-lived only), `status` and `agents` (Claude sessions this machine's pool serves on it; left out when the pool does not answer).
- `quickAccess`: `{label, link, kind}`: the Cua desktops index first (`kind` `cua`), then every other preview as `preview list --json` reports it (`preview`), then `Conductor in der Claude-App` (`app`, https://claude.ai/code) when a Conductor is configured. The per-desktop shares on port 18501 and up are never listed: they open through the index.
- `queue`: the queued `tell` tasks: `id`, `target`, `text` (first 200 characters), `queuedAt` (ms), `attempts`.

## herdr sessions after a reboot

herdr saves each session's workspaces, folders and the conversation of every Claude pane in `~/.config/herdr/sessions/<session>/session.json`, and resumes those Claude agents (`claude --resume <id>`) when its server starts again. Something has to start the server at boot. On a Linux device, `add` writes one systemd user template unit, `~/.config/systemd/user/claude-pool-herdr@.service` (`herdr --session %i server`, restarted on failure), and enables an instance `claude-pool-herdr@<session>` for every session that has a `session.json` and was given with `--herdr-session` (with `--herdr-units all`, also for every session running at that moment). A throwaway session therefore never comes back at boot unless you asked for it. The `herdr-sessions` check is `ok` when each registered session is enabled; a running session nobody registered is named in its detail (`runs without a unit`) and never makes the check fail.

The unit sets `PATH` to what a login shell has (`~/.local/bin`, `~/bin`, `/usr/local/sbin`, `/usr/local/bin`, `/usr/sbin`, `/usr/bin`, `/sbin`, `/bin`), so an agent herdr resumes finds `claude-pool` behind the `claude` function and `python3` behind `claude-pool`, as it did in the ssh session that first started the server.

Enabling never starts a unit next to a running server, so nothing that runs is interrupted: a session started by hand (for example from another machine's herdr profile) keeps running, and its unit takes over at the next boot. A session that is registered but not running is started by its unit right away. A session that a unit claude-pool did not write already serves, for example `herdr-trial.service`, counts as served and is never touched; neither is that unit.

To hand a running session to its unit now instead of at the next boot:

```sh
ssh <ssh-target> .local/bin/claude-pool device herdr-units --adopt [--session <session>]
```

It refuses, changing nothing, while any agent of the session is working or waiting for an answer. Otherwise it stops that session's server by the process id that owns the session's socket (never by name, never another session), starts the unit and checks that the session lists the same workspaces and that each Claude agent came back with the same conversation. Every process running in that session's panes ends; conversations survive. herdr may resume the agents only once a client attaches: if the check says an agent did not come back, attach once with `herdr --session <session>` and run `claude-pool device check <name>`.

`device remove` disables and deletes only the units claude-pool wrote; the sessions keep running until that machine reboots.

To verify after a reboot, on the device:

```sh
systemctl --user status claude-pool-herdr@<session>      # active (running)
systemctl --user show -p Environment claude-pool-herdr@<session>   # the PATH above
herdr --session <session>                                # attach: the workspaces and Claude agents are back
```

Then, in one of its panes: `type claude` names the function that runs `claude-pool exec`, `command -v claude-pool` prints `~/.local/bin/claude-pool`, and `claude-pool usage --once` answers. If an agent is missing, attach once (herdr resumes agents when a client attaches) and run `claude-pool device check <name>` from the other machine.

## Cua

Cua lets machines see and operate each other's desktops. claude-pool never installs Cua. Install it with Cua's own installer from github.com/trycua/cua on both machines, check `cua --version` on both, then run `claude-pool device add <ssh-target> --cua` again. The host binds `<tailscale-ip>:3211` only, the peer registers it as a Space, and `cua mcp` is added for Claude. On macOS, grant Accessibility to `cua-spacesd`. The Cua token travels over a pipe only. If `check` says `see docs/DEVICES.md, Cua`, one of these steps is missing.

When a firewall lets only ssh through to a machine (so port 3211 cannot be reached and `cua spaces add` times out), `add --cua` reaches that machine's host through an SSH tunnel that claude-pool owns on the registering machine: a LaunchAgent `io.github.claude-pool.cua-tunnel.<device>` on macOS or a systemd user unit `claude-pool-cua-tunnel-<device>.service` on Linux, running `ssh -N -L 127.0.0.1:<port>:<tailscale-ip>:3211 <ssh-target>` with a port from 32300 to 32399. The Space is registered at `127.0.0.1:<port>`, bound to loopback only; no root is needed on either machine. `check` reports `linked both ways (<device> through an SSH tunnel)` and flags a tunnel that is not loaded or not running; `remove` deletes the tunnel and its Space. A refused token never opens a tunnel, and tunnels claude-pool did not create are never touched.

## Preview links

Preview runs on Linux devices (macOS devices are `skipped`); `add` installs the `preview` tool there plus a rule in the user's Claude instructions.

```sh
preview 5173 --name "landing page"
preview file docs/report.md
preview cua
preview list
preview stop <port>|<path>|cua
preview stop --all
preview doctor
preview restore
```

`preview file <path>` shares one file or one folder, read-only. Markdown opens as a page (add `?raw=1` for the source), images and PDFs open inline, other text is plain, anything else downloads. Dotfiles and dot folders (`.env`, `.git`, `~/.ssh`), anything a symlink points to outside the shared folder, the home folder itself and its ancestors, `/proc`, `/sys`, `/dev` and `/run` are refused. File shares listen on 127.0.0.1 and are exposed on the tailnet only, on ports 18400 to 18499. `preview doctor` says which backend is available. A preview answers only requests addressed to the device's tailnet name, its short name, its tailnet address or localhost with the preview's port; any other host gets `421`, so a web page cannot read a preview through a name it points at the tailnet address.

`preview cua` on a device with Cua gives one HTTPS link to every Cua desktop it knows (`cua spaces ls`), to see and operate them from the iPhone. The link opens a short list of the desktops (served on 127.0.0.1:18500 by `preview-cua-index.service`); each `direct:<host>:<port>` desktop gets its own share on port 18501 and up, which reaches only loopback or tailnet addresses. Tapping a desktop makes a fresh one-hour viewer link with `cua sandbox view` and opens it with audio off; the viewer ticket lives only in that redirect and is never printed, logged or stored. Only the Tailscale login that owns the device may open these links; anyone else, other tailnet members included, gets `403`. It needs HTTPS through Tailscale Serve and refuses the HTTP backend. Run it again after adding or removing a Space (each desktop keeps its port); stop everything with `preview stop cua`.

Previews survive a reboot: every preview, file share and Cua share is an enabled unit file in `~/.config/systemd/user` (Tailscale keeps the Serve entries), and `preview stop` disables and deletes exactly those files; previews an older release started come back only after `preview restore` has run once, which `preview doctor` and `device check` point out.

Without the Tailscale operator setting, links are HTTP and may be blocked by the server's firewall on some ports; `preview doctor` shows which ports pass. HTTPS works on any port.

## What never leaves a machine

Refresh tokens, passwords, authorization codes, Cua pairing tokens and any file with a `refreshToken`. Secrets never appear in command arguments, logs, `devices.json`, git or GitHub. Long-lived tokens move only through a pipe, into a 0600 file in a 0700 directory.
