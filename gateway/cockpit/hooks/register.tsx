import { controlURL, phoneURL, tokenFile as configuredTokenFile } from './connection'
import { atom, read, update } from 'claude-code'
import { registerSuiteLimits } from './limits'
import type { EngineInterface, Register, RenderElement, SessionRateLimit } from 'claude-code'

import {
  assignTranscripts,
  claudePids,
  findAgents,
  findTranscripts,
  sessionFolder,
  statLines,
  tailAgent,
  workflowIdOf,
  workflowNameOf,
  lsofCwd,
  parse,
  projectDir,
  PS,
  sessionsOf,
  SYS,
  tailTranscript,
  VM,
} from './machine'
import type { AgentFile } from './machine'
import { drawMachine } from './machine-view'
import { machineSections } from './machine-app'
import { item } from './pills'
import type { Tone } from './pills'
import type { Chip } from './theme'
import type { Account, Live, Machine, Pace, Quota, Window, Wrap, PoolData } from '../types'

// The terminal gets character cells, the app vector drawings.

const quota = atom({ plugin: 'cockpit', key: 'quota' } as const, { others: [] } as Quota)
const pool = atom({ plugin: 'cockpit', key: 'pool' } as const, null as PoolData | null)
const switchNoted = atom({ plugin: 'cockpit', key: 'switchNoted' } as const, 0)
const wrapNoted = atom({ plugin: 'cockpit', key: 'wrapNoted' } as const, false)
const POOL_PANE = 'cockpit-pool'
const HISTORY_PANE = 'cockpit-pool-history'
const SESSIONS_PANE = 'cockpit-pool-sessions'
const FORECAST_PANE = 'cockpit-pool-forecast'
const HEALTH_PANE = 'cockpit-pool-health'
const preview = atom({ plugin: 'cockpit', key: 'preview' } as const, false)
const machine = atom({ plugin: 'cockpit', key: 'machine' } as const, null as Machine | null)
const live = atom({ plugin: 'cockpit', key: 'live' } as const, {} as Live)
const wrap = atom({ plugin: 'cockpit', key: 'wrap' } as const, { off: false, wrapping: false } as Wrap)

// An optional usage guard tells workflow agents to
// wrap up before the 5h window runs out. ~/.claude/NOWRAP switches it off; it
// writes a WRAPUP file when it fires. Paths under $HOME.
const NOWRAP = '.claude/NOWRAP'
const WRAPUPS = ['.claude/WRAPUP']
const WRAP_AT = 97 // the guard's --at
const WRAP_MARGIN = 15 * 60_000 // and --margin
const WRAP_KEY = 'cockpit-wrap'
const MACHINE = 'machine' // $.store: the last snapshot, shared so sessions take turns sampling
const MACHINE_EVERY = 5_000
const PANE = 'machine'
let isCollecting = false
const PREVIEW_MS = 15_000

// $.store is shared by every session on this machine, so each session reports
// the account it runs on and the band shows all of them.
const ACCOUNTS = 'accounts' // Record<accountId, Account>
const SAMPLES = 'samples' // Record<accountId, [t, pct][]> of the 5h window
// The forecast. Readings come from every session on the account, each time a
// response reports the rate limits, so a quiet spell with none means nothing
// was burning.
const PACE_TAU = 20 * 60_000 // a reading's weight falls by e every 20 min of age
const PACE_LOOKBACK = 60 * 60_000
const PACE_MIN_SPAN = 10 * 60_000 // less history than this is noise
const PACE_MIN_READINGS = 3
const IDLE_AFTER = 5 * 60_000 // a gap longer than this eases the rate off
const MAX_CARRY = 10 * 60_000 // how far an old reading is carried forward to now
const WARN_SPARE = 10 * 60_000 // warn only when it runs out this long before the reset
const GUARD_WINDOW = 45 * 60_000 // the usage guard's window, for "wrap-up due"
const SWITCH_AT = 85
const EXPAND_AT = 60

// Claude Code's own dark theme, so the band reads as part of the app.
const C = {
  claude: '#D77757',
  session: '#E8956F',
  week: '#B1B9F9',
  text: '#FFFFFF',
  soft: '#ABABAB',
  empty: '#4A4D60',
  border: '#5C5F73',
  success: '#4EBA65',
  warning: '#FFC107',
  error: '#FF6B80',
}

type $ = EngineInterface
type Samples = Record<string, [number, number][]>

