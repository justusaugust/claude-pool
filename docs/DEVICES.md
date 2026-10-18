# Several machines

One pool can serve several machines (a Mac, a Linux server). One machine adds another over SSH, and `claude-pool help device` prints the short version of this page. Everything here is meant to be followed by a person or an agent alone.

Tailscale is the only network path between machines. Nothing listens on a public address.

## Add a machine

On a machine that already runs the pool:

```sh
claude-pool device add <ssh-target> [--name <name>] [--cua] [--usage-owner]
```

`<ssh-target>` is anything `ssh` accepts without a password prompt, for example `user@host` or an alias. Run the same command again to repair a half-finished machine: every step is idempotent and finishes only what is missing. A step a machine cannot do is `skipped` with the reason.

In order, `add` checks and then fixes: `reachable`, `native-claude`, `install`, `version`, `linger`, `shell`, `accounts`, `service`, `doctor`, `native-login`, `herdr`, `app`, `cua`, `conductor`, `readings`, `preview`. It prints one line per step, a `fix:` line under anything that needs work, and a "Needs you" list of manual steps.

Exit code 0 means no step is `missing`, `failed` or `unreachable`. Manual steps never block. Exit code 2 means read the `fix:` line and run it. Add `--cua` to share the desktop through Cua. Add `--usage-owner` to make the device the one that polls usage and runs the Conductor.

An older launcher on the device answers a device command with exit 86 (outdated) or 87 (none). The pool never hands device words to it, because it would take them as a Claude prompt. The fix is the same: `claude-pool device add <ssh-target>` from a device that is up to date.

## Check, list, sync, switch

```sh
claude-pool device check [name] [--probe] [--json]
claude-pool device list
claude-pool device sync [--force-registry]
claude-pool device switch [--wait]
```

`check` changes nothing and prints the table below for every device. `--probe` sends one tiny real Claude request per long-lived account. `sync` pushes the registry and the long-lived tokens to every device. If `check` reports `registry diverged`, run `sync --force-registry` on the device whose list is right. `switch` restarts the local pool to apply a pending account change, and only when no request is in flight.

## What each check proves

| check | proves | fix |
| --- | --- | --- |
| `reachable` | ssh to the device works without a password prompt | make ssh keys and Tailscale work, then rerun |
| `native-claude` | Native Claude Code is installed | curl -fsSL https://claude.ai/install.sh \| bash on the device |
| `install` | claude-pool is installed and knows the device commands | claude-pool device add <ssh-target> |
| `version` | the device runs the same build as this one | claude-pool device add <ssh-target> |
| `linger` | Linux services survive logout (macOS: skipped) | sudo loginctl enable-linger <user> as root on the device |
| `shell` | the claude shell function sends claude through the pool | claude-pool device add <ssh-target> |
| `accounts` | registry, token fingerprints, reserve settings and account health match here | the fix printed on the line: device sync, --force-registry, or account add --long-lived |
| `service` | the pool service is running | claude-pool device add <ssh-target> |
| `doctor` | claude-pool doctor reports nothing on the device | the fix doctor prints |
| `native-login` | Native Claude is logged in to an account the pool serves | claude-pool device add <ssh-target>, then the one Authorize click |
| `herdr` | this machine has a herdr profile for the device | herdr machine add <ssh-target> --label <name> |
| `app` | the Claude app lists and trusts the SSH environment (macOS only) | the Claude app clicks printed under Needs you |
| `cua` | the device shares its desktop through Cua, and this machine can reach it | macOS Accessibility for cua-spacesd, or docs/DEVICES.md, Cua |
| `conductor` | exactly one Conductor runs, on the registry device, connected to Remote Control | claude-pool device conductor move <device> |
| `readings` | usage readings from the usage owner are fresh | ssh -o BatchMode=yes <owner> true must work from the device |
| `preview` | the preview tool is installed and links are HTTPS (Linux only) | sudo tailscale set --operator=<user> as root, then device add |

## Remove a machine

```sh
claude-pool device remove <name> [--wait] [--uninstall]
claude-pool device remove <name> --forget
```

Removal stops the pool on the device, removes the long-lived token copies and the herdr profile, and unregisters the Cua Space. It never destroys the only copy of a token. The refreshable grant stays on its own machine, byte for byte, with its membership. `--uninstall` also removes claude-pool from the device. The device that runs the Conductor or is the usage owner is refused first: move the Conductor with `claude-pool device conductor move <other>`, or make another device the usage owner with `claude-pool device add <other-target> --usage-owner`.

`--forget` is for a machine that is gone for good. It drops the device from this device's records only and touches nothing on it. The internal verb `forget-accounts` does the account part on the device and runs only when its pool is stopped. Tokens already copied to a lost machine remain there: revoke them (see Tokens) and re-enroll.

In the Claude app on a Mac, remove the SSH environment by hand: Code tab, environment menu.

