# Several machines

One pool can serve several machines (a Mac, a Linux server). One machine adds another over SSH. `claude-pool help device` prints the short version of this page. A person or an agent can follow it alone.

Tailscale is the only network path between machines. Nothing listens on a public address.

## Before you add a machine

- Both machines are in the same tailnet, and ssh from this machine to the target works with no password prompt (`ssh <ssh-target> true` must succeed). If it does not, create a key with `ssh-keygen` and install it with `ssh-copy-id <ssh-target>`.
- The target has Python 3.11 or newer. `add` installs claude-pool there from this machine's checkout.
- This machine has at least one long-lived account, because only long-lived tokens are copied. Run `claude-pool account add <email> --long-lived --name <name>` (it opens a browser; `--from-file` adopts a staged 0600 token file instead), then `claude-pool device sync` once devices exist.

## Add a machine

```sh
claude-pool device add <ssh-target> [--name <name>] [--cua] [--usage-owner] [--herdr-session <session>]
```

`<ssh-target>` is anything `ssh` accepts, for example `user@host` or an alias. The device name must match `[a-z0-9-]{1,40}`; by default it is the host part of the target, lowercased (`justus@personal-vps.example` becomes `personal-vps`). Pass `--name` to choose another.

Run the same command again to repair a half-finished machine: every step finishes only what is missing, and a step a machine cannot do is `skipped` with the reason. In order, `add` checks and fixes `reachable`, `native-claude`, `install`, `version`, `linger`, `shell`, `accounts`, `service`, `doctor`, `native-login`, `herdr`, `app`, `cua`, `conductor`, `readings`, `preview`. It prints one line per step, a `fix:` line under anything that needs work, and a "Needs you" list of manual steps.

Options:

- `--cua` also shares desktops through Cua (see Cua below).
- `--usage-owner` makes the device the one that polls usage. Only a refreshable login can read usage, so the device must serve every account from its own refreshable login first (`claude-pool account add <email> --name <name>` on it); otherwise `add` refuses the role, names the missing accounts and changes no owner. It also creates the Conductor on that device, only when none exists yet.
- `--herdr-session <session>` adds a herdr profile on this machine that opens that herdr session on the target (`herdr machine add <ssh-target> --label <name> --remote-session <session>`). The Conductor's instructions name the session.

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
| `accounts` | registry, token fingerprints, reserve settings and account health match | claude-pool device sync; claude-pool device sync --force-registry on the device whose list is right; claude-pool account add <email> --long-lived --replace --name <name> on the device that first enrolled it, then device sync; claude-pool account reserve <name> --at <pct>; claude-pool device switch |
| `service` | the pool service is running | claude-pool device add <ssh-target>; or ssh <ssh-target> .local/bin/claude-pool --start |
| `doctor` | claude-pool doctor on the device reports nothing | run ssh <device> .local/bin/claude-pool doctor and apply the fix it prints |
| `native-login` | Native Claude is logged in to an account the pool serves | claude-pool device add <ssh-target> and click Authorize; claude-native auth status if it cannot be read |
| `herdr` | this machine has a herdr profile for the device | herdr machine add <ssh-target> --label <name> |
| `app` | the Mac that runs the Claude app lists and trusts the SSH environment | the Claude app clicks printed under Needs you |
| `cua` | the device shares its desktop through Cua and the machines can reach each other | see docs/DEVICES.md, Cua; macOS: switch on cua-spacesd in Accessibility, then claude-pool device add <ssh-target> --cua |
| `conductor` | exactly one Conductor runs, on the registry device, connected to Remote Control | ssh <device> .local/bin/claude-pool device conductor install; claude-pool device conductor move <device>; stop the hand-started Conductor (herdr session conductor) |
| `readings` | usage readings from the usage owner are fresh | ssh -o BatchMode=yes <owner> true must work from the device |
| `preview` | the preview tool is installed and links are HTTPS (Linux devices only) | claude-pool device add <ssh-target>; a root user runs sudo tailscale set --operator=<user> for HTTPS |

`<device>` and `<ssh-target>` are the device name and its ssh target. Several fixes in a row mean the code uses different ones for different problems: the `fix:` line under the step says which applies.

## Remove a machine

```sh
claude-pool device remove <name> [--wait] [--uninstall]
claude-pool device remove <name> --forget
```

