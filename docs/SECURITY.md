# Security and data

The pool runs locally as your operating-system user (a launchd agent on macOS, a `systemd --user` unit on Linux). Shunt owns subscription credentials and token refresh. The gateway validates the Native caller against Anthropic's fixed profile endpoint, matches its user and organization to configured identities, then substitutes a private local client token before forwarding inference to Shunt. Native bearer tokens are not forwarded to other account endpoints or exposed to Cockpit.

Credentials are provisioned only by the browser OAuth flow into the selected private folder. Keep that directory at 0700 and credential/control-token files at 0600. Tokens, passwords and authorization codes do not belong in Git, issues, screenshots, demo videos, command arguments or model prompts. A label/path configuration is separate from credential content. Guided enrollment verifies both user and organization against Anthropic’s fixed profile endpoint, rejects duplicate subscriptions, and validates backend configuration before activating membership. Pasted authorization codes stay inside the owning local login process; raw child output is not logged.

The bridge binds to loopback; client and admin credentials are distinct. Do not expose the admin API or inference ports publicly. Mods run with the local user's privileges and can access session data: review the code before loading it. This is not isolation from hostile software running as the same OS user.

Saved usage history contains quota values, resets, availability and numerical forecasts. Session preferences and switch journals retain bounded routing metadata. They do not store model prompts, thinking text, signatures, connector bodies or credentials. Native Claude maintains its own conversation history separately. Optional phone controls hold recent text/tool summaries in bounded memory and private control metadata; they are not a public dashboard.

Private phone access requires Tailscale identity, same-origin writes, cookie/CSRF checks and exact request generations. A chat instruction cannot grant approval. Unknown delivery, changed tool inputs and unsupported prompts fall back to Native rather than granting lasting permissions.

The one-command installer and updater fetch public code from this repository and may build dependencies locally. Review downloaded code; this alpha does not provide signed binary releases. Releases are staged in separate directories, and retained files are not removed during updates. No live service control or Native session restart is part of an update.

For public releases, export this source directory only. Never publish the original development workspace, account store, runtime directory, Native configuration, raw task transcripts, private receipts or browser state. Demo material must use public fixture content and generic account labels.

No dedicated vulnerability-reporting channel is configured for this alpha. Contact the [repository maintainer](https://github.com/justusaugust) privately before disclosing a security issue. Never include secrets in public issues.

## Several machines

Long-lived tokens are one-year bearer tokens that are good for inference only. They do not rotate, so there is no refresh-token tripwire, and a copy on a lost machine stays valid until it expires or you revoke it in claude.ai settings (revocation is manual and weak: re-enroll and run `claude-pool device sync`). `device` commands copy them to every device through an SSH pipe into 0600 files in 0700 directories, never through arguments, logs, `devices.json` or git. Tokens are compared by fingerprint, the first 12 hex digits of the SHA-256 of the access token, never by value. A device never deletes a token it received: a replaced or removed one is moved to `credentials/.retired` (0600, 0700 folder) so that a token enrolled while another device was unreachable is never lost. Those files stay until you delete them; after revoking a token, delete its retired copies on every device.

Machines talk only over Tailscale. Cua binds the Tailscale address and preview proxies bind loopback or the Tailscale address, never 0.0.0.0. `preview file` shares are read-only, refuse dot paths, and listen on loopback plus the tailnet only. See [devices](DEVICES.md).
