# Security and data

The pool runs locally as the macOS user. Shunt owns subscription credentials and token refresh. The gateway validates the Native caller against Anthropic's fixed profile endpoint, matches its user and organization to configured identities, then substitutes a private local client token before forwarding inference to Shunt. Native bearer tokens are not forwarded to other account endpoints or exposed to Cockpit.

Credentials are provisioned only by the browser OAuth flow into the selected private folder. Keep that directory at 0700 and credential/control-token files at 0600. Tokens, passwords and authorization codes do not belong in Git, issues, screenshots, demo videos, command arguments or model prompts. A label/path configuration is separate from credential content. Guided enrollment verifies both user and organization against Anthropic’s fixed profile endpoint, rejects duplicate subscriptions, and validates backend configuration before activating membership. Pasted authorization codes stay inside the owning local login process; raw child output is not logged.

The bridge binds to loopback; client and admin credentials are distinct. Do not expose the admin API or inference ports publicly. Mods run with the local user's privileges and can access session data: review the code before loading it. This is not isolation from hostile software running as the same OS user.

Saved usage history contains quota values, resets, availability and numerical forecasts. Session preferences and switch journals retain bounded routing metadata. They do not store model prompts, thinking text, signatures, connector bodies or credentials. Native Claude maintains its own conversation history separately. Optional phone controls hold recent text/tool summaries in bounded memory and private control metadata; they are not a public dashboard.

Private phone access requires Tailscale identity, same-origin writes, cookie/CSRF checks and exact request generations. A chat instruction cannot grant approval. Unknown delivery, changed tool inputs and unsupported prompts fall back to Native rather than granting lasting permissions.

For public releases, export this source directory only. Never publish the original development workspace, account store, runtime directory, Native configuration, raw task transcripts, private receipts or browser state. Demo material must use public fixture content and generic account labels.

No dedicated vulnerability-reporting channel is configured for this alpha. Contact the [repository maintainer](https://github.com/justusaugust) privately before disclosing a security issue. Never include secrets in public issues.