export const register: Register = on => {
  registerSuiteLimits(on)
  on('session.start', async ($, e, next) => {
    const result = await next(e)
    await $.command.register({
      name: 'cockpit',
      description: 'Open the pool panel; inspect accounts and control routing',
      argumentHint: 'phone | health | history | sessions | forecast | preview | pin <account> | unpin | pause <account> | resume <account>',
      immediate: true,
    })
    await $.command.register({
      name: 'wrap',
      description: 'Toggle auto-wrap: whether the usage guard tells agents to wrap up before the 5h limit',
      immediate: true,
    })
    await refreshPool($)
    $.clock.every(5_000, () => void refreshPool($))
    await refreshWrap($)
    $.clock.every(15_000, () => void refreshWrap($))
    const usage = await $.session.usage()
    await measureLive($, usage)
    await measure($, usage.rateLimits)
    // Other sessions update the store; pull their numbers in now and then.
    $.clock.every(30_000, () => void refresh($))
    await $.command.register({
      name: 'machine',
      description: 'Toggle the machine pane: load, what uses the CPU, which Claude session spawned it',
      immediate: true,
    })
    // A reload keeps the pane up but drops the timer: pick it up again.
    if ((await $.ui.panes()).some(p => p.id === PANE)) startPolling($)
    return result
  })

  on('session.measure', async ($, e, next) => {
    if (e.changed.includes('context') || e.changed.includes('cost')) await measureLive($, e)
    if (e.changed.includes('rateLimits')) await measure($, e.rateLimits)
    return next(e)
  })

  on('command.run', { command: 'cockpit' }, async ($, e) => {
    const args = e.args.trim().split(/\s+/)
    if (args[0] === 'phone') return { text: await phoneLink($) }
    if (['history', 'sessions', 'forecast', 'health'].includes(args[0] ?? '')) {
      await refreshPool($)
      const id = args[0] === 'health' ? HEALTH_PANE : args[0] === 'sessions' ? SESSIONS_PANE : args[0] === 'forecast' ? FORECAST_PANE : HISTORY_PANE
      await $.ui.open({ id, title: args[0], rows: args[0] === 'forecast' ? 18 : 14, focus: true, closeOnEscape: true })
      return { text: `${args[0]} opened.` }
    }
    if (args[0] === 'preview') {
      await update($, preview, () => true)
      $.clock.after(PREVIEW_MS, () => void update($, preview, () => false))
      return { text: 'Showing sample numbers for 15 seconds.' }
    }
    if (['pin', 'unpin', 'pause', 'resume'].includes(args[0] ?? '')) {
      const p = await read($, pool)
      const name = args.slice(1).join(' ').toLowerCase()
      const matches = p?.accounts.filter(a => a.name.toLowerCase() === name || a.label.toLowerCase() === name) ?? []
      if (args[0] !== 'unpin' && matches.length !== 1) return { text: 'Choose an account from /cockpit.' }
      const result = await poolRequest($, { action: args[0], account: matches[0]?.name })
      if (!result) return { text: 'Unable to confirm routing; refresh /cockpit.' }
      await refreshPool($)
      return { text: args[0] === 'unpin' ? 'Automatic routing enabled.' : `Pool updated: ${args[0]} ${matches[0]?.label}.` }
    }
    await refreshPool($)
    await $.ui.open({ id: POOL_PANE, title: 'cockpit', rows: 14, focus: true, closeOnEscape: true })
    return { text: 'Pool panel opened.' }
  })

  on('turn.step', async function* ($, e, next) {
    const result = yield* next(e)
    if (!e.agentId) {
      await refreshPool($)
      const p = await read($, pool)
      const pressure = p && !p.error && p.wrap && p.accounts.length > 0 && p.accounts.every(a => a.status === 'limited' || a.status === 'paused' || a.status === 'reauthenticate') && p.accounts.some(a => a.status === 'limited')
      if (pressure && !(await read($, wrapNoted))) {
        const appended = await $.session.append({ message: { type: 'user', content: [{ type: 'text', text: 'Cockpit auto-wrap: all eligible subscriptions are near their usage limits. Finish the current safe step, record completed work and next actions, then pause for quota recovery.' }] } })
        await update($, wrapNoted, () => !appended.deny)
        $.ui.log('Pool near its limit; requested a checkpoint before quota recovery.')
      } else if (!pressure) await update($, wrapNoted, () => false)
    }
    return result
  })

  on('ui.render', { component: 'Pane', requestId: POOL_PANE }, async ($, e) => {
    const { Box, Text, Button } = $.ui.resolve(e)
    const p = await read($, pool)
    if (!p) return <Text color="inactive">Pool not connected. /cockpit preview shows the sample card.</Text>
    const compact = e.surface === 'terminal' && (e.viewport?.rows ?? 32) < 32
    const dense = compact && p.accounts.length > 2
    const cells = e.props.bodyColumns >= 100 ? 12 : e.props.bodyColumns >= 68 ? 8 : 4
    const reset = p.forecast?.nextReset
    return <Box flexDirection="column" paddingX={1}>
      <Box flexDirection="row" columnGap={2}>
        <Box flexGrow={1} minWidth={0}><Text bold wrap="truncate-end">{p.error ? 'Pool unavailable · last known state' : p.thinkingDropped ? 'Earlier thinking was dropped' : p.interrupted ? 'Response interrupted · check last step' : p.persistenceError ? 'Recovery state could not be saved' : `${p.ready} ready · ${p.mode === 'pinned' ? `pinned to ${p.accounts.find(a => a.name === p.pin)?.label ?? p.pin ?? '?'}` : 'automatic routing'}`}</Text></Box>
        <Button key="pool-unpin" label="Auto" plain onPress={() => undefined} />
        <Button key="pool-history" label="History" plain onPress={() => undefined} />
        <Button key="pool-sessions" label="Sessions" plain onPress={() => undefined} />
        <Button key="pool-forecast" label="Forecast" plain onPress={() => undefined} />
        <Button key="pool-health" label="Health" plain onPress={() => undefined} />
        {!compact && e.props.bodyColumns >= 90 && <Button key="pool-phone" label="Phone" plain onPress={() => undefined} />}
      </Box>
      <Text color="inactive" wrap="truncate-end">{poolForecast(p, compact)}</Text>
      {poolCoverage(p) && <Text color="warning">{poolCoverage(p)}</Text>}
      {p.quotaWaiting && <Text color="warning">Pool quota exhausted · continuing automatically after reset · Esc to cancel</Text>}
      {p.connectorIdentity && <Text color="inactive" wrap="truncate-end">Subscription pool · connectors {accountLabel(p.connectorIdentity, p.accounts)}</Text>}
      {p.routing && <Text color="inactive" wrap="truncate-end">Route · {p.routing.account ? `${accountLabel(p.routing.account, p.accounts)} · ` : ''}{p.routing.reason}{p.routing.peers ? ` · ${p.routing.peers} other recent session(s)` : ''}</Text>}
      {!compact && poolRange(p) && <Text color="inactive" wrap="truncate-end">{poolRange(p)}</Text>}
      {p.accounts.map(a => <Box flexDirection="column">
        <Box flexDirection="row" columnGap={2} alignItems="center">
        <Box flexGrow={1} minWidth={0}>
          <Text wrap="truncate-end"><Text bold>{p.active === a.name ? '● ' : '○ '}{a.label}</Text> · {a.status}{p.sessions?.some(s => s.account === a.name && s.busy) ? ` · ${p.sessions.filter(s => s.account === a.name && s.busy).length} busy` : ''}{!compact && a.plan ? ` · ${a.plan}` : ''}{dense && <Text color="inactive"> · 5h {poolBar(a.fiveHour, 4)} {a.fiveHour ? fmtPct(a.fiveHour.pct) : '?'} · <Text color={a.sevenDay && a.sevenDay.pct >= 95 ? 'error' : 'inactive'}>week all {poolBar(a.sevenDay, 4)} {a.sevenDay ? fmtPct(a.sevenDay.pct) : 'not reported'}</Text></Text>}</Text>
        </Box>
        <Button key={`pool-pin:${a.name}`} label="Pin" plain onPress={() => undefined} />
        <Button key={`pool-pause:${a.name}`} label={a.paused ? 'Resume' : 'Pause'} plain onPress={() => undefined} />
        </Box>
        {!dense && <Box flexDirection="row" columnGap={2} flexWrap="wrap">
          <Text color={a.fiveHour && a.fiveHour.pct >= 95 ? 'error' : a.fiveHour && a.fiveHour.pct >= 80 ? 'warning' : 'text'}>5h {poolBar(a.fiveHour, cells)} {a.fiveHour ? fmtPct(a.fiveHour.pct) : '?'}<Text color="inactive">{a.fiveHour?.resetsAt && (!compact || e.props.bodyColumns >= 68) ? ` ↻ ${longDur(a.fiveHour.resetsAt - p.updatedAt)}` : ''}{compact ? poolRate(p, a.name, 'fiveHour') : ''}</Text></Text>
          <Text color={a.sevenDay && a.sevenDay.pct >= 95 ? 'error' : a.sevenDay && a.sevenDay.pct >= 80 ? 'warning' : 'inactive'}>week all {poolBar(a.sevenDay, cells)} {a.sevenDay ? fmtPct(a.sevenDay.pct) : 'not reported by Anthropic'}<Text color="inactive">{!compact && a.sevenDay?.resetsAt ? ` ↻ ${longDur(a.sevenDay.resetsAt - p.updatedAt)}` : ''}</Text></Text>
        </Box>}
        <Text color={a.fableWeekly && a.fableWeekly.pct >= 95 ? 'error' : 'inactive'}>Fable weekly · {poolBar(a.fableWeekly, dense ? 4 : cells)} {a.fableWeekly ? fmtPct(a.fableWeekly.pct) : 'fableWeekly' in a ? 'not reported' : 'reading unavailable'}{a.fableWeekly?.resetsAt ? ` ↻ ${longDur(a.fableWeekly.resetsAt - p.updatedAt)}` : ''}</Text>
        {a.quotaLimits?.sevenDay?.status === 'rejected' && <Text color="error">Weekly limit reached{a.quotaLimits.sevenDay.resetsAt ? ` · resets in ${longDur(a.quotaLimits.sevenDay.resetsAt - p.updatedAt)}` : ' · reset not reported'}</Text>}
        {!compact && <Text color="inactive" wrap="truncate-end">Pace · {poolPace(p, a.name)} · checked {a.seenAt ? `${longDur(p.updatedAt - a.seenAt)} ago` : '?'}</Text>}
      </Box>)}
      {p.switch && <Text color="inactive">{p.accounts.find(a => a.name === p.switch?.from)?.label ?? p.switch.from} → {p.accounts.find(a => a.name === p.switch?.to)?.label ?? p.switch.to} · {p.switch.reason}</Text>}
      {p.thinkingDropped && <Text color="warning">Earlier thinking was dropped after switching accounts.</Text>}
      {p.interrupted && <Text color="warning">A response was interrupted; check the last step before retrying.</Text>}
      {p.persistenceError && <Text color="warning">Recovery state could not be saved.</Text>}
      {p.health && <Text color={p.health.backend !== 'ready' || p.health.storageError ? 'warning' : 'inactive'} wrap="truncate-end">{p.error ? 'Health · last known state' : p.health.draining ? 'Health · finishing active responses' : `Health · backend ${p.health.backend} · ${p.health.activeRequests}/${p.health.requestLimit} requests`}</Text>}
      {!!p.recovery?.restarts && <Text color="inactive">Backend recovered {p.recovery.restarts} time(s).</Text>}
      {!compact && <Text color="inactive">{p.error ? 'Offline · retrying every 5s' : `Live · 5s${reset ? ` · next reset ${reset.account} ${reset.window} in ${longDur(reset.at - p.updatedAt)}` : ''}`}</Text>}
      <Text color="inactive">Forecast uses all-model windows; Fable weekly is separate.</Text>
    </Box>
  })

  on('ui.render', { component: 'Pane', requestId: HISTORY_PANE }, async ($, e) => {
    const { Box, Text } = $.ui.resolve(e)
    const p = await read($, pool)
    if (!p) return <Text color="inactive">Pool not connected.</Text>
    return <Box flexDirection="column" paddingX={1}>
      <Text bold>{p.error ? 'Switch history · last known state' : 'Switch history · this session'}</Text>
      {!p.history?.length && <Text color="inactive">No account switches recorded yet.</Text>}
      {p.history?.map(event => <Box flexDirection="column" marginTop={1}>
        <Text>{accountLabel(event.from, p.accounts)} → {accountLabel(event.to, p.accounts)} · {longDur(p.updatedAt - event.at)} ago</Text>
        <Text color="inactive">{event.reason} · cache read {event.cacheReadTokens ?? '?'} · created {event.cacheCreationTokens ?? '?'} tokens{event.thinkingDropped ? ' · thinking dropped' : ''}</Text>
      </Box>)}
    </Box>
  })

  on('ui.render', { component: 'Pane', requestId: SESSIONS_PANE }, async ($, e) => {
    const { Box, Text } = $.ui.resolve(e)
    const p = await read($, pool)
    if (!p) return <Text color="inactive">Pool not connected.</Text>
    return <Box flexDirection="column" paddingX={1}>
      <Text bold>{p.error ? 'Claude sessions · last known state' : `Claude sessions · ${p.sessions?.length ?? 0} connected or recently active`}</Text>
      {!p.sessions?.length && <Text color="inactive">Sessions appear when they connect or send a request.</Text>}
      {p.sessions?.map(s => <Text wrap="truncate-end"><Text bold>{s.session === p.session ? '● ' : '○ '}{s.project ? `${s.project.slice(0, 20)}:${s.session.slice(0, 4)}` : s.session.slice(0, 8)}</Text> · {s.account ? accountLabel(s.account, p.accounts) : 'unassigned'} · {s.model} · {s.effort} · {s.busy ? 'working' : 'idle'}{s.pin ? ' · pinned' : ''} · {s.reason}</Text>)}
      <Text color="inactive">Live · 5s · request activity expires after 5m; open panels stay visible.</Text>
    </Box>
  })

  on('ui.render', { component: 'Pane', requestId: FORECAST_PANE }, async ($, e) => {
    const { Box, Text } = $.ui.resolve(e)
    const p = await read($, pool)
    if (!p) return <Text color="inactive">Pool not connected.</Text>
    const r = p.forecast?.renewals
    return <Box flexDirection="column" paddingX={1}>
      <Text bold>{p.error ? 'Pool outlook · last known state' : `Pool outlook · next 24h${e.surface === 'terminal' ? ' · ↓ details' : ''}`}</Text>
      {poolCoverage(p) && <Text color="warning">{poolCoverage(p)}</Text>}
      <Text color="inactive">All-model windows only; Fable weekly is excluded.</Text>
      {!r && <Text color="inactive">Collecting reset readings.</Text>}
      {r && <Text color="inactive" wrap="truncate-end">Projected coverage ~{longDur(r.coveredSeconds * 1000)}{r.partial ? ` · possible ~${longDur(r.possibleSeconds * 1000)} · partial` : ''}</Text>}
      {r && <Text wrap="truncate-end">24h {Array.from({ length: 24 }, (_, hour) => {
        const start = p.updatedAt + hour * 3600000
        const spans = r.segments.filter(s => s.at < start + 3600000 && s.until > start)
        return spans.length && spans.every(s => s.available.length) ? '━' : spans.some(s => s.available.length || s.uncertain.length) ? '┄' : '·'
      }).join('')} · now → +24h</Text>}
      <Text color="inactive">━ projected ready · ┄ uncertain/mixed · · unavailable</Text>
      {r?.segments.slice(0, 3).map(s => <Text wrap="truncate-end">{s.at === p.updatedAt ? 'Now' : `+${longDur(s.at - p.updatedAt)}`}–+{longDur(s.until - p.updatedAt)} · {s.available.length} ready{s.uncertain.length ? ` + ${s.uncertain.length} uncertain` : ''}</Text>)}
      {r?.resets.map(s => <Text color="inactive" wrap="truncate-end">↻ {s.account} {s.window} · in {longDur(s.at - p.updatedAt)}</Text>)}
      <Text color="inactive" wrap="truncate-end">Same account-wide burn · first known resets only · later renewals unknown.</Text>
      <Text bold>Workloads · measured quota pace</Text>
      {!p.forecast?.workloads?.length && <Text color="inactive">Collecting model, effort and cache observations.</Text>}
      {p.forecast?.workloads?.map(w => <Box flexDirection="column">
        <Text wrap="truncate-end">{w.workload} · {w.requests} sampled replies · cache {w.cacheReadShare == null ? '?' : `${Math.round(w.cacheReadShare * 100)}%`} · thinking {w.medianThinkingTokens == null ? '?' : Math.round(w.medianThinkingTokens)} tokens/reply</Text>
        <Text color="inactive" wrap="truncate-end">{w.windows.length ? w.windows.map(v => `${v.label} ${v.window}: ${Math.round(v.ratePerHour * 10) / 10} pp/h`).join(' · ') : 'Collecting quota evidence'}</Text>
      </Box>)}
      <Text color="inactive">Other machines may contribute to measured quota burn.</Text>
    </Box>
  })

  on('ui.render', { component: 'Pane', requestId: HEALTH_PANE }, async ($, e) => {
    const { Box, Text } = $.ui.resolve(e)
    const p = await read($, pool)
    if (!p) return <Text color="inactive">Pool not connected. Run claude doctor to check the background service.</Text>
    const health = p.health
    return <Box flexDirection="column" paddingX={1}>
      <Text bold>{p.error ? 'Pool health · last known state' : 'Pool health'}</Text>
      {!health && <Text color="inactive">Restart the pool after active work finishes to enable health checks.</Text>}
      {health && <>
        <Text wrap="truncate-end">{health.supervisor} · backend {health.backend}{health.draining ? ' · draining' : ''} · {health.failures} failed checks</Text>
        <Text color="inactive" wrap="truncate-end">Requests {health.activeRequests}/{health.requestLimit} · metadata {health.storageError ? 'needs attention' : 'ready'} · restarts {p.recovery?.restarts ?? 0}</Text>
      </>}
      <Text color="inactive">Quota limits and login issues appear in the account panel.</Text>
      <Text bold>Recent incidents{e.surface === 'terminal' ? ' · ↓ details' : ''}</Text>
      {!p.incidents?.length && <Text color="inactive">No incidents recorded.</Text>}
      {p.incidents?.map(i => <Text wrap="truncate-end">{longDur(p.updatedAt - i.at)} ago · {i.message}{i.account ? ` · ${accountLabel(i.account, p.accounts)}` : ''}{i.status ? ` · HTTP ${i.status}` : ''}</Text>)}
      <Text color="inactive">Operational metadata only · latest 50 of 256 retained events.</Text>
    </Box>
  })

  on('ui.press', async ($, e, next) => {
    if (e.plugin !== 'cockpit' || !e.element.startsWith('pool-')) return next(e)
    if (e.element === 'pool-phone') {
      $.ui.log(await phoneLink($))
      return next(e)
    }
    const p = await read($, pool)
    if (['pool-history', 'pool-sessions', 'pool-forecast', 'pool-health'].includes(e.element)) {
      const id = e.element === 'pool-health' ? HEALTH_PANE : e.element === 'pool-sessions' ? SESSIONS_PANE : e.element === 'pool-forecast' ? FORECAST_PANE : HISTORY_PANE
      await $.ui.open({ id, title: e.element.slice(5), rows: id === FORECAST_PANE ? 18 : 14, focus: true, closeOnEscape: true })
    } else if (e.element === 'pool-unpin') await poolRequest($, { action: 'unpin' })
    else {
      const [action, name] = e.element.slice(5).split(':')
      const account = p?.accounts.find(a => a.name === name)
      if (account) await poolRequest($, { action: action === 'pause' && account.paused ? 'resume' : action, account: name })
    }
    await refreshPool($)
    return next(e)
  })

  on('command.run', { command: 'machine' }, async $ => {
    if ((await $.ui.panes()).some(p => p.id === PANE)) {
      await $.ui.close({ id: PANE })
      return { text: 'Machine pane closed.' }
    }
    startPolling($)
    await $.ui.open({ id: PANE, title: 'machine' })
    return { text: 'Machine pane opened. /machine again closes it.' }
  })

  on('command.run', { command: 'wrap' }, async $ => {
    const off = await toggleWrap($)
    return { text: off ? 'Auto-wrap off: agents keep going to the limit.' : 'Auto-wrap on: agents wrap up before the limit.' }
  })

  on('ui.press', { element: WRAP_KEY }, async ($, e, next) => {
    if (e.plugin === 'cockpit') await toggleWrap($)
    return next(e)
  })

  on('ui.close', ($, e, next) => {
    if (e.id === PANE) stopPolling()
    return next(e)
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const m = await read($, machine)
    if (e.surface === 'terminal') {
      const { Box, Raster, Text } = $.ui.resolve(e)
      if (!m) return <Text color={C.soft}> sampling…</Text>
      // Overfill by a few rows and columns: the pane clips what doesn't fit,
      // and this way no strip of its grey fill shows around the drawing.
      const canvas = drawMachine(m, e.props.bodyColumns + 2, (e.viewport?.rows ?? 0) + 4, await $.clock.now())
      return (
        <Box>
          <Raster key="machine" columns={canvas.columns} rows={canvas.rows} cells={canvas.toBase64()} />
        </Box>
      )
    }
    // Raster is terminal-only: the app gets vector sections instead.
    const { Box, Svg, Text } = $.ui.resolve(e)
    if (!m) return <Text>sampling…</Text>
    // Two columns that wrap: side by side when the pane is wide, stacked when
    // it's slim (the app then scales a section down to fit). The app's own
    // layout decides, so no guess at how wide a cell is.
    const sections = machineSections(m, await $.clock.now())
    // No width or height: a section keeps its own proportions, shrunk to a
    // slim pane as a whole (a fixed height would letterbox it with gaps).
    const draw = (c: Chip) => <Svg source={c.source} alt={c.alt} />
    return (
      <Box flexDirection="row" flexWrap="wrap" columnGap={3} rowGap={1} alignItems="flex-start">
        <Box flexDirection="column" gap={1}>
          {sections.left.map(draw)}
        </Box>
        <Box flexDirection="column" gap={1}>
          {sections.right.map(draw)}
        </Box>
      </Box>
    )
  })

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    const now = await $.clock.now()
    const isPreview = await read($, preview)
    const realQuota = await read($, quota)
    const q = isPreview ? sample(realQuota, now) : realQuota
    const five = q.active?.fiveHour
    const ps = isPreview ? null : await read($, pool)
    if (e.props.hasSurvey || (!five && !ps)) return next(e)

    // Other mods' bands (e.g. image-view's paste thumbnails) sit beneath this hook
    // in the chain; draw theirs under ours instead of swallowing them.
    const below = await next(e)

    // The app draws vectors: tiles there, the character-cell band in a terminal.
    if (e.surface !== 'terminal') {
      const { Box, Button, Svg } = $.ui.resolve(e)
      const l = isPreview ? sampleLive() : await read($, live)
      const { left, right } = desktopTiles(q, l, now, ps)
      const wv = wrapView(q, await read($, wrap), now, ps)
      const draw = (c: Chip) => <Svg source={c.source} alt={c.alt} width={c.width} height={c.height} />
      return (
        <Box flexDirection="column">
          <Box flexDirection="row" flexWrap="wrap" alignItems="center">
            {left.map(draw)}
            <Box flexGrow={1} />
            {wv.status && draw(item({ label: 'agents', value: wv.status, tone: 'warn' }, `Agents: ${wv.status}`))}
            <Button key={WRAP_KEY} label={wv.label} plain dimColor onPress={() => undefined} />
            <Box width={2} />
            {right.map(draw)}
          </Box>
          {below}
        </Box>
      )
    }

    const { Box, Button, Text } = $.ui.resolve(e)
    const l = isPreview ? sampleLive() : await read($, live)
    const wv = wrapView(q, await read($, wrap), now, ps)
    return (
      <Box flexDirection="column">
        {terminalBand(Box, Text, q, l, now, e.props.bodyColumns, {
          ...wv,
          button: label => <Button key={WRAP_KEY} label={label} plain dimColor onPress={() => undefined} />,
        }, ps)}
        {below}
      </Box>
    )
  })
}