Removal removes the `claude` shell function there (claude-pool's block and a hand-written `claude() { command claude-pool exec "$@"; }` line that `add` accepted), stops the pool on the device, removes the long-lived token copies and the herdr profile, forgets the Cua Space and syncs the new registry to the other devices. The refreshable grant stays on its own machine, byte for byte. `--uninstall` also removes claude-pool from the device. A partly failed removal exits 1 and the device stays registered: fix what it prints and rerun `claude-pool device remove <name>`, which completes it.

It refuses, changing nothing, when:

- the device holds a long-lived token this machine has no matching copy of (the only copy): run `claude-pool device sync` first. Exit 2.
- tasks are running there: exit code 3. Use `--wait`, or rerun later.
- it is the Conductor device (move it first, see below) or the usage owner (make another device the owner with `claude-pool device add <other-target> --usage-owner`).

`--forget` is for a machine that is gone for good. It removes the device from this device's records and syncs that registry to the other devices, and touches nothing on the lost machine. Tokens already copied there remain valid: revoke them (see Tokens) and re-enroll. The internal verb `forget-accounts` does the account part on the device and runs only when its pool is stopped.

On a Mac, also remove the SSH environment in the Claude app by hand: Code tab, environment menu.

## What only a human does

- **One Native Authorize click per new machine.** `add` opens the claude.ai authorize page in this machine's browser and forwards the callback. Nothing is pasted. If that cannot work on a machine, `native-login` is `manual` and prints the exact commands to run there: `claude-native auth login`, open the link it prints, click Authorize.
- **macOS Accessibility for `cua-spacesd`.** System Settings, Privacy & Security, Accessibility: switch on `cua-spacesd`, then run `claude-pool device add <ssh-target> --cua` again.
- **Linux linger.** Services stop at logout unless linger is on. `add` tries `loginctl enable-linger` as your user; if the system refuses, a root user runs `sudo loginctl enable-linger <user>` once. `add` prints it and never runs it.
- **Tailscale operator for HTTPS preview links.** A root user runs `sudo tailscale set --operator=<user>` once. Without it, previews work over HTTP.
- **The Claude app on the Mac.** The `app` check is about the Mac that runs the Claude app: Code tab, environment menu, add the SSH environment, open it once and trust it. The check lists these clicks.

There is no sudo and no root step anywhere in `add`.

## Tokens and accounts

Refreshable accounts stay on the one machine that logged in; they are never copied. The only credentials copied between machines are long-lived tokens, only by `device` commands, through an SSH pipe into 0600 files. A token is identified by its fingerprint (the first 12 hex digits of the SHA-256 of the access token), which is what `check` compares.

```sh
claude-pool account add <email> --long-lived --name <name>
claude-pool account add <email> --long-lived --replace --name <name>
claude-pool account remove <name> [--wait]
claude-pool account reserve <name> --at <pct>
claude-pool account reserve <name> --off
```

A long-lived token lasts one year and is inference only. `doctor` warns 30 days before it expires and fails 7 days before. Revoking a token is manual, in claude.ai settings. After a revoke or an expiry, devices report `reauthenticate`. Fix it on the device that first enrolled the account (it holds the old token and has a browser): `claude-pool account add <email> --long-lived --replace --name <name>`, then `claude-pool device sync`. If that device is gone and `--replace` fails with "No long-lived token is saved", enroll again without `--replace` on any machine with a browser, then `claude-pool device sync`.

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
claude-pool conductor snapshot [--json]       # sessions, agents, last line, previews, open PRs
claude-pool conductor events                  # watcher: one line per important change
claude-pool conductor tell <target> <task>    # prompt an agent, or queue the task until it is idle
claude-pool conductor journal add|show|prune  # append-only journal
```

## Cua

Cua lets machines see and operate each other's desktops. claude-pool never installs Cua. Install it with Cua's own installer from github.com/trycua/cua on both machines, check `cua --version` on both, then run `claude-pool device add <ssh-target> --cua` again. The host binds `<tailscale-ip>:3211` only, the peer registers it as a Space, and `cua mcp` is added for Claude. On macOS, grant Accessibility to `cua-spacesd`. The Cua token travels over a pipe only. If `check` says `see docs/DEVICES.md, Cua`, one of these steps is missing.

## Preview links

Preview runs on Linux devices (macOS devices are `skipped`); `add` installs the `preview` tool there plus a rule in the user's Claude instructions.

```sh
preview 5173 --name "landing page"
preview file docs/report.md
preview list
preview stop <port>|<path>
preview stop --all
preview doctor
```

`preview file <path>` shares one file or one folder, read-only. Markdown opens as a page (add `?raw=1` for the source), images and PDFs open inline, other text is plain, anything else downloads. Dotfiles and dot folders (`.env`, `.git`, `~/.ssh`), anything a symlink points to outside the shared folder, the home folder itself and its ancestors, `/proc`, `/sys`, `/dev` and `/run` are refused. File shares listen on 127.0.0.1 and are exposed on the tailnet only, on ports 18400 to 18499. `preview doctor` says which backend is available. A preview answers only requests addressed to the device's tailnet name, its short name, its tailnet address or localhost with the preview's port; any other host gets `421`, so a web page cannot read a preview through a name it points at the tailnet address.

Without the Tailscale operator setting, links are HTTP and may be blocked by the server's firewall on some ports; `preview doctor` shows which ports pass. HTTPS works on any port.

## What never leaves a machine

Refresh tokens, passwords, authorization codes, Cua pairing tokens and any file with a `refreshToken`. Secrets never appear in command arguments, logs, `devices.json`, git or GitHub. Long-lived tokens move only through a pipe, into a 0600 file in a 0700 directory.
