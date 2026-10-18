# Troubleshooting

**Which updater should I use?** `claude update` updates Native Claude Code. `claude pool update` or `claude-pool update` updates Claude Pool. If an older shell function still sends `claude update` to the pool updater, replace it with the Native-compatible function in setup; no running session needs to restart.

**An update is installed but the backend is still old.** This is deliberate: the updater never restarts the live service or reloads Native sessions. New commands use the selected release. Gateway/backend changes activate at the next service start; loaded Mods keep their old files until you choose to reload or start another session. Early alpha launchd definitions may need a deliberate reload after tasks finish.

**Linux: `systemctl --user` says it cannot connect.** The pool needs a user service manager. Log in through a login that starts one (SSH with pam_systemd does), check `echo $XDG_RUNTIME_DIR` is set, and for a server that should keep serving after logout ask a root user for `loginctl enable-linger <user>` once. The installer never changes this.

**Installation finds an existing launcher or directory it does not own.** It stops before replacing it. Use a separate `--prefix` and `--bin-dir` for an isolated test, or deliberately resolve ownership first. The private development wrapper is not a managed public installation.

**The build needs tools.** The one-command installer can use Homebrew for Python, Node and Rustup on macOS. Without Homebrew, install the requirements in setup and rerun. On Linux it fetches rustup into `~/.cargo` itself; Node/npm and a C compiler (`build-essential`) must already be installed. An unchanged checksum-verified backend is reused on updates.

**Account setup is waiting or was interrupted.** Rerun the same `claude-pool account add <email>` command with the same name. A saved login is reused; no existing credential is overwritten. The provider's first usage poll can take five minutes. Until it arrives, setup reports pending rather than claiming the account is ready.

**The browser selected the wrong account or organization.** Nothing is enrolled when email or requested organization checks fail. Keep the saved file private; do not paste it into support messages. Use the correct account name or deliberately resolve that saved login with its owner. The CLI never replaces an existing refresh token automatically.

**No browser opens on a server.** Expected over SSH: copy the printed authorization URL into a browser on any machine, finish the login there, and paste the shown code into the hidden terminal prompt. Do not forward ports or copy credential files between machines. If the hidden prompt never appears, something handled the URL locally: the login child drops `DISPLAY` and `WAYLAND_DISPLAY` in SSH sessions on Linux so `xdg-open` cannot block on a browser you cannot see; outside SSH, unset them yourself or set `BROWSER=true` for that command.

**Authorization shows a redirect error.** The guided CLI uses the registered manual-code browser flow from the start. Start a fresh `account add` flow and use its complete code in the hidden dialog; old codes and codes from another login are rejected.

**The banner says API Usage Billing or connectors disappear.** Start with the explicit `claude-pool` launcher. It retains Native OAuth and removes conflicting API-key/auth-token/provider overrides in its child environment. Bare/safe modes disable required integration. Do not add a fake API key or copy tokens into shell configuration. Verify the intended Native account with `claude-native auth status`.

**An artifact is not found after a switch.** Compare the Native login, not just the serving account in Cockpit. A changed Native login may lack access to the original owner's artifact. Restore the intended login deliberately or use its original owner session; inference routing cannot transfer it.

**The Artifact or connector tool is missing entirely.** Check Native feature availability, organization policy and any environment settings that disable feature-flag evaluation or nonessential traffic. Tool discovery can also be asynchronous. Changing the inference account does not authorize a missing connector or override Native feature policy.

**Weekly usage is not reported.** This is a provider observation gap. It is not 0% used or proof of unlimited capacity. A confirmed weekly rejection is recorded separately and blocks that account until reset. Fable weekly is a separate bucket.

**The monitor keeps changing but observations are old.** The UI refreshes more often than the provider cache. Look at checked/observation age. Offline readings are marked stale and retried; a changing clock does not mean a new quota reading.

**The pool is unavailable.** Run `claude-pool doctor`. The launcher can start its own launchd or systemd user service, so this is not a passive process-only diagnostic. Check port conflicts, configuration, backend build and private-file permissions. This alpha refuses a differing service definition rather than replacing it. Do not kill unrelated listeners or restart active Native tasks.

**A plugin update seems absent.** Installed files and loaded session code are different states. Use a user-managed `/reload-plugins` checkpoint or start a fresh disposable session. A tool-list change can require confirmation; do not force it past Native's check.

**Thinking resets when tools change on resume.** The launcher defaults to Native's forced tool discovery, inline tools and mid-conversation system support. Native owns the saved prefix, signatures, tool discovery and permission checks; the pool never rewrites signed history. Explicit environment overrides still win. Tested on Native 2.1.301 with Opus 5.5/high effort. Earlier conversations or explicitly disabled discovery can still require a Native reasoning reset. Native may retain an existing tool's recorded description/schema until compaction; a changed tool implementation must remain compatible with that recorded schema.

**Context7 needs authentication.** Open `/mcp` in Claude and authenticate Context7 through its browser flow. The official plugin uses OAuth; setting `CONTEXT7_API_KEY` does not authenticate that plugin. This login is separate from pool accounts and remains Native-managed.

**Cua stopped connecting after its app updated.** Check `cua daemon status --json` against the installed version before restarting anything. An old daemon may still serve another task. For new clients, the supported `cua mcp --embedded` mode uses the installed runtime without replacing that daemon. Verify with `/mcp` and a read-only `list_spaces` call. Cua Driver is a separate installation: use its `mcp-config` output for the absolute executable path, then grant CuaDriver Accessibility and Screen Recording in macOS settings. Do not restart active Claude sessions to apply these configuration changes.

**Resume changes another window's account.** Use `claude <alias> --resume <id>` to fork into a new independently pinned conversation. A plain `--resume` keeps Native behavior. Inspect that session's Cockpit pin before changing pool-wide pauses.

**A stream failed after a tool call.** The gateway does not blindly replay partial output or tool effects. Inspect the last step before retrying when Native cannot safely continue. An API error is not proof that a file write or external side effect did not complete.

**A reset wait ended when the terminal closed.** The waiting process must remain alive. This release does not recreate exited clients or guarantee sleep/network recovery across a long quota wait.

## Several machines

Start with `claude-pool device check`; each problem has a `fix:` line. See [devices](DEVICES.md).

**`reauthenticate` on every device.** A long-lived token was revoked or expired. Run `claude-pool account add <email> --long-lived --replace --name <name>` on the device that first enrolled the account (it holds the old token and has a browser), then `claude-pool device sync`.

**Usage readings are not fresh on a device.** Readings are pulled from the usage owner over SSH by the pool service, which cannot answer a password prompt. Make `ssh -o BatchMode=yes <owner> true` work from that device.

**Services stop when I log out of a Linux device.** Linger is off. A root user runs `sudo loginctl enable-linger <user>` once; `device add` only prints it.

**Two Conductors, or Conductor not ready.** Only one may run. `claude-pool device conductor status` names where; stop the extra one and run the fix it prints, or `claude-pool device conductor move <device>`.

**`registry diverged`.** Two devices changed their lists while they could not reach each other. Run `claude-pool device sync`: it merges both lists row by row. If a list is still wrong afterwards, run `claude-pool device sync --force-registry` on the device whose list is right; the other devices move the tokens it drops to `credentials/.retired` and never delete them.

**Preview links are HTTP only.** Tailscale Serve is not allowed for that user. A root user runs `sudo tailscale set --operator=<user>`, then `claude-pool device add <ssh-target>`.

**A device prints "outdated launcher (no device support)".** Its launcher is older than device commands (`missing` means none is installed). Run `claude-pool device add <ssh-target>` from an up-to-date device.