// Sampling runs only while the pane is open.
let poll: { cancel: () => void } | undefined

function startPolling($: $) {
  poll?.cancel()
  void pollMachine($)
  poll = $.clock.every(MACHINE_EVERY, () => void pollMachine($))
}

function stopPolling() {
  poll?.cancel()
  poll = undefined
}

type TextEl = ReturnType<EngineInterface['ui']['resolve']>['Text']

const FIVE_H = 5 * 3600_000
const SEVEN_D = 7 * 24 * 3600_000

// How far through the window the clock is, 0-1: the bar's tick. Fill past
// the tick means burning faster than time passes.
function elapsed(w: Window, span: number, now: number) {
  if (w.resetsAt === undefined) return undefined
  return Math.max(0, Math.min(1, 1 - (w.resetsAt - now) / span))
}

// Quiet until it matters: a muted amber past the expand level, red past switch.
function toneFor(pct: number): Tone {
  return pct >= SWITCH_AT ? 'crit' : pct >= EXPAND_AT ? 'warn' : 'fg'
}

const DAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat']

// When the next window starts, on the local clock: "14:30", or "Mon 14:30"
// when it's not today.
function clockAt(at: number, now: number) {
  const d = new Date(at)
  const hm = `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
  return new Date(now).toDateString() === d.toDateString() ? hm : `${DAYS[d.getDay()]} ${hm}`
}

function windowItem(label: string, w: Window, span: number, now: number, warn?: { text: string; tone: Tone }) {
  const resets = w.resetsAt !== undefined ? longDur(w.resetsAt - now) : undefined
  // Normally when it resets and the clock time the next window starts; running
  // out early takes the first place, in its tone, and the reset follows faint.
  return item(
    {
      label,
      value: fmtPct(w.pct),
      tone: toneFor(w.pct),
      bar: { pct: w.pct, mark: elapsed(w, span, now) },
      note: warn?.text ?? (resets && `resets in ${resets}`),
      noteTone: warn?.tone,
      extra: warn ? resets && `resets in ${resets}` : w.resetsAt !== undefined ? clockAt(w.resetsAt, now) : undefined,
    },
    `${label} ${fmtPct(w.pct)} used${resets ? `, resets ${resets}` : ''}${warn ? `; ${warn.text}` : ''}`,
  )
}

function desktopTiles(q: Quota, l: Live, now: number, ps: PoolData | null = null) {
  const left: Chip[] = []
  const right: Chip[] = []
  const five = q.active?.fiveHour
  const week = q.active?.sevenDay

  if (five) {
    const f = project(five, q.pace, now, ps ? 95 : 100)
    const warn =
      f.runsOutIn !== undefined
        ? { text: `${ps ? 'switch' : 'out'} in ${etaText(f.runsOutIn)}`, tone: (f.runsOutIn < 60 * 60_000 ? 'crit' : 'warn') as Tone }
        : undefined
    left.push(windowItem('Session', five, FIVE_H, now, warn))
  }
  if (week) left.push(windowItem('Week', week, SEVEN_D, now))

  if (l.contextPct !== undefined) {
    const pct = Math.round(l.contextPct)
    const usd = l.usd !== undefined && l.usd > 0 ? `$${l.usd.toFixed(2)}` : undefined
    left.push(
      item(
        { label: 'Context', value: `${pct}%`, tone: toneFor(pct), bar: { pct }, note: usd },
        `Context ${pct}% full${usd ? `, session cost ${usd}` : ''}`,
      ),
    )
  }

  // The account in use; the switch hint when it's time.
  const target = ps ? undefined : switchTarget(q, now)
  if (q.active) {
    const others = q.others.map(o => `${o.label} ${otherText(o, now)}`)
    right.push(
      item(
        target
          ? { label: q.active.label, value: `→ /login ${target.label}`, tone: 'ok', note: `${otherText(target, now)} used` }
          : { label: 'on', value: q.active.label, dot: true, note: ps ? `${ps.error ? 'offline' : ps.mode} · ${ps.ready} ready` : undefined },
        target
          ? `On ${q.active.label}. Switch: /login ${target.label}, ${otherText(target, now)} used there`
          : `On ${q.active.label}${others.length ? `; ${others.join(', ')}` : ''}`,
      ),
    )
  }
  return { left, right }
}

type BoxEl = ReturnType<EngineInterface['ui']['resolve']>['Box']

// One styled run of terminal text; a line is a list of them, so it can be
// measured before it's drawn.
type Seg = { t: string; c: string; b?: boolean; d?: boolean }
const segLength = (segs: Seg[]) => segs.reduce((n, s) => n + [...s.t].length, 0)

// How much the line carries, richest first; the band takes the first that fits.
type Level = {
  words: boolean // "Session" rather than "5h"
  bar: number // cells, 0 for none
  sessionNote: 'long' | 'clock' | 'short' | 'tiny' | 'none'
  weekNote: 'long' | 'short' | 'none'
  context: boolean
  cost: boolean
  others: boolean
  wrap: 'long' | 'short' | 'none' // "auto-wrap on", "wrap on", or left to /wrap
}
const LEVELS: Level[] = [
  { words: true, bar: 14, sessionNote: 'long', weekNote: 'long', context: true, cost: true, others: true, wrap: 'long' },
  { words: true, bar: 12, sessionNote: 'long', weekNote: 'long', context: true, cost: true, others: false, wrap: 'long' },
  { words: true, bar: 10, sessionNote: 'clock', weekNote: 'short', context: true, cost: true, others: false, wrap: 'short' },
  { words: true, bar: 8, sessionNote: 'short', weekNote: 'none', context: true, cost: false, others: false, wrap: 'short' },
  { words: true, bar: 0, sessionNote: 'short', weekNote: 'none', context: true, cost: false, others: false, wrap: 'short' },
  { words: false, bar: 0, sessionNote: 'tiny', weekNote: 'none', context: true, cost: false, others: false, wrap: 'short' },
  { words: false, bar: 0, sessionNote: 'none', weekNote: 'none', context: false, cost: false, others: false, wrap: 'short' },
  { words: false, bar: 0, sessionNote: 'none', weekNote: 'none', context: false, cost: false, others: false, wrap: 'none' },
]
const GAP = 3

// The terminal's band: the app's quiet line in character cells. Theme colors,
// so it reads in light and dark terminals; color only when it matters. It
// carries as much as fits the width (LEVELS) and never wraps; capped so a
// very wide terminal doesn't strand the account at the far edge.
function terminalBand(
  Box: BoxEl,
  Text: TextEl,
  q: Quota,
  l: Live,
  now: number,
  columns: number,
  wv: { label: string; short: string; status?: string; button: (label: string) => RenderElement },
  ps: PoolData | null = null,
) {
  const five = q.active?.fiveHour
  const week = q.active?.sevenDay
  if (!five) return ps ? <Text color="inactive"> {ps.error ? 'pool unavailable' : `${ps.active ? accountLabel(ps.active, ps.accounts) : 'pool'} · quota ? · ${ps.ready} ready · ${ps.mode}`} </Text> : null
  const room = Math.min(columns, 170) - 2
  const toneColor = (t: Tone) => (t === 'crit' ? 'error' : t === 'warn' ? 'warning' : t === 'ok' ? 'success' : 'text')
  const f = project(five, q.pace, now, ps ? 95 : 100)
  const target = ps ? undefined : switchTarget(q, now)

  const resets = (w: Window, how: 'long' | 'clock' | 'short' | 'tiny') => {
    if (w.resetsAt === undefined) return undefined
    const left = w.resetsAt - now
    if (how === 'long') return `resets in ${longDur(left)} · ${clockAt(w.resetsAt, now)}`
    if (how === 'clock') return `↻ ${longDur(left)} · ${clockAt(w.resetsAt, now)}`
    if (how === 'short') return `↻ ${longDur(left)}`
    return `↻ ${dur(left)}`
  }

  const reading = (lv: Level, label: string, short: string, value: string, tone: Tone, bar?: { pct: number; mark?: number }) => {
    const segs: Seg[] = [
      { t: (lv.words ? label : short) + ' ', c: 'inactive' },
      { t: value, c: toneColor(tone), b: true },
    ]
    if (bar && lv.bar > 0) segs.push({ t: ' ', c: 'text' }, ...hairline(bar.pct, lv.bar, tone === 'fg' ? 'inactive' : toneColor(tone), bar.mark))
    return segs
  }

  const build = (lv: Level) => {
    const items: Seg[][] = []

    const session = reading(lv, 'Session', '5h', fmtPct(five.pct), toneFor(five.pct), { pct: five.pct, mark: elapsed(five, FIVE_H, now) })
    if (f.runsOutIn !== undefined) {
      // Running out early leads, in its tone; the reset follows quietly.
      const t: Tone = f.runsOutIn < 60 * 60_000 ? 'crit' : 'warn'
      session.push({ t: ` ${ps ? 'switch' : 'out'} in ${etaText(f.runsOutIn, !lv.words)}`, c: toneColor(t) })
      // The reset is what you plan around, so it stays as long as any note does.
      const after =
        lv.sessionNote === 'long' ? resets(five, 'long') : lv.sessionNote === 'clock' || lv.sessionNote === 'short' ? resets(five, 'short') : undefined
      if (after) session.push({ t: ` · ${after}`, c: 'inactive', d: true })
    } else if (lv.sessionNote !== 'none') {
      const note = resets(five, lv.sessionNote)
      if (note) session.push({ t: ` ${note}`, c: 'inactive' })
    }
    items.push(session)

    if (week) {
      const wk = reading(lv, 'Week', 'wk', fmtPct(week.pct), toneFor(week.pct), { pct: week.pct, mark: elapsed(week, SEVEN_D, now) })
      const note = lv.weekNote === 'none' ? undefined : resets(week, lv.weekNote === 'long' ? 'long' : 'short')
      if (note) wk.push({ t: ` ${note}`, c: 'inactive' })
      items.push(wk)
    }

    if (lv.context && l.contextPct !== undefined) {
      const pct = Math.round(l.contextPct)
      const ctx = reading(lv, 'Context', 'ctx', `${pct}%`, toneFor(pct), { pct })
      if (lv.cost && l.usd !== undefined && l.usd > 0) ctx.push({ t: ` $${l.usd.toFixed(2)}`, c: 'inactive' })
      items.push(ctx)
    }

    // The account: who you're on, or where to switch.
    let account: Seg[] = []
    if (q.active) {
      if (target) {
        account = [{ t: `→ /login ${target.label}`, c: 'success', b: true }]
        if (lv.words) account.push({ t: ` · ${otherText(target, now)} used`, c: 'inactive' })
      } else {
        account = [
          { t: '● ', c: 'success' },
          { t: q.active.label, c: 'text' },
        ]
        if (ps && lv.words) account.push({ t: ` · ${ps.error ? 'offline' : ps.mode} · ${ps.ready} ready`, c: 'inactive' })
        if (!ps && lv.others) for (const o of q.others) account.push({ t: `  ○ ${o.label} ${otherText(o, now)}`, c: 'inactive', d: true })
      }
    }
    // The guard's switch rides beside the account, with its status when it has one.
    const status: Seg[] = wv.status && lv.wrap !== 'none' ? [{ t: wv.status, c: 'warning' }] : []
    const wrapLabel = lv.wrap === 'long' ? wv.label : lv.wrap === 'short' ? wv.short : ''
    const length =
      items.reduce((n, it) => n + segLength(it), 0) +
      GAP * items.length +
      (status.length ? segLength(status) + GAP : 0) +
      (wrapLabel ? [...wrapLabel].length + GAP : 0) +
      segLength(account)
    return { items, account, status, wrapLabel, length }
  }

  const chosen = LEVELS.map(build).find(b => b.length <= room) ?? build(LEVELS[LEVELS.length - 1]!)
  const draw = (segs: Seg[]) => (
    <Text wrap="truncate-end">
      {segs.map(s => (
        <Text color={s.c} bold={s.b} dimColor={s.d}>
          {s.t}
        </Text>
      ))}
    </Text>
  )
  return (
    <Box flexDirection="row" columnGap={GAP} paddingX={1} width={room + 2}>
      {chosen.items.map(draw)}
      <Box flexGrow={1} />
      {chosen.status.length > 0 && draw(chosen.status)}
      {chosen.wrapLabel && wv.button(chosen.wrapLabel)}
      {chosen.account.length > 0 && draw(chosen.account)}
    </Box>
  )
}

// A hairline meter in whole cells: heavy where used, light where not, and a
// tick where the clock is in the window.
function hairline(pct: number, cells: number, color: string, mark?: number): Seg[] {
  const used = Math.round((Math.max(0, Math.min(100, pct)) / 100) * cells)
  const tick = mark === undefined ? -1 : Math.min(cells - 1, Math.floor(mark * cells))
  const runs: Seg[] = []
  for (let i = 0; i < cells; i++) {
    const cell: Seg = i === tick ? { t: '│', c: 'text' } : i < used ? { t: '━', c: color } : { t: '─', c: 'inactive', d: true }
    const last = runs[runs.length - 1]
    if (last && last.c === cell.c && !!last.d === !!cell.d) last.t += cell.t
    else runs.push(cell)
  }
  return runs
}

async function refreshWrap($: $) {
  const home = (await $.env.get('HOME')) ?? ''
  const off = await $.fs.exists(`${home}/${NOWRAP}`).catch(() => false)
  const found = await Promise.all(WRAPUPS.map(f => $.fs.exists(`${home}/${f}`).catch(() => false)))
  await update($, wrap, () => ({ off, wrapping: found.some(Boolean) }))
}

// Creates or deletes ~/.claude/NOWRAP; $.fs has no delete, so rm does that.
// Answers whether auto-wrap is now off.
async function toggleWrap($: $) {
  const p = await read($, pool)
  if (p) {
    const result = await poolRequest($, { action: 'wrap', enabled: !p.wrap })
    if (!result) return !p.wrap
    await refreshPool($)
    return !result.wrap
  }
  const home = (await $.env.get('HOME')) ?? ''
  const path = `${home}/${NOWRAP}`
  const off = await $.fs.exists(path).catch(() => false)
  if (off) await $.process.run(['rm', '-f', path], { timeoutMs: 5_000 })
  else await $.fs.write(path, `auto-wrap switched off from the cockpit band\n`)
  await refreshWrap($)
  return !off
}

// What the band says about the guard: the switch, and a status while it's
// wrapping up or about to (the guard's own math: the 5h % now, projected at
// the burn rate to when agents would be done, or to the reset if sooner).
function wrapView(q: Quota, wr: Wrap, now: number, ps: PoolData | null = null) {
  if (ps) return { label: ps.wrap ? 'auto-wrap on' : 'auto-wrap off', short: ps.wrap ? 'wrap on' : 'wrap off', status: ps.error ? 'pool unavailable' : ps.ready === 0 && ps.accounts.length > 0 && ps.accounts.every(a => a.status === 'paused') ? 'accounts paused' : ps.ready === 0 && ps.accounts.every(a => a.status !== 'unknown') ? 'no account ready' : undefined }
  const label = wr.off ? 'auto-wrap off' : 'auto-wrap on'
  const short = wr.off ? 'wrap off' : 'wrap on'
  if (wr.wrapping) return { label, short, status: 'wrapping up' }
  const five = q.active?.fiveHour
  const rate = q.guardRate
  if (wr.off || !five || rate === undefined || !q.active || five.resetsAt === undefined || five.resetsAt <= now) return { label, short }
  const proj = Math.min(100, five.pct + rate * (now - q.active.seenAt))
  const ahead = Math.min(100, proj + rate * Math.max(0, Math.min(WRAP_MARGIN, five.resetsAt - now)))
  return ahead >= WRAP_AT ? { label, short, status: 'wrap-up due' } : { label, short }
}

function longDur(ms: number) {
  const m = Math.max(0, Math.round(ms / 60_000))
  if (m < 60) return `${m}m`
  const h = Math.floor(m / 60)
  if (h < 24) return m % 60 ? `${h}h ${m % 60}m` : `${h}h`
  return h % 24 ? `${Math.floor(h / 24)}d ${h % 24}h` : `${Math.floor(h / 24)}d`
}

async function measureLive($: $, m: { context: { tokens?: number; percent?: number }; cost?: { usd: number } }) {
  const pooled = await $.env.get('COCKPIT_BASE_URL')
  await update($, live, () => ({ contextPct: m.context.percent, contextTokens: m.context.tokens, usd: pooled ? undefined : m.cost?.usd }))
}

function sampleLive(): Live {
  return { contextPct: 42, contextTokens: 84_200, usd: 4.32 }
}

function fmtTok(n: number) {
  if (n < 1000) return String(n)
  if (n < 1_000_000) return `${(n / 1000).toFixed(n < 10_000 ? 1 : 0)}k`
  return `${(n / 1_000_000).toFixed(1)}M`
}

async function pollMachine($: $) {
  if (isCollecting) return
  isCollecting = true
  try {
    const now = await $.clock.now()
    const shared = (await $.store.get(MACHINE)) as Machine | undefined
    // Another session sampled moments ago: reuse it rather than run ps again.
    const snapshot =
      shared && now - shared.at < MACHINE_EVERY - 2_000 ? shared : await collect($, now, shared?.trend ?? [])
    if (snapshot !== shared) await $.store.set(MACHINE, snapshot)
    await update($, machine, () => snapshot)
  } finally {
    isCollecting = false
  }
}

async function collect($: $, now: number, trend: [number, number][]) {
  const opts = { timeoutMs: 5_000 }
  const [sys, vm, ps] = await Promise.all([
    $.process.run(SYS, opts),
    $.process.run(VM, opts),
    $.process.run(PS, opts),
  ])
  const pids = claudePids(ps.stdout)
  const lsof = pids.length > 0 ? (await $.process.run(lsofCwd(pids), opts)).stdout : ''

  // Each session's transcript: find them per directory, read the last lines.
  const home = (await $.env.get('HOME')) ?? ''
  const sessions = sessionsOf(ps.stdout, lsof)
  const cwds = [...new Set(sessions.map(s => s.cwd).filter((cwd): cwd is string => cwd !== undefined))]
  const found = new Map(
    await Promise.all(
      cwds.map(async cwd => [cwd, (await $.process.run(findTranscripts(projectDir(home, cwd)), opts)).stdout] as const),
    ),
  )
  const files = assignTranscripts(sessions, found)
  const tails = new Map(
    await Promise.all([...files].map(async ([pid, file]) => [pid, (await $.process.run(tailTranscript(file), opts)).stdout] as const)),
  )

  // Its subagents and workflow agents, written to in the last 15 minutes.
  const agents = new Map(
    await Promise.all(
      [...files].map(async ([pid, file]) => {
        const folder = sessionFolder(file)
        const found = await $.process.run(findAgents(folder), opts)
        const runs = statLines(found.stdout)
        const ids = [...new Set(runs.map(r => workflowIdOf(r.file)).filter((id): id is string => id !== undefined))]
        const names = new Map(
          await Promise.all(
            ids.map(async id => [id, (await $.process.run(workflowNameOf(folder, id), opts)).stdout.trim() || id] as const),
          ),
        )
        const list: AgentFile[] = await Promise.all(
          runs.map(async ({ file: agent, mtime }) => {
            const tail = (await $.process.run(tailAgent(agent), opts)).stdout
            const meta = await $.fs.read(agent.replace(/\.jsonl$/, '.meta.json')).catch(() => '{}')
            const description = (JSON.parse(meta) as { description?: string }).description
            const id = workflowIdOf(agent)
            return { file: agent, mtime, tail, description, workflow: id ? names.get(id) : undefined }
          }),
        )
        return [pid, list] as const
      }),
    ),
  )
  return parse({ sys: sys.stdout, vm: vm.stdout, ps: ps.stdout, lsof, tails, agents }, now, trend)
}


// Made-up numbers that light every part of the card: 88% with a pace that
// runs out 70 min early, and a second account with room.
function sample(real: Quota, now: number): Quota {
  const id = real.active?.id ?? 'preview'
  return {
    active: {
      id,
      label: real.active?.label ?? 'account',
      fiveHour: { pct: 88, resetsAt: now + 90 * 60_000 },
      sevenDay: { pct: 32, resetsAt: now + 4 * 24 * 3600_000 },
      seenAt: now,
    },
    others: [{ id: 'preview-other', label: 'gmail', fiveHour: { pct: 20, resetsAt: now + 3 * 3600_000 }, seenAt: now }],
    pace: { rate: 12 / (20 * 60_000), pctNow: 88 },
    guardRate: 12 / (20 * 60_000),
  }
}

async function whoAmI($: $): Promise<{ id: string; label: string } | undefined> {
  try {
    const home = await $.env.get('HOME')
    const config = JSON.parse(await $.fs.read(`${home}/.claude.json`))
    const account = config.oauthAccount
    if (!account?.accountUuid) return undefined
    const domain = String(account.emailAddress ?? '').split('@')[1] ?? ''
    // Show the provider domain, never the email address.
    return { id: account.accountUuid, label: domain.split('.')[0] || 'account' }
  } catch {
    return undefined
  }
}

function toWindow(limits: SessionRateLimit[], kind: string): Window | undefined {
  const one = limits.find(l => l.kind === kind)
  if (!one) return undefined
  return { pct: one.percentUsed, resetsAt: one.resetsAt ? Date.parse(one.resetsAt) : undefined }
}

async function measure($: $, limits: SessionRateLimit[]) {
  if (await $.env.get('COCKPIT_BASE_URL')) return refreshPool($)
  const me = await whoAmI($)
  const fiveHour = toWindow(limits, 'five_hour')
  if (!me || !fiveHour) return refresh($)

  const now = await $.clock.now()
  const accounts = ((await $.store.get(ACCOUNTS)) ?? {}) as Record<string, Account>
  accounts[me.id] = { ...me, fiveHour, sevenDay: toWindow(limits, 'seven_day'), seenAt: now }
  await $.store.set(ACCOUNTS, accounts)

  const samples = ((await $.store.get(SAMPLES)) ?? {}) as Samples
  // Only this window's readings: an earlier window's would bend the fit.
  const start = fiveHour.resetsAt !== undefined ? fiveHour.resetsAt - FIVE_H : now - FIVE_H
  const mine = (samples[me.id] ?? []).filter(([t, pct]) => t >= start && pct <= fiveHour.pct)
  samples[me.id] = [...mine, [now, fiveHour.pct]].slice(-200) as [number, number][]
  await $.store.set(SAMPLES, samples)

  await refresh($)
}

async function refresh($: $) {
  if (await $.env.get('COCKPIT_BASE_URL')) return refreshPool($)
  const me = await whoAmI($)
  const now = await $.clock.now()
  const accounts = ((await $.store.get(ACCOUNTS)) ?? {}) as Record<string, Account>
  const samples = ((await $.store.get(SAMPLES)) ?? {}) as Samples
  const active = me ? accounts[me.id] : undefined
  const others = Object.values(accounts)
    .filter(a => a.id !== me?.id)
    .sort((a, b) => a.label.localeCompare(b.label))

  await update($, quota, q => ({
    ...q,
    active,
    others,
    pace: active?.fiveHour ? pace(samples[active.id] ?? [], active.fiveHour, active.seenAt, now) : undefined,
    guardRate: active ? guardRate(samples[active.id] ?? [], now) : undefined,
  }))
}

// The burn as a weighted least-squares slope through this window's readings
// of the last hour, recent ones counting most, so one jump doesn't swing it
// and a change of pace shows within minutes. Through a quiet spell (no
// reading for over IDLE_AFTER) it eases off; the last reading is carried
// forward to now at that rate, at most MAX_CARRY.
function pace(samples: [number, number][], five: Window, seenAt: number, now: number): Pace | undefined {
  const start = five.resetsAt !== undefined ? five.resetsAt - FIVE_H : -Infinity
  const pts = samples.filter(([t]) => t >= start && t <= now && now - t <= PACE_LOOKBACK)
  const first = pts[0]
  const last = pts[pts.length - 1]
  if (!first || !last || pts.length < PACE_MIN_READINGS || last[0] - first[0] < PACE_MIN_SPAN) return undefined

  let sw = 0
  let sx = 0
  let sy = 0
  for (const [t, y] of pts) {
    const w = Math.exp(-(now - t) / PACE_TAU)
    sw += w
    sx += w * t
    sy += w * y
  }
  const mx = sx / sw
  const my = sy / sw
  let sxx = 0
  let sxy = 0
  for (const [t, y] of pts) {
    const w = Math.exp(-(now - t) / PACE_TAU)
    sxx += w * (t - mx) ** 2
    sxy += w * (t - mx) * (y - my)
  }
  const slope = sxx > 0 ? Math.max(0, sxy / sxx) : 0

  const gap = Math.max(0, now - Math.max(seenAt, last[0]))
  const rate = gap > IDLE_AFTER ? slope * Math.exp(-(gap - IDLE_AFTER) / PACE_TAU) : slope
  return { rate, pctNow: Math.min(100, five.pct + rate * Math.min(gap, MAX_CARRY)) }
}

// The usage guard's rate, as it computes it: first to last reading of the
// last 45 minutes. Only for "wrap-up due", which has to match the guard.
function guardRate(samples: [number, number][], now: number) {
  const recent = samples.filter(([t]) => now - t <= GUARD_WINDOW)
  const first = recent[0]
  const last = recent[recent.length - 1]
  if (!first || !last || recent.length < 2) return undefined
  return Math.max(0, (last[1] - first[1]) / Math.max(1, last[0] - first[0]))
}

// Where the window lands at its reset, and when it runs out if that comes
// first by at least WARN_SPARE (closer than that, the reset wins anyway).
function project(five: Window, p: Pace | undefined, now: number, limit = 100) {
  if (!p || five.resetsAt === undefined || five.resetsAt <= now) return {}
  const toReset = five.resetsAt - now
  const atReset = Math.min(100, p.pctNow + p.rate * toReset)
  if (p.rate <= 0 || atReset < limit) return { atReset }
  const runsOutIn = Math.max(0, limit - p.pctNow) / p.rate
  return toReset - runsOutIn >= WARN_SPARE ? { atReset, runsOutIn, spare: toReset - runsOutIn } : { atReset }
}

// A forecast to the minute only when it's close: "~1h 05m" to five minutes
// under two hours, to fifteen beyond; `tight` for the narrowest band.
function etaText(ms: number, tight = false) {
  const m = Math.max(1, Math.round(ms / 60_000))
  const step = m < 30 ? 1 : m < 120 ? 5 : 15
  const r = Math.max(step, Math.round(m / step) * step)
  const text = tight ? dur(r * 60_000) : longDur(r * 60_000)
  return m < 30 ? text : `~${text}`
}

function effective(w: Window | undefined, now: number) {
  if (!w) return undefined
  return w.resetsAt && w.resetsAt <= now ? 0 : w.pct
}

function otherText(o: Account, now: number) {
  const five = effective(o.fiveHour, now)
  const week = effective(o.sevenDay, now)
  if (five === undefined) return '?'
  const parts = [five === 0 && o.fiveHour?.pct ? 'fresh' : fmtPct(five)]
  if (week !== undefined && week >= 80) parts.push(`wk ${fmtPct(week)}`)
  return parts.join(' · ')
}

function switchTarget(q: Quota, now: number) {
  const five = q.active?.fiveHour?.pct ?? 0
  const week = q.active?.sevenDay?.pct ?? 0
  if (five < SWITCH_AT && week < 95) return undefined
  return q.others
    .filter(o => (effective(o.fiveHour, now) ?? 100) < 60 && (effective(o.sevenDay, now) ?? 0) < 90)
    .sort((a, b) => (effective(a.fiveHour, now) ?? 0) - (effective(b.fiveHour, now) ?? 0))[0]
}

function fmtPct(pct: number) {
  return `${Math.round(pct * 10) / 10}%`
}

function dur(ms: number) {
  const m = Math.max(0, Math.round(ms / 60_000))
  if (m < 60) return `${m}m`
  const h = Math.floor(m / 60)
  if (h < 24) return `${h}h${String(m % 60).padStart(2, '0')}`
  return `${Math.floor(h / 24)}d${h % 24}h`
}


async function poolRequest($: $, body?: Record<string, unknown>): Promise<PoolData | null> {
  const base = await $.env.get('COCKPIT_BASE_URL')
  const tokenFile = await $.env.get('COCKPIT_TOKEN_FILE')
  if (tokenFile && tokenFile !== configuredTokenFile) return null
  let token: string | undefined
  try { token = tokenFile ? (await $.fs.read(tokenFile)).trim() : await $.env.get('ANTHROPIC_AUTH_TOKEN') } catch { return null }
  if (!base || !token) return null
  try {
    const url = new URL(base)
    if (url.protocol !== 'http:' || url.hostname !== '127.0.0.1') return null
    const response = await $.http.fetch(`${base}/cockpit/${await $.session.id()}`, {
      method: body ? 'POST' : 'GET',
      headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json', 'x-cockpit-project': encodeURIComponent(await $.env.get('COCKPIT_PROJECT') ?? '') },
      body: body ? JSON.stringify(body) : undefined,
    })
    if (!response.ok) return null
    const result = JSON.parse(response.text) as PoolData
    // shortcut: read telemetry separately until the running bridge includes Fable.
    if (Array.isArray(result.accounts) && result.accounts.some(a => !('fableWeekly' in a)) ) {
      try {
        const telemetry = await $.http.fetch(controlURL + '/internal/usage', { headers: { Authorization: `Bearer ${token}` } })
        if (telemetry.ok) {
          const extra = JSON.parse(telemetry.text) as Pick<PoolData, 'accounts'>
          if (Array.isArray(extra.accounts)) for (const a of result.accounts) {
            const source = extra.accounts.find(b => b.id === a.id && b.name === a.name)
            const w = source?.fableWeekly
            if (w && Number.isFinite(w.pct) && w.pct >= 0) a.fableWeekly = w
            else if (source && 'fableWeekly' in source) a.fableWeekly = undefined
          }
        }
      } catch { /* Keep the main pool view available when telemetry is offline. */ }
    }
    return Array.isArray(result.accounts) ? result : null
  } catch { return null }
}

async function refreshPool($: $) {
  if (!(await $.env.get('COCKPIT_BASE_URL'))) return
  const previous = await read($, pool)
  const result = await poolRequest($)
  if (!result) {
    await update($, pool, () => ({ ...(previous ?? { active: null, mode: 'auto' as const, wrap: true, ready: 0, accounts: [], updatedAt: 0 }), error: true }))
    await update($, quota, q => ({ ...q, pace: undefined }))
    return
  }
  await update($, pool, () => result)
  const accounts = result.accounts.map(a => ({ ...a, fiveHour: a.fiveHour ?? undefined, sevenDay: a.sevenDay ?? undefined }))
  const active = accounts.find(a => a.name === result.active)
  const speed = result.forecast?.accounts?.find(a => a.name === active?.name)?.windows.fiveHour?.ratePerHour
  await update($, quota, q => ({ ...q, active, others: accounts.filter(a => a !== active),
    pace: active?.fiveHour && typeof speed === 'number' && Number.isFinite(speed) && speed >= 0 ? { rate: speed / 3600_000, pctNow: active.fiveHour.pct } : undefined,
    guardRate: undefined }))
  if (result.switch) {
    const event = result.switch
    let announce = false
    // Compare-and-set survives overlapping polls and Mod reloads; stale replies cannot replay a notice.
    await update($, switchNoted, at => {
      announce = event.at > at
      return Math.max(at, event.at)
    })
    if (announce) $.ui.log(`${accountLabel(event.from, accounts)} → ${accountLabel(event.to, accounts)} · ${event.reason}`)
  }
}

function accountLabel(name: string, accounts: { name: string; label: string }[]) {
  return accounts.find(a => a.name === name)?.label ?? name
}

function poolForecast(p: PoolData, compact = false) {
  const f = p.forecast
  if (!f) return 'Pool forecast · collecting pace'
  if (compact) {
    const capacity = !f.eligibleAccounts ? 'no ready accounts' : f.seconds == null ? 'collecting pace' : `~${dur(Math.round(f.seconds / 300) * 300_000)}`
    const range = f.secondsLow != null && f.secondsHigh != null && f.secondsHigh - f.secondsLow > Math.max(600, (f.seconds ?? 0) * .15) ? ` · range ${dur(Math.round(f.secondsLow / 300) * 300_000)}–${dur(Math.round(f.secondsHigh / 300) * 300_000)}` : f.secondsLow != null && f.secondsHigh == null ? ' · range unsettled' : ''
    return `Pool ${capacity}${range}${f.partial ? ' · partial' : ''}${f.paceQuality && f.seconds != null ? ` · ${f.paceQuality}` : ''} · live 5s`
  }
  const capacity = !f.eligibleAccounts ? 'no ready accounts' : f.seconds == null ? 'collecting pace' : `~${longDur(Math.round(f.seconds / 300) * 300_000)} at recent pace${f.partial ? ' · partial' : ''} (${f.estimatedAccounts}/${f.eligibleAccounts})${f.paceQuality ? ` · ${f.paceQuality}` : ''}`
  return `Pool forecast · ${capacity}`
}

function poolCoverage(p: PoolData) {
  const missing = p.accounts.filter(a => a.status === 'ready' && !a.sevenDay).map(a => a.label)
  if (missing.length) return `Weekly all models not reported: ${missing.join(', ')}. Capacity may run out sooner.`
  return p.accounts.some(a => a.status === 'ready' && !a.fiveHour) ? '5h readings missing; capacity may run out sooner.' : null
}

function poolRange(p: PoolData) {
  const f = p.forecast
  if (!f || f.seconds == null) return null
  const { secondsLow: low, secondsHigh: high } = f
  const range = low != null && high != null && high - low > Math.max(600, f.seconds * .15) ? `Allowance ~${longDur(Math.round(low / 300) * 300_000)}–${longDur(Math.round(high / 300) * 300_000)}` : low != null && high == null ? 'Allowance range not settled' : null
  return [range, f.resetBeforeLimit ? 'resets arrive first' : null].filter(Boolean).join(' · ') || null
}

function poolBar(w: Window | undefined, cells: number) {
  if (!w) return '┄'.repeat(cells)
  const pct = Math.max(0, Math.min(100, w.pct))
  const filled = pct > 0 ? Math.max(1, Math.floor(pct * cells / 100)) : 0
  return '━'.repeat(filled) + '─'.repeat(cells - filled)
}

function poolRate(p: PoolData, name: string, key: 'fiveHour' | 'sevenDay') {
  const w = p.forecast?.accounts?.find(a => a.name === name)?.windows[key]
  if (!w) return ''
  if (typeof w.ratePerHour === 'number' && w.ratePerHour > 0) return ` · ~${w.ratePerHour.toFixed(1)}%/h`
  return w.ratePerHour === 0 ? ' · no recent burn' : w.reason === 'reading stale' ? ' · stale' : w.reason === 'low signal' ? ' · low signal' : ' · collecting'
}

function poolPace(p: PoolData, name: string) {
  const windows = p.forecast?.accounts?.find(a => a.name === name)?.windows
  if (!windows) return 'collecting pace'
  return (['fiveHour', 'sevenDay'] as const).flatMap(key => {
    const w = windows[key]
    return w ? [`${key === 'fiveHour' ? '5h' : 'week'} ${w.ratePerHour != null && w.ratePerHour > 0 ? `~${w.ratePerHour.toFixed(1)}%/h` : w.reason}`] : []
  }).join(' · ') || 'collecting pace'
}

async function phoneLink($: $) {
  const task = await $.env.get('COCKPIT_CONTROL_TASK')
  return task && phoneURL ? 'Private Cockpit · ' + phoneURL + '#task=' + task : 'Run claude phone in a terminal to open private Cockpit. This existing task has not been reloaded.'
}
