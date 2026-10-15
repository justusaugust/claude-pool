# Optional private phone Cockpit

This surface is experimental. The Mac must remain running and reachable. A physical phone acceptance test is still required; desktop responsive-browser tests alone do not establish iOS backgrounding or network recovery.

Install and sign in to Tailscale on the Mac and phone, then run:

```sh
claude-pool phone --status
```

This prepares a private control listener and records the authenticated Tailscale identity/hostname in runtime metadata. It does not automatically expose a port. Inspect existing Tailscale Serve routes first. If 8443 is unused, configure a private route to the configured control port (default 18342):

```sh
tailscale serve status
tailscale serve --bg --https=8443 http://127.0.0.1:18342
claude-pool phone
```

Do not enable Funnel for this route. Do not replace an existing Serve route. The application requires the authenticated identity headers supplied by [Tailscale Serve](https://tailscale.com/kb/1242/tailscale-serve); a direct public reverse proxy is not an equivalent deployment.

Start new Claude sessions after preparing phone configuration. `/phone` links the exact connected task. Existing sessions are observed read-only unless the user loads the session-control Mod; they are not migrated automatically.

Supported controls include targeted ordinary instructions, exact-request approval/denial for supported permission hooks and structured question answers. Unsupported Native dialogs remain in Native. Instructions cannot approve actions, and approvals cannot be reused for changed tool input or a new worker generation.

`claude-pool phone --chat` optionally opens a separate Native Remote Control overview with model-free `tasks`, `usage` and targeted `send` commands. It does not add Native Remote Control to the pooled workers; their detailed view and supported approvals remain in private web Cockpit. Keep this optional overview open only while needed.
