# Upstream provenance

The inference engine is [Shunt](https://github.com/pleaseai/shunt), credited to its upstream contributors. This project adds the Native-auth bridge, launcher, Cockpit integration, forecast/history, continuity and optional phone controls.

`backend/upstream.json` pins revision `e147f9b9c13b7072c04b1330bd1e7fd61d63b4c5`, Rust 1.96.1 and the SHA-256 of `backend/shunt.patch`. The build script fetches that revision, verifies the patch and builds the dashboard-enabled backend. This is a pinned source build, not a claim that upstream's latest release contains these changes.

The patch contains the tested organization-aware account identity, per-window quota/rejection persistence, multi-account failover and account attribution changes, associated documentation/tests, and existing protocol/test compatibility fixes from the source used by the private implementation. Review the patch as part of releasing or changing the pin. Do not silently drop it by substituting a stock upstream binary.

The patch also lets account `priority` outrank the sticky session slot and the round-robin slot, so a later-priority (reserved) account is tried last while an earlier-priority account is available under its threshold.

The release patch also updates the UI lockfile's transitive `source-map-js` dependency from 1.2.1 to 1.2.2 for [GHSA-68fv-2mgg-jv7q](https://github.com/advisories/GHSA-68fv-2mgg-jv7q). No other dependency entry is changed by that fix. The admin UI is built into static assets; the deployed backend does not run a Node source-map service.

Shunt is dual-licensed MIT OR Apache-2.0. Copies of both upstream license texts are retained in `backend/`. This integration is MIT. Claude Code and its generated SDK are separate vendor dependencies; no Claude binary, Native credential store or generated SDK bundle is distributed here.
