# State machines

This file lists every explicit and implicit state machine in claude-pool: its states, invariants and transition table, the invalid states it can reach with evidence, and the fix groups, each linked to an issue. IDs are stable: automata A1 to A19, bugs F1 to F45, fix groups G1 to G12, invariants I1… per automaton. A bug is **confirmed** when a code path and a repro test (or recorded data) show it, and **possible** when only the code path does.

Line numbers refer to `main` at 92bf795 (after #35). The repro tests named below live in `tests/test_zustand_pool.py` and `tests/test_zustand_geraete.py`; they are not in the repo yet, and each fix adds its own. Accounts and devices are anonymized ("account A", "device A").

## Overview

| ID | Name | Kind | Risk | Confirmed bugs | Status |
|---|---|---|---|---|---|
| A1 | Account state and usability | implicit | high | F1, F2 | fixed in #35 |
| A2 | Request in the gateway (with session state) | implicit | high | F3, F4 | open (F1 path fixed in #35) |
| A3 | Account block | explicit | medium | F8, F9 | open |
| A4 | Brake: hold and waiting room | hold explicit, rest implicit | medium | F11 | open |
| A5 | Backend supervision and service restart | implicit | low | none | open (F12 possible) |
| A6 | Conductor supervision and code version | serve explicit, version implicit | medium | F13 | open |
| A7 | Conductor inbox, queue, waker | explicit | low | none | open (F16 possible) |
| A8 | Conductor alerts | explicit | high | F17, F18 | F17 fixed in #35, F18 open |
| A9 | Long-lived token probe | explicit | low | none | open (F20 possible) |
| A10 | Client hooks | implicit | medium | none | open (F21 possible) |
| A11 | Phone control | explicit | low | none | no bugs found |
| A12 | Install, update and running code | implicit | high | F31, F42 | open |
| A13 | Device registry and sync | merge explicit, flow implicit | high | F32, F33, F34, F35 | open |
| A14 | Long-lived tokens per device | implicit | low | none | no bugs found |
| A15 | Device state and traffic light | implicit | high | F30, F36, F37 | open |
| A16 | Account lifecycle | implicit | medium | none (F32 see A13) | open (F40, F43 possible) |
| A17 | Add and remove a device | implicit (step sequence) | medium | F38 | open |
| A18 | Cockpit session display | implicit, derived | low | none | open (F45 possible) |
| A19 | Phone hub and remote calls | explicit | low | none | open (F44 possible) |
| – | Trivial: heavy_queue, scratch, conductor_board caches, NativeAuth cache, journal, views and helpers | trivial | low | none | F22 possible |

## A1 Account state and usability (implicit)

**Location**: Shunt status per account, read in `gateway/bridge.py:307-360` (mapping `gateway/bridge.py:321-325`, Shunt's `capped` and `capped_fable` kept since #35 at `gateway/bridge.py:333`). Pool overlays: `paused_names` (`gateway/bridge.py:80`, Cockpit `gateway/bridge.py:777-782`), `blocked` (A3), reserve from `config.json` (`reserveAt`, `reserveHours`, `reserveTimezone`, `reserveSoft`), written to Shunt as `max_utilization` and `priority` (`gateway/routing.py:106-123`, `gateway/settings.py:67-69`, every minute `gateway/launch.py:226-229`). State files: `sessions.json` keys `paused`, `blocked`; `quota.json` (Shunt); `shunt.toml`. Predicates "account can work":

| Predicate | Location | Used for | Respects hard reserve cap |
|---|---|---|---|
| Shunt `available` | `backend/shunt.patch:254-265` | selection in Shunt | yes (`capped` excludes) |
| `routing.eligible`, `open_quota` | `gateway/routing.py:96-103` | `choose`, agent account | yes (`reserve_blocked`) |
| `routing.holds` (new in #35) | `gateway/routing.py:77-93` | shared by the three below | yes (hard reserve and Shunt `capped`) |
| `telemetry.capacity` | `gateway/telemetry.py:61-73` | quota wait in gateway and client hook, alert `konten-limit` | yes since #35 (before: no, F1) |
| `brake.standstill` | `gateway/brake.py:49-55` | brake "no ready account" | yes |
| `brake.returns_at` | `gateway/brake.py:30-36` | brake stage, next reset | yes since #35 (before: no, F2) |

**States** (per account): `ready`, `limited` (Shunt `near_quota` at 95 % or `capped` at the reserve cap), `cooling`, `paused` (Cockpit, block or `restore_pauses`), `reauthenticate`, `unknown` (no reading yet). On top: reserve active, inactive (outside `reserveHours`), soft; window `rejected` or 100 %.

**Invariants**: I1 At any moment all predicates agree on whether Shunt still chooses the account. I2 `capacity()` is `quota_wait` exactly when no account Shunt still chooses has quota. In particular `standstill()` true ⇒ `capacity()` not `ready`. I3 `returns_at(account)` is the moment from which Shunt chooses the account again. I4 An account that Shunt holds paused is in `paused_names` or `blocked` (see A3).

**Transition table**

| State | Event | Condition | Next state | Location |
|---|---|---|---|---|
| ready | reading or response header | window ≥ 95 % | limited (near) | Shunt, `gateway/bridge.py:323` |
| ready | reading | window ≥ `max_utilization` (hard reserve) | limited (capped), Shunt no longer chooses it | `gateway/routing.py:122`, `backend/shunt.patch:254-265` |
| limited | window reset | | ready | Shunt |
| ready | 429 without quota status, 401, 5xx | | cooling | `backend/shunt.patch:66-73` |
| * | Cockpit `pause` | account known | paused, in `paused_names` | `gateway/bridge.py:777-782` |
| paused | Cockpit `resume` | | ready, out of `paused_names` | `gateway/bridge.py:777-782` |
| * | 403 that blocks OAuth for the account's organization | | paused, in `blocked` (A3) | `gateway/bridge.py:1008`, `gateway/bridge.py:387-400` |
| unknown | first reading | | ready or limited | `gateway/bridge.py:325` |
| reserve active | hour change | outside `reserveHours` | reserve inactive (options without cap) | `gateway/routing.py:39-52`, `gateway/launch.py:226-229` |
| * | gateway restart | in `paused` or `blocked` | paused | `gateway/bridge.py:159-162`, `gateway/bridge.py:382-385` |

**Reachable invalid states**

**F1 `capacity()` counted an account at its reserve cap as free, so a weekly-limit 429 reached interactive sessions.** Confirmed; fixed in #35. Code path before #35: Shunt exhausts every account it may choose and passes on the 429 of the last one tried, with `x-shunt-account` (`backend/shunt.patch:1071-1097`). The gateway enters the quota wait only for streaming `/v1/messages` and only when `capacity()` is `quota_wait` (`gateway/bridge.py:1020-1023`). `capacity()` blocked an account only on a `rejected` window or at 100 %, but an account with a hard reserve stops at its cap (99 % with cap 0.99, 77 % with cap 0.75) and never reaches 100 %, so it returned `ready`. Not committed: `gateway/bridge.py:1064-1071` sends the 429 with Anthropic headers and Claude Code shows the weekly-limit dialog. Committed: `gateway/bridge.py:1062-1063` raises and `gateway/bridge.py:1144-1150` sends a framed `api_error`. State combination: one unpaused account limited by its hard reserve below 100 %; every account Shunt still chooses exhausted; an interactive, exempt or never-classified session (`gateway/brake.py:95-106`); `stream: true`. The brake held only background sessions in the same state, because `standstill` counts `limited` as exhausted: I2 violated. The client hook did not wait either: `gateway/background-bootstrap/hooks/register.ts:80-92` asks `/pool/capacity` (`gateway/bridge.py:827-841`), the same function. Evidence: in one incident two accounts sat at their caps, three at 100 % and one was blocked; the journal shows 429 plus `stream_interrupted` series on the main request, an interactive session hung in the weekly-limit dialog, and the pattern recurred right after every block probe (A3) and in series about 20 s apart. Repro: `KontoLage.test_F1_capacity_zaehlt_ein_von_seiner_reserve_gedeckeltes_konto_als_frei`, `Anfrage.test_F1_ein_wochenlimit_429_geht_an_eine_interaktive_sitzung_durch`.

**F2 `brake.returns_at` used the five-hour reset for an account at its reserve cap.** Confirmed; fixed in #35. Before #35, a weekly window under 100 % meant the five-hour reset counted as the return, but an account over its cap stays out until its weekly reset. `stage` takes the minimum of all returns (`gateway/brake.py:39-46`). I3 violated. Evidence: the usage history recorded `forecast.brake.resetAt` at the five-hour reset of the capped account, about 16 h before the pool actually resumed. Effect: the stage is computed against a reset that is too early, comes out too low, and the `bremse` alert names a wrong time (`gateway/conductor_alerts.py:82-85`). Repro: `KontoLage.test_F2_bremse_rechnet_mit_dem_5h_reset_eines_reservegedeckelten_kontos`.

Possible: `cooling` and `unknown` still count as free in `capacity()`. After a block release (A3) the account shows briefly as `unknown` or `ready`, which explains a `brake_wait settling` right after each release. Not confirmed as a separate bug.

**Risk**: high before #35. #35 added the shared predicate `routing.holds`; what remains is to have `routing.eligible` and the alerts use it too.

## A2 Request in the gateway (implicit)

**Location**: admission `gateway/bridge.py:808-868`, brake wait `gateway/bridge.py:878-920`, `proxy` `gateway/bridge.py:922-1166`. Runtime flags: `lease` with `waiting`, `cancelled`, `upstream`, `progress` (`gateway/bridge.py:234`); local `committed`, `sse`, `waited`, `hour_cache`, `short`, `complete`, `stream_error`. Session state in `sessions.json` under `sessions.<id>`: persistent `mode`, `pin`, `active`, `routing`, `history`, `profile`, `project`, `ownerAt`, `autoAt`, `interrupted`; in memory only `inFlight`, `reserved`, `serving`, `brakeWaiting`, `quotaWaiting` (dropped on load, `gateway/bridge.py:128-147`).

**States**: new, rejected, admitted, brake-waiting, reserved, sent, retry 400, retry 403, quota-waiting, forwarded, done, interrupted, cancelled, failed (502).

**Invariants**: I1 At most `max_requests` (32) requests are active, of these at most 28 non-main requests (`gateway/bridge.py:226-237`). I2 A waiting request holds no slot; it goes up only if I1 still holds afterwards. I3 A 429 for an exhausted pool never reaches Claude Code while the pool can wait (quota wait, `gateway/bridge.py:1020-1061`). I4 After a committed stream nothing is retried, only ended with a frame (`gateway/bridge.py:870-876`, `gateway/bridge.py:1144-1150`). I5 `inFlight` > 0 ⇔ `reserved` set; `serving` only with `inFlight` > 0; no `inFlight` after a restart (`gateway/bridge.py:1153-1163`, `gateway/bridge.py:134`). I6 A retry (400, 403) goes only if nothing reached Claude Code yet, and at most once.

**Transition table**

| State | Event | Condition | Next state | Location |
|---|---|---|---|---|
| new | request | token ok, known path, `admit` free | admitted | `gateway/bridge.py:808-867`, `gateway/bridge.py:226-237` |
| new | request | draining, backend 3 failed probes, 32 active, 28 helper requests, buffer full | rejected 503 | `gateway/bridge.py:863-866` |
| admitted | brake reason | messages, session, `stream`, waiting room free | brake-waiting (200 SSE, ping 10 s) | `gateway/bridge.py:948-955`, `gateway/bridge.py:523-539` |
| admitted | brake reason | waiting room full (24) | admitted, goes at once | `gateway/bridge.py:884`, `gateway/bridge.py:528` |
| brake-waiting | hold off, owner writes, 3 h reached | | admitted, committed, without new admission | `gateway/bridge.py:900-911`, `gateway/bridge.py:599-600` |
| brake-waiting | client gone, draining, error | | cancelled | `gateway/bridge.py:913-917` |
| admitted | main request with session | `reserve` ok | reserved (`inFlight`+1, `reserved`) | `gateway/bridge.py:971-982`, `gateway/bridge.py:642-668` |
| sent | 400 | `hour_cache` or short form | sent, original body, once | `gateway/bridge.py:996-1007` |
| sent | 403 with block code | `block_account` succeeds | sent, steering removed, once | `gateway/bridge.py:1008-1018` |
| sent | 429 | `stream`, `capacity` = `quota_wait` | quota-waiting (200 SSE, ping) | `gateway/bridge.py:1020-1037` |
| sent | 429 | `capacity` = `ready`, not committed | 429 to Claude Code (F1) | `gateway/bridge.py:1023`, `gateway/bridge.py:1064-1071` |
| sent | 429 | `capacity` = `ready`, committed | interrupted, framed `api_error` | `gateway/bridge.py:1062-1063`, `gateway/bridge.py:1144-1150` |
| quota-waiting | `capacity` no longer `quota_wait` | | sent, same body | `gateway/bridge.py:1038-1061` |
| quota-waiting | client gone, draining | | cancelled | `gateway/bridge.py:1039`, `gateway/bridge.py:1054` |
| sent | 200 | | forwarded, `serving` | `gateway/bridge.py:1064-1084` |
| forwarded | `message_stop` | no `stream_error` | done (ledger, `served`) | `gateway/bridge.py:1118-1128` |
| forwarded | error, stall 300 s | | interrupted | `gateway/bridge.py:1135-1150`, `gateway/bridge.py:213` |
| any | end | main request | `inFlight`−1, `interrupted` if unfinished | `gateway/bridge.py:1153-1163` |
| `inFlight` stored | gateway restart | | `interrupted: true` | `gateway/bridge.py:134` |

**Reachable invalid states**

**F3 A released brake wait goes up without admission and exceeds `max_requests`.** Confirmed (test). `gateway/bridge.py:600` sets `lease['waiting'] = False`; the request then counts as active in `active()` (`gateway/bridge.py:222-224`) without `admit` (`gateway/bridge.py:231`) having checked it. Up to 24 waits release at once when the hold goes off or the owner writes. A new interactive request then gets 503 "Pool request capacity reached" although 4 slots are reserved for it. I1 and I2 violated. Repro: `Anfrage.test_F3_freigegebene_bremswarte_ueberschreitet_die_anfragegrenze` (`max_requests = 1`, a background request waits, an interactive one runs, the brake releases, two requests are active).

**F4 Every 400 is retried as a cache or short-form rejection.** Confirmed (test). `gateway/bridge.py:996-1007` checks only the status. A 400 such as "prompt is too long" goes up a second time, the journal records `cache_fallback` or `short_label_fallback`, and `label_counted('refused')` counts wrong. Low cost, misleading diagnosis. I6 half kept. Repro: `Anfrage.test_F4_jedes_400_wird_als_cache_ablehnung_wiederholt`.

**F5 The quota wait holds a request slot.** Possible. `gateway/bridge.py:1020-1061` sets no `lease['waiting']`. Rare while F1 prevented the wait; after #35 waiting requests can hold slots until the client timeout (about 600 s, see F11) and push interactive requests into 503. Planned with G2.

**F6 A session's `quotaWaiting` disappears while a second request of the same session still waits.** Possible. Set at `gateway/bridge.py:1035`, cleared by the first finished request at `gateway/bridge.py:1052`. Display only.

**F7 Two overlapping main requests of one session share one `routing` object.** Possible. `reserve` stores `decision` as `state['routing']` and `context['routing']` (`gateway/bridge.py:663-666`); `gateway/bridge.py:1080-1081` changes it in place, so `served` can write a wrong reason ("upstream failover") into the switch history. Display and statistics only.

**Risk**: high, because the F1 transition lives here. A small state class for `lease` (one state instead of `waiting`, `committed`, `sse`) with a single re-admission point would prevent F3 and F5.

## A3 Account block (explicit)

**Location**: `gateway/bridge.py:34-47` (`BLOCKED_CODES`, `BLOCK_SECONDS` 6 h), `block_account` `gateway/bridge.py:387-400`, `release_blocks` `gateway/bridge.py:402-416`, `restore_pauses` `gateway/bridge.py:382-385`, called from `gateway/bridge.py:1008-1018` and every 30 s from `gateway/launch.py:248-250`. State: `sessions.json` key `blocked` (`{name: {until, code}}`), journal `account_blocked`, `account_unblocked`.

**States**: free; blocked (in `blocked`, paused in Shunt); probe (resumed in Shunt, no longer in `blocked`, no result yet); blocked and paused by hand.

**Invariants**: I1 An account paused in Shunt is in `blocked` or `paused_names`. I2 While an account is blocked, the pool sends it no request and passes no rejection with that code to Claude Code. I3 Every block ends with a new attempt, except under a manual pause.

**Transition table**

| State | Event | Condition | Next state | Location |
|---|---|---|---|---|
| free | 403 with block code | pausing in Shunt succeeds | blocked 6 h, request goes once more | `gateway/bridge.py:1008-1018`, `gateway/bridge.py:387-400` |
| free | 403 with block code | admin call fails | free, 403 to Claude Code | `gateway/bridge.py:394` |
| blocked | tick 30 s | `until` reached, no manual pause | probe, `account_unblocked` | `gateway/bridge.py:406-416` |
| blocked | tick 30 s | `until` reached, manual pause | paused by hand, entry gone | `gateway/bridge.py:408`, `gateway/bridge.py:413-415` |
| probe | first request to it | 403 with block code | blocked, `account_blocked` | as above |
| blocked | gateway or Shunt restart | | blocked (`restore_pauses`) | `gateway/bridge.py:160-162`, `gateway/launch.py:213` |
| blocked | Cockpit `resume` | | free in Shunt, blocked in the pool | `gateway/bridge.py:777-782` |

Evidence for the cycle: the journal shows `account_unblocked` followed one to two minutes later by `account_blocked`, every six hours.

**Reachable invalid states**

**F8 A block during the release is lost; the account stays paused in Shunt for good.** Confirmed (test with forced interleaving), not seen live. `release_blocks` resumes the account in Shunt (`gateway/bridge.py:411`) and only then removes it under the lock (`gateway/bridge.py:413-415`). If a 403 arrives in another thread in between, `block_account` pauses it again and rewrites `blocked[name]` (`gateway/bridge.py:393-398`, `fresh` false, so no journal). `release_blocks` then removes exactly that new entry. Shunt keeps the account paused, the pool knows no reason, nothing resumes it, and the alert reports no blocked account. I1 and I3 violated until a service restart. Repro: `Sperre.test_F8_eine_sperre_waehrend_der_freigabe_geht_verloren`.

**F9 A second rejected account of the same organization is not blocked; its 403 reaches Claude Code.** Confirmed (test), not seen live. After a block the request goes once more (`gateway/bridge.py:1013-1018`). If the next account answers with the same code, nothing checks it: `gateway/bridge.py:1019` journals and `gateway/bridge.py:1064-1071` forwards the 403; the account stays in the pool. Several pool accounts can share one organization, so one organization block hits all of them. Auto-mode classifier requests then fail (comment at `gateway/bridge.py:34-36`). I2 violated. Repro: `Anfrage.test_F9_ein_zweites_gesperrtes_konto_derselben_organisation_wird_nicht_pausiert`.

**F10 Cockpit `resume` on a blocked account resumes it in Shunt but leaves the entry in `blocked`.** Possible. `gateway/bridge.py:777-782`. The next request gets the 403 and `block_account` extends silently. The button has no effect.

**Risk**: medium. Making the probe an explicit state under the lock fixes F8, F10 and F18 together.

## A4 Brake: hold and waiting room (hold explicit, rest implicit)

**Location**: `Hold` `gateway/brake.py:63-88`; `stage`, `standstill`, `braking` `gateway/brake.py:39-60`; `interactive`, `exempt` `gateway/brake.py:95-106`; `brake_info` (60 s cache), `brake_hold`, `brake_reason` `gateway/bridge.py:481-513`; waiting room and `owner_typed` `gateway/bridge.py:523-606`; `brake_wait` `gateway/bridge.py:878-920`; tick 30 s `gateway/launch.py:245-247`. State: `sessions.json` key `brake` (`on`, `calmSince`), per session `ownerAt`, `autoAt`, in memory `brakeWaiting`; daily file `brake-waits/*.jsonl`; `brake.json` (`urgent`, written by `gateway/conductor_tools.py:864-872`).

**States**: hold off; hold on; hold on and calm since t. Per wait: waiting, then `released`, `owner_message`, `capped`, `cancelled`, `draining`, `failed`. Reason: `stage n`, `no ready account`, `settling` (`gateway/bridge.py:501`).

**Invariants**: I1 Hold on ⇔ the last measured probe braked or calm has lasted under 15 min. I2 A background request waits at most `MAX_WAIT` (3 h) in total and is then sent (`gateway/brake.py:23`). I3 Every wait writes exactly one line to `brake-waits` and one `brake_wait` to the journal; `brakeWaiting.requests` returns to 0. I4 Interactive, never-classified and exempt sessions never wait.

**Transition table**

| State | Event | Condition | Next state | Location |
|---|---|---|---|---|
| off | probe | stage ≥ 1 or standstill, measured | on, `brake_hold_on` | `gateway/brake.py:69-74`, `gateway/bridge.py:493-503` |
| on | probe | does not brake | on, calm since now | `gateway/brake.py:76-77` |
| on, calm | probe | brakes | on, calm reset | `gateway/brake.py:71-73` |
| on, calm | probe | does not brake, calm ≥ 900 s | off, `brake_hold_off` | `gateway/brake.py:78-79` |
| * | probe | not measurable | unchanged | `gateway/bridge.py:489`, `gateway/bridge.py:497` |
| * | restart | | from `sessions.json` | `gateway/brake.py:85-88`, `gateway/bridge.py:163` |
| waiting | check every 30 s | hold off or project urgent | `released` | `gateway/bridge.py:905-911` |
| waiting | check every 2 s | a person wrote in the session | `owner_message` | `gateway/bridge.py:900-904`, `gateway/bridge.py:564-595` |
| waiting | check | 3 h since this HTTP request began | `capped` | `gateway/bridge.py:907-910` |
| waiting | client closes | | `cancelled` | `gateway/bridge.py:895`, `gateway/bridge.py:913-915` |

**Reachable invalid states**

**F11 Claude Code ends every wait after about 600 s; the 3 h cap never applies, the logical wait is unbounded, and `brake_wait` floods the journal.** Confirmed. `brake_wait` measures `since` per HTTP request (`gateway/bridge.py:885`). Claude Code ends the request at its timeout and sends it again; each retry is a new wait with a new `since`, a new line and a new journal entry (`gateway/bridge.py:538`). I2 and I3 violated for the logical request. Evidence: over nine days of `brake-waits`, 501 waits; 386 `cancelled`, 384 of them after at least 590 s (median 610 s, maximum 620 s); 0 `capped`. One subagent session waited 51 times in a row over 8.7 h. 137 of 256 journal lines were `brake_wait`, so the journal reached back only 27 h. Effect: the 3 h emergency release does not exist in practice; each retry resends the whole body (up to 128 MB) and is admitted again; important journal entries fall out sooner. Repro: data above. Test for the fix in G2.

**Risk**: medium. The hold is explicit already; a wait chain per session and class is missing.

## A5 Backend supervision and service restart (implicit)

**Location**: `supervise` `gateway/launch.py:220-280`, `restart_idle` `gateway/launch.py:348-363`, `check_backend` `gateway/bridge.py:248-261`, `recovery` and `health` `gateway/bridge.py:84`, `gateway/bridge.py:92`, rejection `gateway/bridge.py:231`, `gateway/bridge.py:865`. No file state besides the journal (`backend_*`, `restart_*`).

**States**: backend `starting`, `ready`, `unresponsive` with counter `failures`; recovery `ready`, `restarting`, `waiting for active responses`; `draining`.

**Invariants**: I1 With `failures` ≥ 3 new requests are rejected with 503 until a probe succeeds. I2 A restart never ends an active request (check and admission under one lock). I3 `recovery.state` describes the current recovery.

**Transition table**

| State | Event | Condition | Next state | Location |
|---|---|---|---|---|
| ready | probe every 10 s fails | | unresponsive, `failures`+1 | `gateway/bridge.py:252-256` |
| unresponsive | probe succeeds | | ready, `failures` 0, `backend_recovered` | `gateway/bridge.py:257-260` |
| unresponsive | `failures` ≥ 3 | active requests | `waiting for active responses`, `restart_deferred` | `gateway/launch.py:351-355` |
| unresponsive | `failures` ≥ 3 | none active | `restarting`, Shunt stopped and started | `gateway/launch.py:356-361`, `gateway/launch.py:265-273` |
| running | Shunt process exits | | `restarting`, `backend_exited` | `gateway/launch.py:263-264` |
| restarting | start and `restore_pauses` succeed | | `ready`, `backend_restarted` | `gateway/launch.py:268-274` |
| restarting | start fails | | wait 0.5 to 30 s, `restart_failed` | `gateway/launch.py:275-280` |

**F12 `recovery.state` stays `waiting for active responses` when the backend recovers by itself.** Possible. `gateway/launch.py:353` sets it; after `gateway/bridge.py:259` the loop continues (`gateway/launch.py:256-261`) and only `gateway/launch.py:272` sets `ready` again. `/pool/status` and the overview (`gateway/bridge.py:432`, `gateway/bridge.py:817`) show the stale value.

**Risk**: low. Resetting to `ready` on `backend_recovered` is enough.

## A6 Conductor supervision and code version (serve explicit, version implicit)

**Location**: `serve` `gateway/conductor.py:153-204`; `serve_service` with threads `deliver_until` and `waker_until` `gateway/conductor.py:207-218`; unit `io.github.claude-pool.conductor.service` through the stable `run.py` `gateway/conductor.py:226-230`; `install` with restart rule `gateway/conductor.py:354-406` (`391`, `398`); `run.py:9-20` resolves `current` at start; `update_install` `install.py:155-218` (`install.py:218`: "Running tasks and services were not restarted"); `--start`, `--stop` only for `SERVICE_LABEL` `gateway/launch.py:652-683`. State `conductor.json` (`pid`, `ready`, `startedAt`, `restarts`, `lastExit`, `sessionId`, `previousSessionId`, `fresh`).

**States** (serve): starting, ready (`Connected` or `Single session`), exited, pause (10 to 300 s), new conversation (`fresh`). Version: unit runs version X, `current` points to Y.

**Invariants**: I1 Every long-running pool process runs `current`, or all versions running at once read and write shared files in the same format. I2 The Conductor conversation is left only when it really cannot be resumed. I3 There is exactly one Conductor process per pool (`duplicates`, `gateway/conductor.py:269-290`).

**Transition table**

| State | Event | Condition | Next state | Location |
|---|---|---|---|---|
| starting | output contains `Connected` | | ready, `fresh` cleared | `gateway/conductor.py:180-182` |
| starting, ready | child exits | | exited | `gateway/conductor.py:176`, `gateway/conductor.py:190` |
| exited | | with session, never ready, under 30 s | failed attempt +1 | `gateway/conductor.py:193-194` |
| exited | | 3 failed attempts | new conversation (`sessionId` dropped, `previousSessionId`, `fresh`) | `gateway/conductor.py:195-198` |
| exited | | otherwise | pause, doubled up to 300 s, back to 10 s after 600 s uptime | `gateway/conductor.py:199-204` |
| version X | `claude-pool update` | | version X, `current` = Y | `install.py:215-218` |
| version X | `claude-pool --stop`, `--start` | | version X (only the pool service restarts) | `gateway/launch.py:655-683` |
| version X | `device conductor install` | unit or session changed, or `--restart` | version `current` | `gateway/conductor.py:382`, `gateway/conductor.py:391`, `gateway/conductor.py:398` |

**Reachable invalid states**

**F13 After `claude-pool update` and `--stop`/`--start` the Conductor unit keeps running old code.** Confirmed. Code path: the unit starts `run.py`, which resolves `current` once at start (`run.py:14-20`). `update_install` restarts nothing (`install.py:218`); `--stop`/`--start` handle only the pool service (`gateway/launch.py:655-683`); `install` sees no change because the unit points to the stable `run.py` (`gateway/conductor.py:226-230`, `gateway/conductor.py:382`). Evidence: the user service journal shows the pool service restarted three times after updates within one week while the Conductor unit never restarted; it ran next to at least three newer pool versions. No format break was found in the shared files (`conductor.json`, `conductor-queue.json`, `conductor-inbox.json`, `journal.md`, `wake.log`, `shunt.toml`), so the effect is that fixes to `serve`, `deliver_until` and `waker_until` silently wait for the next manual restart. I1 kept only by luck. Repro: to be written with G5.

**F14 A short network outage while resuming starts a new Conductor conversation.** Possible. Three fast failures suffice (`gateway/conductor.py:193-198`); with pauses of 10 and 20 s they fit in about 70 s. The old conversation is not tried again.

**F15 Going back to an older version drops newer journal codes and state keys.** Possible. the journal drops unknown codes on load (`gateway/diagnostics.py:49-60`) and writes the whole list back (`gateway/diagnostics.py:82`); `gateway/bridge.py:670-672` stores only known keys. A version before #34 would drop `account_blocked` and `blocked`, and a blocked account would not be paused at start.

**Risk**: medium. Recording the running version (in `conductor.json` and `/pool/status`, compared by `device check` and `doctor` with `build-info.json`) costs a few hours. Whether `update` restarts the unit itself is a behavior decision.

## A7 Conductor inbox, queue, waker (explicit)

**Location**: inbox `gateway/conductor_inbox.py:64-119` (`seq`, `delivered`, `generation`, `keys`, `waker`); waker thread `gateway/conductor_inbox.py:152-177`; `_deliver` `gateway/conductor_tools.py:473-513`; `deliver`, `deliver_until` `gateway/conductor_tools.py:610-637`; `tell`, `queue_items`, `tell_cancel` `gateway/conductor_tools.py:688-787`. Files `conductor-inbox.json`, `conductor-queue.json`, locks `*.lock`, `*.deliver.lock`, `conductor-events.json.watch.lock`.

**States**: inbox event pending (`seq` > `delivered`), delivered; waiter with generation g, superseded. Queue item: waiting, `attempts` 1 to 2, orphaned, delivered, undeliverable, cancelled.

**Invariants**: I1 Only the newest waiter delivers; each event exactly once (`gateway/conductor_inbox.py:112-118`, under lock). I2 The same key wakes at most once per 600 s (`gateway/conductor_inbox.py:70-71`). I3 A cancelled task is no longer delivered. I4 At most one deliverer at a time (deliver lock, the watcher has priority, `gateway/conductor_tools.py:620-626`).

**Transition table**

| State | Event | Condition | Next state | Location |
|---|---|---|---|---|
| (inbox) | `add` | key not seen in 600 s | pending | `gateway/conductor_inbox.py:64-75` |
| pending | waiter sees events | 5 s collected, own generation | delivered, hook exits 2 | `gateway/conductor_inbox.py:109-118` |
| waiter g | new Stop hook | | superseded (exits 0) | `gateway/conductor_inbox.py:101-108` |
| (queue) | `tell` to a busy agent | | waiting | `gateway/conductor_tools.py:709-713` |
| waiting | agent idle, prompt succeeds | | delivered (journal) | `gateway/conductor_tools.py:495-498` |
| waiting | prompt fails | 3rd attempt | undeliverable | `gateway/conductor_tools.py:499-503` |
| waiting | agent missing | | orphaned, undeliverable after 24 h | `gateway/conductor_tools.py:484-491` |
| waiting | `tell --cancel` | | cancelled | `gateway/conductor_tools.py:768-787` |

**F16 A cancelled task can still be delivered.** Possible. `_deliver` reads the list (`gateway/conductor_tools.py:477`), prompts outside the queue lock (`gateway/conductor_tools.py:495`) and applies changes afterwards (`gateway/conductor_tools.py:504-511`). `tell_cancel` takes only the queue lock, not the deliver lock, and reports "cancelled" while the prompt is already on its way. I3 violated.

**Risk**: low. Let `tell_cancel` take the deliver lock.

## A8 Conductor alerts (explicit)

**Location**: `update` `gateway/conductor_alerts.py:41-55`, `check` `gateway/conductor_alerts.py:57-85`, tick 30 s in the pool daemon `gateway/launch.py:241-244`. State `conductor-alerts.json` per kind (`active`, `announced`, `woke`), output to the Conductor wake log.

**States** per kind (`backend`, `konten-limit`, `anmeldung`, `gesperrt`, `bremse`): inactive; active and announced; active and silent.

**Invariants**: I1 At most one wake per kind per hour (`QUIET`, `gateway/conductor_alerts.py:18`, `gateway/conductor_alerts.py:46`). I2 An announced state ends with exactly one `vorbei` line (`gateway/conductor_alerts.py:51-52`). I3 Every state that needs the Conductor is reported, in particular a pool without a usable account (`konten-limit`). I4 `vorbei` means the cause is gone.

**Transition table**

| State | Event | Condition | Next state | Location |
|---|---|---|---|---|
| inactive | check | active, last wake ≥ 1 h ago | active, announced, line `an conductor` | `gateway/conductor_alerts.py:45-49` |
| inactive | check | active, last wake < 1 h ago | active, silent | `gateway/conductor_alerts.py:46` |
| active, announced | check | no longer active | inactive, line `vorbei` | `gateway/conductor_alerts.py:50-53` |
| active, silent | check | no longer active | inactive, no line | `gateway/conductor_alerts.py:50-53` |
| `konten-limit` | check | `capacity(...)` ≠ `ready` | active | `gateway/conductor_alerts.py:63-70` |
| `gesperrt` | check | `server.blocked` not empty | active | `gateway/conductor_alerts.py:74-81` |

**Reachable invalid states**

**F17 `konten-limit` never reported a standstill while an account sat at its hard reserve cap.** Confirmed; fixed in #35. `gateway/conductor_alerts.py:65` uses `capacity()`; because of F1 it was `ready` while any account sat at its cap, so the alert could not become active. I3 violated. Evidence: `conductor-alerts.json` never had a `konten-limit` key and the wake log had no such line, while the journal shows a standstill of about 27 hours (`brake_wait no ready account` until `brake_hold_off`). The Conductor learned of it only because a session hung in the limit dialog. Repro: `KontoLage.test_F17_alarm_konten_limit_meldet_den_stillstand`.

**F18 `gesperrt` ends and starts again on every probe; the same block wakes the Conductor every six hours.** Confirmed. `release_blocks` empties `blocked` for the probe (A3), `check` reads that as the end (`vorbei`), a minute later the first 403 blocks again, and since the last wake was 6 h ago it wakes again. I4 violated; I1 formally kept. Evidence: wake log pairs `vorbei` and `an` one minute apart, four times in one day, each followed by a Conductor wake with the same text. Repro: `KontoLage.test_F18_gesperrt_endet_und_beginnt_bei_jedem_neuen_versuch`.

**F19 A state that began silently is never reported later, even if it lasts hours.** Possible. `gateway/conductor_alerts.py:46` decides `announced` only on the start edge.

**Risk**: high before #35, because F17 disabled the only alert for an empty pool. With G1 merged, the remaining work is the probe phase for `gesperrt` (G3).

## A9 Long-lived token probe (explicit)

**Location**: `verdict`, `AuthProbe` `gateway/readings.py:130-185`, `reauthenticate` `gateway/readings.py:124-127`, findings `gateway/long_lived.py:117-180`. File `long-lived-probes.json` (per account `at`, `fingerprint`, `result`).

**States** per account: no probe, `unknown`, `ok`, `rejected`, each bound to the token fingerprint.

**Invariants**: I1 `rejected` only after a 401 from the probed account itself. I2 A new token resets the result. I3 At most one probe per 15 min and account.

**Transition table**

| State | Event | Condition | Next state | Location |
|---|---|---|---|---|
| * | tick 300 s | account `cooling`, 15 min since last probe | probe running (`claude-pool <alias> -p ...`) | `gateway/readings.py:175-178` |
| probe | result | own 2xx | `ok` | `gateway/readings.py:134-135` |
| probe | result | own 401 | `rejected` | `gateway/readings.py:136` |
| probe | result | otherwise | previous result or `unknown` | `gateway/readings.py:182` |
| * | token replaced | new fingerprint | old result dropped | `gateway/readings.py:174` |

**F20 The probe hardly reaches an account in state `cooling`.** Possible. The probe pins the account (`gateway/launch.py:743-749`), `routing.choose` honors a pin only for `eligible` accounts (`gateway/routing.py:173-174`), and Shunt orders cooled accounts last (steering prefers only available ones, `backend/shunt.patch:918-941`). While another account answers, the result stays `unknown`.

**Risk**: low. A probe path that bypasses Shunt's choice (admin endpoint or own header) would fix it.

## A10 Client hooks (implicit)

**Location**: `connect` and account handover `gateway/background-bootstrap/hooks/register.ts:17-50`, `waitForQuota` `:80-92`, `turn.step` `:104-138`; generation, tick and status `gateway/session-control/hooks/register.ts:13-93`; `gateway/respawn-continuation/hooks/register.ts:4-21`; `heavy_queue.py` through `limits.ts`.

**States**: hook not connected, connected; per step: quota wait, step running, step stopped. Respawn: delivered, not delivered, unknown. Phone hook: generation g, tick running.

**Invariants**: I1 Never repeat streamed output or tools (`register.ts:123-124`). I2 An unknown delivery status is never sent again (`gateway/respawn-continuation/hooks/register.ts:14-17`). I3 A short pool outage stops no agent.

**Transition table** (`gateway/background-bootstrap/hooks/register.ts`)

| State | Event | Condition | Next state | Location |
|---|---|---|---|---|
| not connected | `session.start` or step | token, session, pin confirmed | connected | `register.ts:98-107` |
| connected | step | `capacity` = `quota_wait` | quota wait (long poll) | `register.ts:84-88` |
| step running | error 429 | nothing streamed, wait returns `ready` | step once more | `register.ts:122-126` |
| step running | other error or `/pool/capacity` not ok | | step stopped ("Pool request stopped") | `register.ts:72`, `register.ts:128-133` |

**F21 A short outage of `/pool/capacity` ends an agent's step instead of waiting.** Possible. `gateway/bridge.py:839-840` answers 503 when the admin read from Shunt fails; `register.ts:72` throws and `register.ts:128-133` ends the step with `end_turn`. I3 violated as soon as Shunt does not answer for three seconds.

**Risk**: medium. A bounded retry on 503 is enough.

## A11 Phone control (explicit)

**Location**: `Controls` `gateway/control.py:27-260`, `gateway/control_hook.py:25-62`, `gateway/session-control/hooks/register.ts:29-49`. One file per `Controls(path)` (tasks without events, commands without text).

**States**: task with generation, `idle`, `working`, `offline`, `needs-native`, `retired`. Command `queued`, `delivering`, `accepted`, `unknown`. Request `pending`, `allow`, `deny`, `local`, `expired`, `consumed`.

**Invariants**: I1 A command is never handed over twice (`delivering` is saved before handover, `gateway/control.py:111-117`; a restart turns `queued` and `delivering` into `unknown`, `gateway/control.py:38`). I2 A decision applies only to the input confirmed by PreToolUse (digest, `gateway/control.py:179`). I3 A decision is consumed exactly once (`gateway/control.py:230-231`).

**Transition table**

| State | Event | Condition | Next state | Location |
|---|---|---|---|---|
| command `queued` | worker poll | same generation | `delivering` (saved) | `gateway/control.py:111-117` |
| `delivering` | acknowledgement | accepted | `accepted` | `gateway/control.py:119-125` |
| `delivering` | acknowledgement, restart, new generation | not accepted | `unknown` | `gateway/control.py:124`, `gateway/control.py:38`, `gateway/control.py:94-95` |
| request `pending` | decision on the phone | not expired, worker online | `allow`, `deny`, `local` | `gateway/control.py:199-221` |
| `pending` | 120 s | | `expired` | `gateway/control.py:229` |
| `allow`, `deny` | fetched by hook | | `consumed` | `gateway/control.py:230-231` |

No bugs found. **Risk**: low.

## A12 Install, update and running code (implicit)

**Location**: Fresh install `install.py:96-152`, update `install.py:155-218`, run with lock `.claude-pool.install.lock` `install.py:286-312`, `install.sh:28-31`; `claude-pool update` clones `main` and runs `install.py --bootstrap` (`gateway/update.py:18-32`). `run.py:9-20` resolves `current` once at start and `execv`s into `versions/<v>/gateway/launch.py`. Service: definition points to the stable `run.py` (`gateway/launch.py:61-68`), `install_service` `gateway/launch.py:82-108`, `--start`/`--stop` with marker `RUNTIME/maintenance` `gateway/launch.py:652-686`. Remote path: archive over ssh stdin, `install.py --bootstrap` on the target (`gateway/device_install.py:32-42`, `gateway/device_install.py:117-121`); `ensure_install`, `ensure_version` `gateway/devices_cli.py:640-657`; `ensure_service` `gateway/devices_cli.py:778-784`. Version: `version` with backend, `sourceVersion` sources only (`install.py:55-72`); `device_sync.build_version` reads `build-info.json` of the installed release on disk (`gateway/device_sync.py:122-126`); `service_stale` macOS only (`gateway/device_sync.py:81-96`). Files: `~/.local/share/claude-pool/{installation.json, run.py, current, versions/<v>/}`, `~/.local/state/claude-pool/plugins/<mod>-<version>-<hash>/` (hash over `connection.ts`, which contains the release path, `gateway/settings.py:73-79`, `gateway/launch.py:490-510`).

**States** (per device): Disk: `missing`, `legacy` (source install without `run.py`), `installed(v)` (`current` → `versions/v`), `staging` (`versions/.staging-*`, only during a run). Processes: pool daemon on release d, Conductor unit on c (A6, F13), sessions with Mods from `plugins/…-<hash of v>`. Service: `running`, `loaded, stopped` (marker), `not loaded`, `missing`. Valid: `installed(v)` with d = v and c = v. Reachable and invisible: d ≠ v.

**Invariants**: I1 After a successful `update`, `current` points to a complete, verified release (`install.py:191-192`); on any error everything stays as before. Holds. I2 At most one install run per prefix (`install.py:299-301`). Holds. I3 The version `device check` reports is the one the pool daemon runs. Violated (F31). I4 A release is not deleted while a process or Mod folder uses it. Holds only because nothing is ever deleted (F42).

**Transition table**

| State | Event | Condition | Next state | Location |
|---|---|---|---|---|
| missing | `install.sh`, `install.py --bootstrap` | launcher free, Native ≥ 2.1.300 | installed, prefix is the release | `install.py:128-143` |
| installed(v) | `claude-pool update` | clone works, new release u | `current` → `versions/u`; daemon stays on v | `gateway/update.py:26-29`, `install.py:200-216` |
| installed(v) | `update` | same release | unchanged ("already up to date"), only `sourceVersion` added | `install.py:193-199` |
| installed(v) | `update` | `versions/u` exists (going back) | `current` → `versions/u` after content check | `install.py:201-203` |
| legacy | `update` | | new `run.py` and launcher, `current` set, service file prepared | `install.py:205-214` |
| any | `update` | clone, check or backend fails | unchanged | `gateway/update.py:27-31`, `install.py:187`, `install.py:192` |
| daemon on d | `claude-pool --stop` | responses drain | stopped, marker set, unit stays loaded | `gateway/launch.py:658-683`, `gateway/launch.py:590-592` |
| stopped | `claude-pool --start` | | daemon on `current` | `gateway/launch.py:655-657`, `run.py:14-20` |
| daemon running | `--start` | | unchanged, no restart | `gateway/launch.py:97-108` |
| daemon on d < v | `device add` from another device | | disk updated, daemon stays on d, step `service` ok | `gateway/devices_cli.py:640-657`, `gateway/devices_cli.py:778-784` |
| daemon on d | service file with other `ProgramArguments` | macOS only, only the switch `launch.py` → `run.py` | `serviceStale` | `gateway/device_sync.py:81-96` |

**Reachable invalid states**

**F31 After `update` or `device add` the pool daemon runs the old release and nothing reports it.** Confirmed (code path; fixed by hand twice in operation). Code path: `gateway/update.py:29` → `install.py:215-218` → `device status` reports the disk as `version` (`gateway/device_sync.py:125`) → `check_version` compares disk with disk (`gateway/devices_cli.py:648-652`) → `check_service` is ok because `serviceStale` is true only when `ProgramArguments` change (`gateway/device_sync.py:95`), and they always point to `run.py` (`gateway/launch.py:63-64`). On Linux `serviceStale` is always false (`gateway/device_sync.py:86`). `device add` does not heal it: `ensure_service` starts only when nothing runs (`gateway/devices_cli.py:781-782`, `gateway/launch.py:100-103`). Effect: green light and `versionMatches` true while fixes wait for a manual restart. Same root as F13. Repro: to be written with G8.

**F42 Releases and Mod folders are never cleaned up.** Confirmed (inventory). One new folder per update (`install.py:200-204`), one Mod folder per release (`gateway/launch.py:490-510`); no code deletes. One device had 29 folders in `versions/` (1.5 GB) and 18 Mod folders. Do not delete blindly: running sessions call `versions/<old>/gateway/control_hook.py` through their Mod (`hookPath`), and daemon and Conductor run from their release.

**Risk**: high, because of F31. Tracking the running release per service and comparing it with `current` (with G5) costs a few hours.

## A13 Device registry and sync (merge explicit, flow implicit)

**Location**: Merge: `stamp`, `update`, `merge`, `receive` `gateway/registry.py:196-279`; `force` `gateway/registry.py:282-306`; lock `.devices.json.lock`. Flow: `receive_registry` (merge, tombstones, hooks) `gateway/device_sync.py:297-356`; `merge_from`, `push`, `pull`, `sync_one`, `sync_all` `gateway/device_sync.py:441-507`. Hooks: `reserve_hook` (`gateway/device_sync.py:28`, `gateway/accounts_cli.py:428-445`), herdr (`gateway/device_integrations.py:117-122`), Conductor (`gateway/conductor.py:582`). Triggers: `account add|remove|reserve|rename`, only when other devices are registered (`gateway/accounts_cli.py:1015-1024`, `gateway/launch.py:598-605`); `device add|remove|sync`; Conductor move; puller in the daemon on every third successful reading, about every 15 min (`gateway/readings.py:228-232`); self-check every 6 h, repairs reserves only (`gateway/launch.py:41`, `gateway/devices_cli.py:751`).

**States**: Per device: generation g, rows with their own generation, tombstones. Between two devices: `equal`, `behind`, `ahead`, `diverged` (same generation, other hash, `gateway/devices_cli.py:703`). Per receive: `unchanged`, `replaced`, `merged`; per hook `ok` or `failed`; per tombstone `removed`, `kept`, `kept_busy`, `kept_native`, `failed`. Per device and subscription: reserve `equal to registry`, `different`, `not in registry`.

**Invariants**: I1 Merge is commutative, associative, idempotent (`gateway/registry.py:1-7`). Holds. I2 After a successful sync every device carries the reserve of every registry row in its config. Violated (F33, F34). I3 An error on the receiver reaches the caller. Violated (F34). I4 A reserve applies on every device that serves the same subscription, or the difference is visible. Violated (F32). I5 A device on an older release accepts the parts of a newer registry it knows. Violated (F35). I6 A later change wins over an earlier one. Holds only causally through the generation (F39).

**Transition table**

| State | Event | Condition | Next state | Location |
|---|---|---|---|---|
| g | local change | valid, at most one usage owner | g+1, changed rows and tombstones at g+1 | `gateway/registry.py:218-230` |
| g | receive | valid, no unknown fields | max(g, g'); per row higher generation, on a tie the larger digest | `gateway/registry.py:243-279` |
| g | receive | unknown field | abort, nothing written | `gateway/registry.py:52-54`, `gateway/device_sync.py:304-305` (F35) |
| received | | registry changed | hooks run | `gateway/device_sync.py:351-355` |
| received | | registry unchanged | no hooks, even after a failure | `gateway/device_sync.py:351` (F33) |
| tombstone | each receive | account busy | `kept_busy`, next receive retries | `gateway/device_sync.py:333-334` |
| tombstone | each receive | last account or Native login | `failed`, stays | `gateway/device_sync.py:340-346` |
| push | receiver exits 0 | report with `failed` or hook `failed` | `ok` | `gateway/device_sync.py:446-466` (F34) |
| pull in the puller | any | | result dropped | `gateway/readings.py:229-232` (F34) |
| `account reserve x` | | x has a registry row | config, `shunt.toml`, registry g+1, sync | `gateway/accounts_cli.py:390-408` |
| `account reserve x` | | x without registry row | only config and `shunt.toml` here | `gateway/accounts_cli.py:403-406` (F32) |
| self-check | every 6 h | reserve differs from registry | `reserve_hook` runs | `gateway/devices_cli.py:678-692`, `gateway/devices_cli.py:751` |

**Reachable invalid states**

**F32 A reserve on an account without a registry row stays on the device where it was set; output and checks do not say so.** Confirmed (incident, test). Code path: `reserve` writes config and `shunt.toml` (`gateway/accounts_cli.py:399`), looks up the registry row by UUIDs (`gateway/accounts_cli.py:403-404`), finds none for an account with its own login, and leaves the registry unchanged. `sync_all` (`gateway/accounts_cli.py:1018`) then skips a device without ssh target: "skipped: no ssh target from this device (it syncs with this one itself)" (`gateway/device_sync.py:503`). That device pulls only the registry, which does not carry the reserve. `device check` does not see it: `_reserve_issues` compares registry rows only (`gateway/devices_cli.py:683-685`). Evidence: a reserve set on device B for a blocked account never reached device A; it was noticed only when device A kept using the account. Repro: `Abgleich.test_F32_reserve_eines_kontos_ohne_registryzeile_erreicht_kein_anderes_geraet`.

**F33 Hooks run only when a receive changes the registry; a failed hook is never retried by sync.** Confirmed (test). A device pulls a registry with a new reserve, `registry.receive` writes it (`gateway/registry.py:278`), `reserve_hook` fails, report `hooks: failed`. Next pull: `now == previous`, no hook (`gateway/device_sync.py:351`). The reserve arrives only with the 6 h self-check (`gateway/devices_cli.py:751`), and only if the daemon runs new code (F31). herdr and Conductor hooks have no repair. Same pattern in `receive_account`: a failed reserve after enrolment is journaled only if a journal is passed (`gateway/device_sync.py:255-264`); `pull` passes none (`gateway/device_sync.py:484`), and the next receive returns before the reserve (`gateway/device_sync.py:244-246`). Repro: `Abgleich.test_F33_ein_fehlgeschlagener_reserve_hook_wird_beim_naechsten_empfang_nicht_wiederholt`.

**F34 Receiver errors do not reach the caller.** Confirmed (test). `push` checks only the exit code of `receive-registry` (`gateway/device_sync.py:415`, `gateway/device_sync.py:453`); the report with `failed` and `hooks` (`gateway/devices_cli.py:207-210`) is dropped. `merge_from` drops its own report (`gateway/device_sync.py:443`). The puller drops the result of `pull` entirely (`gateway/readings.py:229-232`). `device sync` shows `ok` when the other device did not set the reserve or did not remove an account. Repro: `Abgleich.test_F34_push_meldet_ok_obwohl_der_empfaenger_hook_und_entfernung_als_fehlgeschlagen_meldet`.

**F35 A device on an older release rejects the whole registry once a newer release writes a new field.** Confirmed (test, observed when a field was added). `registry.keys` raises on any unknown field (`gateway/registry.py:52-54`, account fields `gateway/registry.py:25-26`); `receive_registry` aborts before writing (`gateway/device_sync.py:304-305`). Tombstones, new devices and token sync stop until the old device is updated; the puller stays silent (F34), and because of F30 the old release is not visible either. `config.json` is just as strict (`gateway/account_metadata.py:52`): an old daemon after `update` without restart (F31) would reject a config with a new field when it rereads it. Possible. Repro: `Abgleich.test_F35_ein_neues_feld_eines_neueren_geraets_blockiert_den_ganzen_empfang`.

**F39 When two devices change the same row separately, the higher generation wins, not the later change; the loser's hook silently rewrites its config.** Possible. Both at g = 9; device A changes two other rows and then a reserve (row g = 12); later device B changes the same reserve (g = 10). On the next sync device A's value wins (`gateway/registry.py:233`, `gateway/registry.py:239`) and `reserve_hook` rewrites device B's config (`gateway/accounts_cli.py:438-444`) without output. Needs the puller off or a long outage.

**Risk**: high. F32 to F35 together explain why a config change does not arrive on the other device and nobody notices. The merge is explicit and correct; the flow needs a hook state per device with "pending" and a report up to the caller.

## A14 Long-lived tokens per device (implicit)

**Location**: `reconcile`, `receive_account` `gateway/device_sync.py:199-265`; `remove_local` `gateway/device_sync.py:285-294`; `switch` `gateway/device_sync.py:521-548`; `long_lived.switch_pending`.

**States** (per subscription and device): `no token`, `replica` (only `claude-long-lived/`), `ready to switch` (replica, store holds own login), `served` (store = replica), `retired` (`stores/.retired/`).

**Invariants**: I1 No token is deleted, only retired (`gateway/device_sync.py:238`, `gateway/device_sync.py:285-294`, `gateway/device_sync.py:510-518`). Holds. I2 An older token never overwrites the one the registry names (`gateway/device_sync.py:233`, `gateway/device_sync.py:462`, `gateway/device_sync.py:480`). Holds. I3 Switching only with a quiet pool, between stop and start (`gateway/device_sync.py:521-546`). Holds.

**Transition table**

| State | Event | Condition | Next state | Location |
|---|---|---|---|---|
| no token | `receive-account` | not owner, row in registry, no store | served, member, reserve from registry | `gateway/device_sync.py:247-259` |
| no token | `receive-account` | usage owner | replica | `gateway/device_sync.py:241` |
| replica | `receive-account` | store holds own login | ready to switch | `gateway/device_sync.py:244-245` |
| ready to switch | `device switch` | quiet, `--stop` works | served, old login retired, `--start` | `gateway/device_sync.py:529-546` |
| served | tombstone with same fingerprint | quiet, not Native, not last account | retired, member removed | `gateway/device_sync.py:333-347` |

No confirmed bugs. Possible: a crash between `retire_store` and `write_private` in `switch` (`gateway/device_sync.py:537-538`) leaves the account without a store; the token stays in `.retired`. **Risk**: low.

## A15 Device state and traffic light (implicit)

**Location**: Check: reachable, native-claude, install, version `gateway/devices_cli.py:620-657`; service, doctor `gateway/devices_cli.py:773-791`; native-login `gateway/devices_cli.py:793-805`; `check_all` `gateway/devices_cli.py:1114-1119`. Result in `RUNTIME/device-check-cache.json` (`gateway/conductor_board.py:76-86`), shared with every device that reaches this one by ssh (`gateway/conductor_board.py:124-141`), merged there, newest wins (`gateway/conductor_board.py:113-120`). Self-check in the daemon every 6 h (`gateway/launch.py:41`, `gateway/launch.py:71-80`, `gateway/launch.py:231-235`). Board: `_from_steps`, `_facts` `gateway/conductor_board.py:145-159`; `_probe` `gateway/conductor_board.py:184-198`; `machines` `gateway/conductor_board.py:200-234`; `_machine` (light and code) `gateway/conductor_board.py:236-276`. Status: `service_state` `gateway/device_sync.py:70-78`, `service_stale` `gateway/device_sync.py:81-96`, `status` `gateway/device_sync.py:170-182`, `loaded` `gateway/service.py:139-144`.

**States**: Per step: `ok`, `missing`, `manual`, `skipped`, `failed`, `unreachable` (`gateway/device_report.py:4`). Per device on the board: `reachable`, `poolRunning`, `versionMatches`, each true, false or None; light `green`, `grey`, `amber`, `red`; code `ok`, `unreachable`, `not-checkable`, `pool-stopped`, `version-mismatch`, `never-checked`, `failed:<step>`, `missing:<step>`, `manual:<step>`. Source of the facts: own device (live, `versionMatches` fixed true, `gateway/conductor_board.py:214`), probe over ssh (at most every 600 s), shared check (device without ssh from here).

**Invariants**: I1 `poolRunning` true ⇒ the pool answers. Violated (F36). I2 `versionMatches` true ⇒ the device has the same `sourceVersion` as this one. Violated for devices without ssh from here (F30) and against running code (F31). I3 The worst problem decides light and code (`gateway/conductor_board.py:271`). Holds. I4 A device on which no pool runs is red. Violated (F37, F36).

**Transition table** (facts and light of another device)

| State | Event | Condition | Next state | Location |
|---|---|---|---|---|
| any | probe exit 255 | not "timed out" | red `unreachable` | `gateway/conductor_board.py:188-191`, `gateway/conductor_board.py:247` |
| any | probe "timed out" | | grey `not-checkable` | `gateway/conductor_board.py:189-190`, `gateway/conductor_board.py:249` |
| any | probe exit 86 or 87 | | `poolRunning` None, amber `version-mismatch` | `gateway/conductor_board.py:192`, `gateway/conductor_board.py:257` (F37) |
| any | probe status | `service == 'running'` (unit loaded) | `poolRunning` true | `gateway/conductor_board.py:197` (F36) |
| any | probe status | `version` ≠ own | amber `version-mismatch` | `gateway/conductor_board.py:198`, `gateway/conductor_board.py:257` |
| no ssh from here | shared check | | facts from the self-check steps | `gateway/conductor_board.py:216`, `gateway/conductor_board.py:241`, `gateway/conductor_board.py:154-159` (F30) |
| checked | step `native-login` `manual` | Conductor not here | hidden | `gateway/conductor_board.py:262` |
| checked | step `failed` | | red `failed:<step>` | `gateway/conductor_board.py:266` |
| never checked | | own device or ssh | grey `never-checked` | `gateway/conductor_board.py:269` |

**Reachable invalid states**

**F30 The self-check always reports `version ok`, and a device without ssh to the checked one takes that as `versionMatches` true.** Confirmed (test, check cache). Device A checks itself every 6 h. `check_version` compares device A's `build_version()` with device A's own `device status` (`gateway/devices_cli.py:648-652`, `gateway/device_install.py:110-114`). The result goes to device B (`gateway/conductor_board.py:124-141`), which has no ssh to device A, never probes (`gateway/conductor_board.py:216`) and takes `_facts` from the shared check (`gateway/conductor_board.py:241`): `versionMatches` true, green. Evidence: the shared cache entry reads `version ok` measured only against itself, while the two devices ran different releases; it was noticed only through a missing reserve. Repro: `Lage.test_F30_die_selbstpruefung_meldet_version_ok_und_das_board_ohne_ssh_uebernimmt_sie`.

**F36 `service: running` and `poolRunning` only mean "unit loaded or enabled".** Confirmed (test). `service_state` returns `running` once `service.loaded` is true (`gateway/device_sync.py:77`); under systemd `loaded` means `is-active` or `is-enabled` (`gateway/service.py:143-144`), under launchd "job known" (`gateway/service.py:141-142`). After `claude-pool --stop` the unit stays loaded (`gateway/launch.py:590-592`), and a crashed daemon stays enabled. `device check` reports `service ok running` (`gateway/device_install.py:140`), the board `poolRunning` true and green (`gateway/conductor_board.py:212`, `gateway/conductor_board.py:197`). The right check exists already: `pool_stopped` (`gateway/devices_cli.py:320-327`, marker and ports). Repro: `Lage.test_F36_service_running_heisst_nur_einheit_aktiviert`.

**F37 A device without claude-pool shows amber "other version" instead of red "pool not running".** Confirmed (test). `_probe` sets `poolRunning` None and `versionMatches` false for exit 87 (no launcher) (`gateway/conductor_board.py:192`), which only gives amber (`gateway/conductor_board.py:257`). Rare, small effect. Repro: `Lage.test_F37_ein_geraet_ohne_claude_pool_gilt_als_andere_version_statt_als_gestoppt`.

**F41 `native-login` counts only registry accounts as "served by the pool".** Possible. `native_login._served` checks only the registry rows passed in (`gateway/native_login.py:87-96`, called with `value['accounts']` at `gateway/devices_cli.py:796`), and the registry holds only long-lived accounts. A Native login with a pool account that has its own login reports `failed:native-login` "is not an account the pool serves" although the pool serves it. Related: a `failed:native-login` from a check before a Conductor move stays red until the next check (up to 6 h); only `manual` is hidden for devices without Conductor (`gateway/conductor_board.py:262`).

**Risk**: high, because the light shows green in exactly the cases it should catch (F30, F36). Each fact needs a source and a reference: `versionMatches` against which version, `poolRunning` from a response instead of the unit.

## A16 Account lifecycle (implicit)

**Location**: `run_cli` `gateway/accounts_cli.py:1027-1122`; `enroll_steps` (long-lived) `gateway/accounts_cli.py:543-603`; own login `gateway/accounts_cli.py:1105-1122`; `reserve` `gateway/accounts_cli.py:378-408`; `remove_account` `gateway/accounts_cli.py:670-719`; `rename_account` with rollback `gateway/accounts_cli.py:888-996`; Cockpit pause `gateway/bridge.py:777-782` (A1). Files: `config.json`, `shunt.toml`, `stores/<name>.json`, `claude-long-lived/<name>.json`, `stores/.retired/`, registry row (long-lived only), `sessions.json` (`paused`, `blocked`).

**States** (per account and device): `not connected`, `login saved, not member` (stopped after step 1), `member`, `member with reserve` (hard, soft, with window), `paused`, `blocked` (A3), `in registry` (long-lived only), `removed` (files in `.retired`, tombstone).

**Invariants**: I1 Config and `shunt.toml` change only together and only after `shunt check` (`gateway/accounts_cli.py:232-250`). Holds. I2 An own login is never deleted (`gateway/accounts_cli.py:616-645`). Holds. I3 A removed account leaves no state that hits a new account of the same name. Violated (F40). I4 An account in the registry is served by the device that added it (except a usage owner without own login). Briefly violable (F43).

**Transition table**

| State | Event | Condition | Next state | Location |
|---|---|---|---|---|
| not connected | `account add <email>` | browser login works | login saved | `gateway/accounts_cli.py:1114-1115` |
| login saved | steps 2 and 3 | profile matches, `shunt check` ok | member | `gateway/accounts_cli.py:1116-1122` |
| login saved | `add` again | | continues at step 2 ("Using saved login") | `gateway/accounts_cli.py:1114` |
| not connected | `add --long-lived` | identity verified | in registry (g+1), then member | `gateway/accounts_cli.py:584`, `gateway/accounts_cli.py:602` |
| member | `reserve --at p` | | with reserve; registry only if a row exists | `gateway/accounts_cli.py:1065-1077`, `gateway/accounts_cli.py:399-408` |
| member with reserve | window edge (every minute) | `reserveHours` | Shunt options switched | `gateway/accounts_cli.py:329-343` |
| member | Cockpit pause | | paused, name in `paused` | `gateway/bridge.py:777-782` |
| member | `remove` | not last, not Native, quiet | removed; `paused` and `blocked` stay | `gateway/accounts_cli.py:689-711` (F40) |
| member | `rename` | quiet, pool restarted | member under new name, `paused` carried over | `gateway/accounts_cli.py:790-791`, `gateway/accounts_cli.py:888-996` |

**F40 A manual pause survives `account remove` and hits a newly connected account of the same name at the next start.** Possible. `remove_account` does not touch `sessions.json` (`gateway/accounts_cli.py:670-719`), `rename` does (`gateway/accounts_cli.py:790-791`). Pause `x`, `account remove x`, `account add … --name x`, `--stop`/`--start`: `restore_pauses` pauses `x` in Shunt (`gateway/bridge.py:382-385`).

**F43 `add --long-lived` writes the registry row before enrolling locally.** Possible. `gateway/accounts_cli.py:584` (registry g+1) runs before `gateway/accounts_cli.py:602` (`register`). If `shunt check` rejects, other devices fetch and enroll the token (A14), this one does not. Running `add` again continues. Small effect.

**Risk**: medium. Each step is clean and resumes on retry; a rule "remove clears every trace of the name, like rename" is enough.

## A17 Add and remove a device (implicit, step sequence)

**Location**: `add` (registry first, then check and make each step in `ADD_ORDER`, sync, final check, `record_checks`, `share_checks`) `gateway/devices_cli.py:1150-1210`; `remove` `gateway/devices_cli.py:1334-1437`; `forget` `gateway/devices_cli.py:1273-1321`.

**States**: `not registered`, `registered, steps open`, `ready`; on removal `cleanup running` (preview, herdr units, shell, service, accounts, Cua, herdr), `removed`.

**Invariants**: I1 A device stays registered until it is cleaned up, so `device remove` can be repeated (`stop_here`, `gateway/devices_cli.py:1378-1381`). Violated (F38). I2 `add` can be repeated and heals every step. Holds except for the running daemon (F31).

**Transition table** (removal)

| State | Event | Condition | Next state | Location |
|---|---|---|---|---|
| registered | `device remove` | reachable, quiet, no single copy there | cleanup running | `gateway/devices_cli.py:1347-1376` |
| cleanup | preview, herdr units, shell, service or accounts fail | | stays registered, fix `device remove` | `gateway/devices_cli.py:1382-1409` |
| cleanup | Cua or herdr integration fails | | dropped from the registry anyway, exit 1 | `gateway/devices_cli.py:1411-1424` (F38) |
| removed | `--uninstall` fails | | removed, manual command shown | `gateway/devices_cli.py:1427-1430` |

**F38 `device remove` drops the device although the Cua or herdr cleanup failed; the suggested fix goes nowhere.** Confirmed (code path). `gateway/devices_cli.py:1413-1418` and `gateway/devices_cli.py:1420` append `failed` with fix `claude-pool device remove <name>` (`_Plain`, `gateway/devices_cli.py:1441`), then `registry.update(drop)` runs anyway (`gateway/devices_cli.py:1424`). The fix ends at "No device named …" (`gateway/devices_cli.py:1339-1340`). Tunnel units to the removed device with `Restart=always` (`gateway/device_cua.py:277`) or herdr profiles remain. Repro: to be written with G11.

**Risk**: medium for F38 (rare, but units that restart forever), otherwise low.

## A18 Cockpit session display (implicit, derived)

**Location**: `readTranscript` `gateway/cockpit/hooks/machine.ts:248-296`, `withAgents` `gateway/cockpit/hooks/machine.ts:306-316`, `assignTranscripts` `gateway/cockpit/hooks/machine.ts:118-135`; display `machine-view.ts`, `machine-app.ts`.

**States** per session: `needs`, `working`, `done`, `unknown`; recomputed on each poll from the end of the transcript file, nothing stored.

**Invariant**: I1 Each session shows the state of its own file. Heuristic only.

**Transition table**

| State | Event | Condition | Next state | Location |
|---|---|---|---|---|
| any | last entry `system`, `end_turn` or abort | | `done` | `gateway/cockpit/hooks/machine.ts:274-277` |
| any | open tool call | question tool | `needs` | `gateway/cockpit/hooks/machine.ts:288` |
| any | open tool call | permission tool, over 30 s, CPU under 1 % | `needs` ("permission?") | `gateway/cockpit/hooks/machine.ts:291-292` |
| any | otherwise | | `working` | `gateway/cockpit/hooks/machine.ts:293-295` |
| `done`, `unknown` | subagent wrote in the last 60 s | | `working` | `gateway/cockpit/hooks/machine.ts:314` |

**F45 Two sessions in the same folder can swap states.** Possible. The newest file goes to the session with the most CPU (`gateway/cockpit/hooks/machine.ts:127-131`); if a waiting session (CPU 0) wrote last, the busy one shows its `needs` and vice versa.

**Risk**: low, display only. Use the session id from `~/.claude/sessions/<pid>.json` instead of CPU rank.

## A19 Phone hub and remote calls (explicit)

- `gateway/phone/app.js:42-43`: sending keeps the id until confirmed (safe retry); decisions carry `generation` and `digest`; an error locks the buttons until the next successful poll. No bugs found.
- `gateway/phone-hub/hooks/register.ts:17-19`: each `send <id> <text>` gets a new UUID. **F44** If the reply is lost and the command is sent again, the instruction arrives twice. Possible.
- `gateway/remote.py:73-74`: a timeout becomes exit 255 and thus `unreachable` (`gateway/device_sync.py:412`), even if the command finished on the target; the receive commands are idempotent, so this is harmless.

**Risk**: low.

## Trivial

`gateway/heavy_queue.py`: worker limit and old entry points, no state. `gateway/scratch.py:78-105`: deletes folders of ended sessions after three days without change; "ended" means "not in `~/.claude/sessions/<pid>.json`" (`gateway/scratch.py:48-57`). **F22** A running session without that entry loses its scratch folder after three quiet days. Possible. `gateway/conductor_board.py`: caches for device checks and probes, newest wins; no bug found. `gateway/native_auth.py:42-74`: a Native token identity is cached 120 s, a failure 2 s; a revoked token stays valid up to 120 s. Intended. `diagnostics.Journal`: equal entries within 30 s are merged (`gateway/diagnostics.py:78`). `forecast.py`, `backtest.py`, `telemetry.py` (except `capacity`), `settings.py`, `memory.py`, `agent_cache.py`, `account_login.py`, `account_metadata.py`, `service.py`, `update.py`, `views.py`, `short_label.py`, `validation.py`, `managed_block.py`, `private_files.py`, `device_report.py`: pure functions or one-off flows without own state. `device_herdr.py`, `device_cua.py`, `device_preview.py`, `device_integrations.py` check and make each step idempotently. Cockpit `pills.ts`, `theme.ts`, `canvas.ts`, `limits.ts` are display switches.

## Bugs by impact

Runtime (A1 to A11):

| Rank | F | Effect | Automaton | Evidence | Status |
|---|---|---|---|---|---|
| 1 | F1 | With an exhausted pool, interactive sessions and the Conductor got the weekly-limit 429 and hung in the dialog; gateway and client hook did not wait | A1, A2 | journal and usage history of one incident; `test_F1_*` | fixed in #35 |
| 2 | F17 | The Conductor was never woken for an empty pool (about 27 h standstill without alert) | A8 | `conductor-alerts.json`, wake log; `test_F17_*` | fixed in #35 |
| 3 | F11 | Brake waits end after 600 s at the client; the 3 h cap is never reached (8.7 h chain); 54 % of the journal is `brake_wait` | A4 | `brake-waits`, journal | open |
| 4 | F3 | Released waits bypass admission; more than 32 requests up, interactive requests get 503 | A2, A4 | `test_F3_*` | open |
| 5 | F13 | The Conductor unit runs old code for days after an update; fixes silently do not apply | A6 | service journal, process list | open |
| 6 | F2 | Brake stage computed against a reset that is too early; stage too low, wrong time in the alert | A1, A4 | usage history `resetAt`; `test_F2_*` | fixed in #35 |
| 7 | F9 | When an organization is blocked, the second account's 403 reaches Claude Code and the account stays in the pool | A3 | `test_F9_*` | open |
| 8 | F8 | Lost block: the account stays paused until a service restart, without reason or alert | A3 | `test_F8_*` (forced interleaving) | open |
| 9 | F18 | The same block wakes the Conductor every six hours | A8 | wake log; `test_F18_*` | open |
| 10 | F4 | Every 400 of background agents goes up twice; the journal reports `cache_fallback` wrongly | A2 | `test_F4_*` | open |

Devices, install, cockpit (A12 to A19):

| Rank | F | Effect | Automaton | Evidence | Status |
|---|---|---|---|---|---|
| 1 | F32 | A reserve never reaches the other device; the output says that device syncs itself | A13, A16 | config and registry; `test_F32_*` | open |
| 2 | F30 | A device on an old release shows green on the board | A15 | device-check cache; `test_F30_*` | open |
| 3 | F31 | After `update` the daemon runs old code with a green light | A12 | code path, two manual restarts | open |
| 4 | F33 | A failed hook is never retried by sync (reserve up to 6 h, herdr and Conductor never) | A13 | `test_F33_*` | open |
| 5 | F34 | `device sync` and the puller report ok despite receiver errors | A13 | `test_F34_*` | open |
| 6 | F35 | A new registry field stops sync to older devices entirely | A13 | `test_F35_*` | open |
| 7 | F36 | A stopped or crashed pool shows `service ok` and green | A15 | `test_F36_*` | open |
| 8 | F38 | `device remove` leaves tunnel units that restart forever, with no way to retry | A17 | code path | open |
| 9 | F42 | 1.5 GB of old releases, growing with each update | A12 | `ls`, `du` | open |
| 10 | F37 | A device without a pool is only amber | A15 | `test_F37_*` | open |

Possible, not confirmed: F5, F6, F7, F10, F12, F14, F15, F16, F19, F20, F21, F22, F39, F40, F41, F43, F44, F45.

## Fix groups

| Group | Bugs | Issue | Owner decision |
|---|---|---|---|
| [G1](#g1-one-takes-work-predicate) | F1, F2, F17 (F5 moved to G2) | ISSUE_G1 | no |
| [G2](#g2-brake-waits-as-a-chain-with-re-admission) | F3, F11, F5 | ISSUE_G2 | yes |
| [G3](#g3-explicit-block-automaton-with-probe) | F8, F9, F18, F10 | ISSUE_G3 | no |
| [G4](#g4-retry-a-400-only-for-own-rewrites) | F4 | ISSUE_G4 | no |
| [G5](#g5-make-the-running-service-version-visible) | F13, F15 | ISSUE_G5 | yes |
| [G6](#g6-sync-with-feedback-and-retry) | F33, F34, F39 | ISSUE_G6 | no |
| [G7](#g7-reserve-per-subscription-on-all-devices) | F32 | ISSUE_G7 | yes |
| [G8](#g8-version-against-reference-and-running-code) | F30, F31, F37 | ISSUE_G8 | no |
| [G9](#g9-service-state-from-the-response-not-the-unit) | F36 | ISSUE_G9 | no |
| [G10](#g10-forward-compatible-registry) | F35 | ISSUE_G10 | yes |
| [G11](#g11-drop-a-device-only-after-cleanup) | F38 | ISSUE_G11 | no |
| [G12](#g12-leftovers-and-cleanup) | F40, F41, F42, F43, F44 | ISSUE_G12 | no |

### G1 One takes-work predicate

Fixed in #35. `routing.holds` (a window rejected, full or at the hard reserve cap, or Shunt's `capped`) is used by `telemetry.capacity`, `brake.returns_at` and `brake.standstill`; `bridge.read_accounts` keeps `capped` and `cappedFable`. Alerts follow through `capacity`. Regression tests: `tests/test_capacity_reserve.py`.

### G2 Brake waits as a chain with re-admission

- Sketch: when a wait ends, the request keeps waiting (with pings) until `admit` lets it in again; main slots stay reserved. `brakeWaiting.since` survives a `cancelled` for a grace period (about 120 s) per session and class; the next wait continues the chain, `MAX_WAIT` counts from the chain start, `brake_wait` goes to the journal only at the chain start, and `brake-waits` gets a chain id. The quota wait sets `lease['waiting']` like the brake (F5).
- Regression tests: `Anfrage.test_F3_freigegebene_bremswarte_ueberschreitet_die_anfragegrenze`. New for F11: two waits of one session, the first ended by the client after 0.4 s, `brake_max_wait = 0.6`; the second must end `capped` after at most 0.3 s, with one `brake_wait` in the journal. New for F5: a waiting quota request with `max_requests = 1` still lets an interactive request in.
- Owner decision: does the 3 h cap apply per session chain (recommended), or should the pool answer before the client timeout itself? The latter would rebuild the wait.

### G3 Explicit block automaton with probe

- Sketch: `blocked[name]` gets `state` (`blocked`, `probing`). `release_blocks` sets `probing` and a new `until` under the lock, then calls Shunt; `block_account` always sets `blocked` with a new `until`; only an entry still `probing` whose probe got no 403 (first successful response or 10 min) is removed. On a block code, all accounts of the same organization are blocked, or the retry continues while each new account returns the same code, at most once per account. `conductor_alerts` counts `probing` as active. Cockpit `resume` on a blocked account removes the entry or is refused.
- Regression tests: `Sperre.test_F8_eine_sperre_waehrend_der_freigabe_geht_verloren`, `Anfrage.test_F9_ein_zweites_gesperrtes_konto_derselben_organisation_wird_nicht_pausiert`, `KontoLage.test_F18_gesperrt_endet_und_beginnt_bei_jedem_neuen_versuch`.

### G4 Retry a 400 only for own rewrites

- Sketch: `gateway/bridge.py:996` retries only when the error names `cache_control` or `ttl` (one-hour cache) or for the short form; otherwise forward the 400 unchanged, without a journal entry.
- Regression test: `Anfrage.test_F4_jedes_400_wird_als_cache_ablehnung_wiederholt`.

### G5 Make the running service version visible

- Sketch: `serve_service` and the pool daemon write the version from `build-info.json` at start (`conductor.json` key `version`, `/pool/status` field `build`). `conductor.status()` and `doctor` compare it with `current`; `device check` reports "running old code" with the restart command for the unit. `update` lists the units that run old code. Journal and `stored()` keep unknown codes and keys when writing back (F15).
- Regression tests (to write): `conductor.status()` returns `version`, and `conductor.check()` is not `ok` when it differs from `build-info.json`; the journal loads a line with an unknown code and writes it back unchanged.
- Owner decision: should `claude-pool update` restart the Conductor unit itself? That interrupts the running Conductor turn. Recommended: only report it and let the owner confirm the restart.

### G6 Sync with feedback and retry

- Sketch: `receive_registry` records failed hooks in `RUNTIME/hooks-pending.json` and runs hooks on every receive when the registry changed or a hook is pending (the hooks already compare target and actual). `push` reads the `receive-registry` report and returns `failed: <hook or account>` when `failed` or a hook is `failed`; `merge_from` passes its own report on; the puller writes `registry_sync_failed` to the journal once per cause. `pull` passes `receive_account` a journal, and a reserve that was not set goes through the same marker. F39: `receive_registry` reports when a hook overwrites a local reserve (report only, no new rule).
- Regression tests: `Abgleich.test_F33_ein_fehlgeschlagener_reserve_hook_wird_beim_naechsten_empfang_nicht_wiederholt`, `Abgleich.test_F34_push_meldet_ok_obwohl_der_empfaenger_hook_und_entfernung_als_fehlgeschlagen_meldet`.

### G7 Reserve per subscription on all devices

- Small sketch: `device status` reports the reserve fields per account (not secret); `check_accounts` compares the reserve for every subscription both devices serve (by UUIDs) and reports `missing` with the command for the other device; `account reserve` on an account without a registry row says "applies only on this device" and names the command for the others.
- Large sketch: reserve rows for all subscriptions in the registry (by UUIDs, without tokens), set everywhere by `reserve_hook`. About a day; needs G6 and G10.
- Regression test: `Abgleich.test_F32_reserve_eines_kontos_ohne_registryzeile_erreicht_kein_anderes_geraet` (passes with either way).
- Owner decision: small or large? Recommended: large, because a reserve belongs to the subscription, not the device.

### G8 Version against reference and running code

Builds on G5.
- Sketch: the `version` step carries `sourceVersion` as its own field; for shared checks the board compares it with its own (`_facts` gets `wanted`) instead of taking `ok`. The self-check of a non-owner compares with the usage owner; the puller reaches it by ssh anyway (`gateway/readings.py:215-216`) and the reading brings its `sourceVersion`. `device status` also reports the release of the running daemon (`/pool/status` field `build` from G5); `check_service` reports `failed` "running old code" with `claude-pool --stop && claude-pool --start` when it differs from `current`; `install.py` lists services with old code at the end. `_probe` sets `poolRunning` false on exit 87.
- Regression tests: `Lage.test_F30_die_selbstpruefung_meldet_version_ok_und_das_board_ohne_ssh_uebernimmt_sie`, `Lage.test_F37_ein_geraet_ohne_claude_pool_gilt_als_andere_version_statt_als_gestoppt`. New for F31: daemon reports an old `build`, `build-info.json` a new one, then `check_service` is not `ok`.

### G9 Service state from the response, not the unit

- Sketch: `service_state` returns `running` only when the service is active (systemd `is-active`, launchd `state = running` through `launchd_job`) and the `maintenance` marker is absent, else `stopped`; `pool_stopped` and `service_state` share one function. `loaded` stays for uninstall and removal, where "registered" is meant.
- Regression tests: `Lage.test_F36_service_running_heisst_nur_einheit_aktiviert`; new: marker set and no answer on the ports gives `stopped`.

### G10 Forward-compatible registry

- Sketch: `registry.validate` accepts unknown row fields when they are JSON scalars or short lists that pass `no_tokens`, and the merge keeps them (the row is chosen as a whole already). Known fields stay strictly checked, and the 256 KiB limit stays. The same for `config.json`, so an old daemon does not reject a new config.
- Regression test: `Abgleich.test_F35_ein_neues_feld_eines_neueren_geraets_blockiert_den_ganzen_empfang`.
- Owner decision: this relaxes "known fields only". Recommended: yes, because the token check and the size limit stay.

### G11 Drop a device only after cleanup

- Sketch: handle Cua and herdr failures with `stop_here` like the other sub-steps; the device stays registered and `device remove` can be repeated.
- Regression test (to write): `device_cua.remove` raises in the fake; afterwards the device is still in the registry, exit 1, and a second `remove` reaches the Cua step again.

### G12 Leftovers and cleanup

- Sketch: `account remove` takes the name out of `paused` and `blocked` in `sessions.json` (through the daemon's admin path or under its lock, like `rename`). `native_login.check` gets the config members besides the registry rows (without email, compared by the stored profile as in `matches_native`). `update` deletes releases that are not `current`, that no running process uses (`/proc/*/cmdline`, on macOS `ps`) and that no Mod of a live session points to; the last two stay, Mod folders likewise. `add --long-lived` writes the registry row only after `register`. Phone hub: id from task, text and minute instead of new per call.
- Regression tests (to write): pause, `remove`, `add` with the same name, `restore_pauses` leaves it free; a Native login equal to a config member without registry row is not `failed`; an update with three old unused releases leaves two; a rejected `register` leaves no registry row; two `send` with the same text within a minute give one id.

## Changes

- G1 (F1, F2, F17), fixed by #35, merged into `main` as 92bf795: the shared check `routing.holds` (window rejected, full or at the hard reserve cap, plus Shunt's `capped`) is used by `telemetry.capacity`, `brake.returns_at` and `brake.standstill`; `bridge.read_accounts` keeps `capped` and `cappedFable`. New invariant: an account at its hard reserve cap counts as exhausted for the quota wait, the brake and the alert, as it does in Shunt. Tests: `tests/test_capacity_reserve.py` (red on the previous `main`, green after). Live on one device. F5 stays open and moves to G2.