## What only a human does

- **One Native Authorize click per new machine.** `add` opens the claude.ai authorize page in this machine's browser and forwards the callback. Nothing is pasted. If that cannot work on a machine, the `native-login` step is `manual` and prints the exact commands to run there: `claude-native auth login`, open the link it prints, click Authorize.
- **macOS Accessibility for `cua-spacesd`.** System Settings, Privacy & Security, Accessibility: switch on `cua-spacesd`, then run `claude-pool device add <ssh-target> --cua` again.
- **Linux linger.** Services stop at logout unless a root user runs `sudo loginctl enable-linger <user>` once. `add` prints the command and never runs it.
- **Tailscale operator for HTTPS preview links.** A root user runs `sudo tailscale set --operator=<user>` once. Without it, previews work over HTTP.
- **The Claude app (macOS).** Code tab, environment menu, add the SSH environment, open it once and trust it. The `app` step lists these clicks.

There is no sudo and no root step anywhere in `add`.

## Tokens and accounts

Refreshable accounts stay on the one machine that logged in; they are never copied. The only credentials copied between machines are long-lived tokens, and only by `device` commands, through an SSH pipe into 0600 files. A token is identified by its fingerprint (the first 12 hex digits of the SHA-256 of the access token), which is what `check` compares.

```sh
claude-pool account add <email> --long-lived --name <name>
claude-pool account add <email> --long-lived --name <name> --replace
claude-pool account remove <name> [--wait]
claude-pool account reserve <name> --at <pct>
claude-pool account reserve <name> --off
```

A long-lived token lasts one year and is inference only. `doctor` warns 30 days before it expires and fails 7 days before. Revoking a token is manual, in claude.ai settings. After revoking or after it expires, devices report `reauthenticate`: run `account add <email> --long-lived --name <name>` again, then `claude-pool device sync`.

`account reserve` makes an account the pool's last choice: it takes no new pool work from the given percent of its limit. `--off` returns it to normal. A missing all-model weekly window is normal for Team accounts.

## The usage owner and readings

One device is the usage owner (`add <target> --usage-owner`). It polls the provider. Every other device pulls the readings from it over SSH every 5 minutes, so the device needs `ssh -o BatchMode=yes <owner> true` to work without a prompt.

## Conductor

The Conductor is one permanent Native Claude conversation that watches and prompts your herdr agents. It runs on exactly one device with that machine's Native login (never through the pool) and appears in the Claude iOS app under Remote Control as "Conductor". The pool account with the same identity is reserved from 85% unless you set it otherwise.

```sh
claude-pool device conductor status
claude-pool device conductor move <device>
```

`move` refuses a target whose Native login is a different account. The service reattaches its one conversation with `--session-id`. After three failed attempts it starts a fresh conversation and `device check` reports `fresh` until the next attach works.

What Conductor Claude calls to see and act (bounded, ANSI-free, redacted output):

```sh
claude-pool conductor snapshot [--json]       # sessions, agents, last line, previews, open PRs
claude-pool conductor events                  # watcher: one line per important change
claude-pool conductor tell <target> <task>    # prompt an agent, or queue the task until it is idle
claude-pool conductor journal add|show|prune  # append-only journal
```

## Cua

Cua lets machines see and operate each other's desktops. Claude pool never downloads or installs it. On each machine:

1. Install Cua from its official release and verify the package checksum or signature before running anything.
2. Unpack it in user space (for example under `~/.local`), put `cua` on the PATH of ssh sessions, and use no root.
3. Run `claude-pool device add <ssh-target> --cua`. The host binds `<tailscale-ip>:3211` only, the peer registers it as a Space, and `cua mcp` is added for Claude.
4. On macOS grant Accessibility to `cua-spacesd`.

The Cua token travels over a pipe only. If `check` says `see docs/DEVICES.md, Cua`, one of these steps is missing.

## Preview links

On Linux devices, `add` installs the `preview` tool and a rule in the user's Claude instructions. `preview <port>` gives a Tailscale link to a dev server; `preview file <path>` shares one file or one folder, read-only.

```sh
preview 5173 --name "landing page"
preview file docs/report.md
preview list
preview stop <port>|<path>
```

Markdown opens as a page (add `?raw=1` for the source), images and PDFs open inline, other text is plain, anything else downloads. Dotfiles and dot folders (`.env`, `.git`, `~/.ssh`), anything a symlink points to outside the shared folder, the home folder itself and its ancestors, `/proc`, `/sys`, `/dev` and `/run` are refused. File shares listen on 127.0.0.1 and are exposed on the tailnet only, with ports 18400 to 18499.

## What never leaves a machine

Refresh tokens, passwords, authorization codes, Cua pairing tokens and any file with a `refreshToken`. Secrets never appear in command arguments, logs, `devices.json`, git or GitHub. Long-lived tokens move only through a pipe, into a 0600 file in a 0700 directory.
