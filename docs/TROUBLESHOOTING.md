# Troubleshooting

**An update is installed but the backend is still old.** This is deliberate: the updater never restarts the live service or reloads Native sessions. New commands use the selected release. Gateway/backend changes activate at the next service start; loaded Mods keep their old files until you choose to reload or start another session. Early alpha launchd definitions may need a deliberate reload after tasks finish.

**Installation finds an existing launcher or directory it does not own.** It stops before replacing it. Use a separate `--prefix` and `--bin-dir` for an isolated test, or deliberately resolve ownership first. The private development wrapper is not a managed public installation.

**The build needs tools.** The one-command installer can use Homebrew for Python, Node and Rustup. Without Homebrew, install the requirements in setup and rerun. An unchanged checksum-verified backend is reused on updates.

**Account setup is waiting or was interrupted.** Rerun the same `claude-pool account add <email>` command with the same name. A saved login is reused; no existing credential is overwritten. The provider's first usage poll can take five minutes. Until it arrives, setup reports pending rather than claiming the account is ready.

**The browser selected the wrong account or organization.** Nothing is enrolled when email or requested organization checks fail. Keep the saved file private; do not paste it into support messages. Use the correct account name or deliberately resolve that saved login with its owner. The CLI never replaces an existing refresh token automatically.

**Authorization shows a redirect error.** The guided CLI uses the registered manual-code browser flow from the start. Start a fresh `account add` flow and use its complete code in the hidden dialog; old codes and codes from another login are rejected.

**The banner says API Usage Billing or connectors disappear.** Start with the explicit `claude-pool` launcher. It retains Native OAuth and removes conflicting API-key/auth-token/provider overrides in its child environment. Bare/safe modes disable required integration. Do not add a fake API key or copy tokens into shell configuration. Verify the intended Native account with `claude-native auth status`.

**An artifact is not found after a switch.** Compare the Native login, not just the serving account in Cockpit. A changed Native login may lack access to the original owner's artifact. Restore the intended login deliberately or use its original owner session; inference routing cannot transfer it.

**The Artifact or connector tool is missing entirely.** Check Native feature availability, organization policy and any environment settings that disable feature-flag evaluation or nonessential traffic. Tool discovery can also be asynchronous. Changing the inference account does not authorize a missing connector or override Native feature policy.

**Weekly usage is not reported.** This is a provider observation gap. It is not 0% used or proof of unlimited capacity. A confirmed weekly rejection is recorded separately and blocks that account until reset. Fable weekly is a separate bucket.

**The monitor keeps changing but observations are old.** The UI refreshes more often than the provider cache. Look at checked/observation age. Offline readings are marked stale and retried; a changing clock does not mean a new quota reading.

**The pool is unavailable.** Run `claude-pool doctor`. The launcher can start its own launchd service, so this is not a passive process-only diagnostic. Check port conflicts, configuration, backend build and private-file permissions. This alpha refuses a differing service definition rather than replacing it. Do not kill unrelated listeners or restart active Native tasks.

**A plugin update seems absent.** Installed files and loaded session code are different states. Use a user-managed `/reload-plugins` checkpoint or start a fresh disposable session. A tool-list change can require confirmation; do not force it past Native's check.

**Resume changes another window's account.** Use `claude <alias> --resume <id>` to fork into a new independently pinned conversation. A plain `--resume` keeps Native behavior. Inspect that session's Cockpit pin before changing pool-wide pauses.

**A stream failed after a tool call.** The gateway does not blindly replay partial output or tool effects. Inspect the last step before retrying when Native cannot safely continue. An API error is not proof that a file write or external side effect did not complete.

**A reset wait ended when the terminal closed.** The waiting process must remain alive. This release does not recreate exited clients or guarantee sleep/network recovery across a long quota wait.
