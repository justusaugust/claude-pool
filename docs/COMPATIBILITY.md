# Compatibility and proof boundaries

The following task-level tests were run on the original macOS installation before portable packaging. They establish behavior of that tested implementation, not acceptance on every machine or with every plugin. Portable-launcher changes require separate validation.

| Surface | What was tested | What remains outside that evidence |
| --- | --- | --- |
| Native Mod | Cockpit account/history/session/forecast/health panes; bootstrap and session-control hooks | Every terminal, plugin combination and future SDK version |
| Other plugins/skills/MCP | Native inventory is retained; inference routing stays below tool execution | Universal compatibility, third-party authentication and custom background hooks |
| Opus and thinking | Real Opus 5.5/high responses; signed thinking crossed selected-account switches, handoff, respawn and scoped recovery without a reported thinking drop | All models and every future signature/provider policy |
| Artifacts | Create, edit and read the same owner-only artifact across account switches and targeted respawn | Public sharing, collaborator comments and an intentional Native-owner change |
| Quota during artifacts | Controlled synthetic quota rejection plus real inference/artifact operations | Deliberately exhausting real subscriptions |
| Gmail / Drive | Real, narrow, read-only searches across selected-account switches; empty results, no message/document bodies | Retrieval, writes or every connector |
| Stream recovery | Scoped disconnects before headers and within streamed tool JSON, with a real artifact task | Physical Wi-Fi shutdown, lost upload acknowledgement and arbitrary side-effect replay |
| Reset continuation | Real Native transport across a short synthetic weekly reset | Every account actually exhausted, multi-hour wait, sleep/wake |
| Live usage | Cached account readings, 5s redraw, stale-state/retry/resize and redirected-output handling | Exact weekly percentages when the provider omits them |
| Phone | Private web UI, two-worker approval/denial, targeted instructions, desktop-browser responsive checks | Physical iOS/Android acceptance and full Native history/dialog support |

Artifact and connector ownership is the Native user/organization. It is neither the inference-serving account nor an artifact copy created by the pool. Switching Native owner can break visibility to previous artifacts; the pool does not transfer ownership.

Mods are an official [Claude Code extension surface](https://code.claude.com/docs/en/plugins/mods/overview). This alpha was exercised with Native 2.1.300; it is not a promise that the minimum version that supports Mods includes every API it uses. Validate against the actual target version.

Native [Remote Control requirements](https://code.claude.com/docs/en/remote-control) exclude custom gateway hosts. Private phone Cockpit is a separate implementation, not Native Remote Control for pooled sessions. Native [gateway authentication](https://code.claude.com/docs/en/llm-gateway) and plugin policy remain applicable.
