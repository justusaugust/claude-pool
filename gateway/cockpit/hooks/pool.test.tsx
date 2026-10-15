import { describe, expect, mock, test } from 'claude-code/testing'

const NOW = Date.parse('2026-10-13T00:00:00+02:00')
const BAND = { plugin: 'cockpit', component: 'AbovePrompt', props: { hasSurvey: false, isWorking: false, maxRows: 4, bodyColumns: 120 } } as const
const initial = () => ({ active: 'team', mode: 'auto', wrap: true, ready: 2, updatedAt: NOW, accounts: [
  { id: 'org-gmail', name: 'gmail', label: 'Gmail', status: 'ready', paused: false, fableWeekly: { pct: 0, resetsAt: NOW + 7200000 }, fiveHour: { pct: 20, resetsAt: NOW + 3600000 }, sevenDay: { pct: 10 }, seenAt: NOW },
  { id: 'org-team', name: 'team', label: 'Work', status: 'ready', paused: false, fableWeekly: { pct: 0, resetsAt: NOW + 7200000 }, fiveHour: { pct: 30, resetsAt: NOW + 7200000 }, sevenDay: null, seenAt: NOW },
] })

describe('cockpit pooled routing', () => {
  test('separate Fable usage and missing all-model coverage stay explicit on both surfaces', async ($, on) => {
    mock.clock(on, { now: NOW }); mock.store(on)
    mock.env(on, { COCKPIT_BASE_URL: 'http://127.0.0.1:18341', ANTHROPIC_AUTH_TOKEN: 'fixture-client' })
    on('session.id', () => ({ value: 'pool-session' }))
    const state = initial()
    state.accounts[0]!.fableWeekly.pct = 99
    on('http.fetch', () => ({ value: { status: 200, ok: true, headers: {}, text: JSON.stringify(state) } }))
    on('session.measure', ($, e) => ({ changed: e.changed }) as never)
    on('ui.render', ($, e) => { const { Box } = $.ui.resolve(e); return <Box /> })
    await $.session.measure({ context: {}, rateLimits: [], changed: ['rateLimits'] } as never)
    for (const surface of ['terminal', 'desktop'] as const) {
      const pane = await $.ui.mount({ plugin: 'cockpit', component: 'Pane', requestId: 'cockpit-pool', surface,
        viewport: { columns: 100, rows: 40 }, props: { bodyColumns: 100 } } as never)
      expect(await pane.find({ type: 'Text', text: /week all.*not reported by Anthropic/ })).toBeDefined()
      expect(await pane.find({ type: 'Text', text: /Fable weekly.*0%/ })).toBeDefined()
      expect(await pane.find({ type: 'Text', text: /Fable weekly.*99%/ })).toBeDefined()
      expect(await pane.find({ type: 'Text', text: /Work.*Capacity may run out sooner/ })).toBeDefined()
      expect(await pane.find({ type: 'Text', text: /Forecast uses all-model windows; Fable weekly is separate/ })).toBeDefined()
      await pane.unmount()
    }
  })
  test('a confirmed weekly rejection is visible without inventing a percentage', async ($, on) => {
    mock.clock(on, { now: NOW }); mock.store(on)
    mock.env(on, { COCKPIT_BASE_URL: 'http://127.0.0.1:18341', ANTHROPIC_AUTH_TOKEN: 'fixture-client' })
    on('session.id', () => ({ value: 'pool-session' }))
    const state = initial()
    state.accounts[1]!.status = 'limited'
    Object.assign(state, { quotaWaiting: { since: NOW, resetsAt: NOW + 7200000 } })
    Object.assign(state.accounts[1]!, { quotaLimits: { sevenDay: { status: 'rejected', resetsAt: NOW + 7200000 } } })
    on('http.fetch', () => ({ value: { status: 200, ok: true, headers: {}, text: JSON.stringify(state) } }))
    on('session.measure', ($, e) => ({ changed: e.changed }) as never)
    on('ui.render', ($, e) => { const { Box } = $.ui.resolve(e); return <Box /> })
    await $.session.measure({ context: {}, rateLimits: [], changed: ['rateLimits'] } as never)
    const pane = await $.ui.mount({ plugin: 'cockpit', component: 'Pane', requestId: 'cockpit-pool', surface: 'terminal', props: { bodyColumns: 100 } } as never)
    expect(await pane.find({ type: 'Text', text: /Weekly limit reached.*resets in/ })).toBeDefined()
    expect(await pane.find({ type: 'Text', text: /Pool quota exhausted.*continuing automatically/ })).toBeDefined()
    expect(await pane.find({ type: 'Text', text: /week all.*not reported/ })).toBeDefined()
    await pane.unmount()
  })
  test('running bridge telemetry enriches only the matching account and tolerates an outage', async ($, on) => {
    mock.clock(on, { now: NOW }); mock.store(on)
    mock.env(on, { COCKPIT_BASE_URL: 'http://127.0.0.1:18341', ANTHROPIC_AUTH_TOKEN: 'fixture-client' })
    on('session.id', () => ({ value: 'pool-session' }))
    const state = initial()
    delete (state.accounts[1] as { fableWeekly?: unknown }).fableWeekly
    let online = true
    on('http.fetch', ($, e) => ({ value: { status: online ? 200 : e.url.endsWith('/internal/usage') ? 503 : 200,
      ok: online || !e.url.endsWith('/internal/usage'), headers: {}, text: JSON.stringify(e.url.endsWith('/internal/usage')
        ? { accounts: [{ id: 'org-team', name: 'team', fableWeekly: { pct: 42 } }, { id: 'wrong-org', name: 'gmail', fableWeekly: { pct: 88 } }] }
        : state) } }))
    on('session.measure', ($, e) => ({ changed: e.changed }) as never)
    on('ui.render', ($, e) => { const { Box } = $.ui.resolve(e); return <Box /> })
    await $.session.measure({ context: {}, rateLimits: [], changed: ['rateLimits'] } as never)
    const args = { plugin: 'cockpit', component: 'Pane', requestId: 'cockpit-pool', surface: 'terminal', props: { bodyColumns: 100 } }
    const first = await $.ui.mount(args as never)
    expect(await first.find({ type: 'Text', text: /Fable weekly.*42%/ })).toBeDefined()
    expect(await first.find({ type: 'Text', text: /Fable weekly.*88%/ })).toBeUndefined()
    await first.unmount()
    online = false
    await $.session.measure({ context: {}, rateLimits: [], changed: ['rateLimits'] } as never)
    const second = await $.ui.mount(args as never)
    expect(await second.find({ type: 'Text', text: /Fable weekly.*reading unavailable/ })).toBeDefined()
    expect(await second.find({ type: 'Text', text: /Pool unavailable/ })).toBeUndefined()
    await second.unmount()
  })
  for (const connected of [false, true]) test('phone entry point preserves the existing task: ' + connected, async ($, on) => {
    mock.env(on, connected ? { COCKPIT_CONTROL_TASK: 'public-task' } : {})
    let called = 0
    on('command.run', { command: 'phone' }, () => { called++; return { text: 'Private Cockpit · public fixture' } })
    const result = await $.command.run({ command: 'cockpit', args: 'phone', origin: { kind: 'composer' }, presentation: { isFullscreen: false, columns: 120 } })
    expect(called).toBe(0)
    expect(result.text?.includes(connected ? 'Private Cockpit' : 'has not been reloaded')).toBe(true)
  })
  test('three accounts share compact rows without hiding the personal weekly limit', async ($, on) => {
    mock.clock(on, { now: NOW }); mock.store(on)
    mock.env(on, { COCKPIT_BASE_URL: 'http://127.0.0.1:18341', ANTHROPIC_AUTH_TOKEN: 'fixture-client' })
    on('session.id', () => ({ value: 'pool-session' }))
    const state = initial()
    state.accounts.push({ id: 'org-max', name: 'max', label: 'Studio', status: 'limited', paused: false,
      fableWeekly: { pct: 0, resetsAt: NOW + 7200000 },
      fiveHour: { pct: 0, resetsAt: NOW + 3600000 }, sevenDay: { pct: 100 }, seenAt: NOW })
    on('http.fetch', () => ({ value: { status: 200, ok: true, headers: {}, text: JSON.stringify(state) } }))
    on('session.measure', ($, e) => ({ changed: e.changed }) as never)
    on('ui.render', ($, e) => { const { Box } = $.ui.resolve(e); return <Box /> })
    await $.session.measure({ context: {}, rateLimits: [], changed: ['rateLimits'] } as never)
    const pane = await $.ui.mount({ plugin: 'cockpit', component: 'Pane', requestId: 'cockpit-pool', surface: 'terminal',
      viewport: { columns: 80, rows: 24 }, props: { bodyColumns: 80 } } as never)
    expect(await pane.find({ type: 'Text', text: /Studio.*5h.*week.*100%/ })).toBeDefined()
    expect(await pane.find({ type: 'Button', key: 'pool-pin:max' })).toBeDefined()
    await pane.unmount()
  })
  test('uses the serving account, preserves both surfaces, and never reads native login', async ($, on) => {
    mock.clock(on, { now: NOW }); mock.store(on)
    mock.env(on, { HOME: '/fixture', COCKPIT_BASE_URL: 'http://127.0.0.1:18341', ANTHROPIC_AUTH_TOKEN: 'fixture-client' })
    on('session.id', () => ({ value: 'pool-session' }))
    on('fs.read', () => { throw new Error('Native login must not attribute pool quota') })
    on('http.fetch', () => ({ value: { status: 200, ok: true, headers: {}, text: JSON.stringify(initial()) } }))
    on('session.measure', ($, e) => ({ changed: e.changed }) as never)
    on('ui.render', ($, e) => { const { Box } = $.ui.resolve(e); return <Box /> })
    await $.session.measure({ context: { percent: 42 }, cost: { usd: 12 }, rateLimits: [], changed: ['context', 'rateLimits'] } as never)
    for (const surface of ['terminal', 'desktop'] as const) {
      const ui = await $.ui.mount({ ...BAND, surface } as never)
      if (surface === 'terminal') {
        expect(await ui.find({ type: 'Text', text: /Work/ })).toBeDefined()
        expect(await ui.find({ type: 'Text', text: /2 ready/ })).toBeDefined()
        expect(await ui.find({ type: 'Text', text: /\/login|\$12/ })).toBeUndefined()
      } else {
        const alts = (await ui.findAll({ type: 'Svg' })).map(x => String(x.props.alt))
        expect(alts.some(x => x.includes('Work'))).toBe(true)
      }
      await ui.unmount()
    }
    const narrow = await $.ui.mount({ ...BAND, surface: 'terminal', props: { ...BAND.props, bodyColumns: 44 } } as never)
    expect(await narrow.find({ type: 'Text', text: /\/login/ })).toBeUndefined()
    await narrow.unmount()
    const pane = await $.ui.mount({ plugin: 'cockpit', component: 'Pane', requestId: 'cockpit-pool', surface: 'terminal', props: { bodyColumns: 100 } } as never)
    expect(await pane.find({ type: 'Text', text: /week.*┄.*not reported/ })).toBeDefined()
    expect(await pane.find({ type: 'Button', key: 'pool-pin:gmail' })).toBeDefined()
    await pane.unmount()
  })

  test('pin, pause, unpin and pool wrap stay outside the model prompt and native files', async ($, on) => {
    mock.clock(on, { now: NOW }); mock.store(on)
    mock.env(on, { COCKPIT_BASE_URL: 'http://127.0.0.1:18341', ANTHROPIC_AUTH_TOKEN: 'fixture-client' })
    on('session.id', () => ({ value: 'pool-session' }))
    const actions: string[] = []
    on('fs.write', () => { throw new Error('Pool controls must not edit native guard files') })
    on('http.fetch', ($, e) => {
      if (e.init?.body) actions.push(JSON.parse(e.init.body).action)
      return { value: { status: 200, ok: true, headers: {}, text: JSON.stringify(initial()) } }
    })
    on('session.measure', ($, e) => ({ changed: e.changed }) as never)
    on('ui.render', ($, e) => { const { Box } = $.ui.resolve(e); return <Box /> })
    await $.session.measure({ context: {}, rateLimits: [], changed: ['rateLimits'] } as never)
    const pane = await $.ui.mount({ plugin: 'cockpit', component: 'Pane', requestId: 'cockpit-pool', surface: 'terminal', props: { bodyColumns: 100 } } as never)
    await pane.press({ key: 'pool-pin:gmail' }); await pane.press({ key: 'pool-pause:team' }); await pane.press({ key: 'pool-unpin' })
    expect(actions).toEqual(['pin', 'pause', 'unpin'])
    await pane.unmount()
    const band = await $.ui.mount({ ...BAND, surface: 'terminal' } as never)
    await band.press({ key: 'cockpit-wrap' })
    expect(actions.at(-1)).toBe('wrap')
    await band.unmount()
  })

  test('pool forecast, switch history and recovery warnings render on both surfaces', async ($, on) => {
    mock.clock(on, { now: NOW }); mock.store(on)
    mock.env(on, { COCKPIT_BASE_URL: 'http://127.0.0.1:18341', ANTHROPIC_AUTH_TOKEN: 'fixture-client' })
    on('session.id', () => ({ value: 'pool-session' }))
    const state = { ...initial(),
      forecast: { seconds: 7200, estimatedAccounts: 1, eligibleAccounts: 2, partial: true, nextReset: { account: 'Gmail', window: '5h', at: NOW + 3600000 } },
      history: [{ from: 'gmail', to: 'team', at: NOW - 60000, reason: 'quota or upstream failover', cacheReadTokens: 123, cacheCreationTokens: 456, thinkingDropped: true }],
      recovery: { state: 'ready', restarts: 1 }, interrupted: true, persistenceError: true }
    on('http.fetch', () => ({ value: { status: 200, ok: true, headers: {}, text: JSON.stringify(state) } }))
    on('session.measure', ($, e) => ({ changed: e.changed }) as never)
    on('ui.open', () => ({ value: { isPlaced: true } }))
    on('ui.render', ($, e) => { const { Box } = $.ui.resolve(e); return <Box /> })
    await $.session.measure({ context: {}, rateLimits: [], changed: ['rateLimits'] } as never)
    for (const surface of ['terminal', 'desktop'] as const) {
      const pane = await $.ui.mount({ plugin: 'cockpit', component: 'Pane', requestId: 'cockpit-pool', surface, props: { bodyColumns: 100 } } as never)
      expect(await pane.find({ type: 'Text', text: /Pool forecast.*partial/ })).toBeDefined()
      expect(await pane.find({ type: 'Text', text: /response was interrupted/ })).toBeDefined()
      expect(await pane.find({ type: 'Text', text: /could not be saved/ })).toBeDefined()
      await pane.press({ key: 'pool-history' })
      await pane.unmount()
      const history = await $.ui.mount({ plugin: 'cockpit', component: 'Pane', requestId: 'cockpit-pool-history', surface, props: { bodyColumns: 100 } } as never)
      expect(await history.find({ type: 'Text', text: /Gmail.*Work/ })).toBeDefined()
      expect(await history.find({ type: 'Text', text: /cache read 123.*thinking dropped/ })).toBeDefined()
      await history.unmount()
    }
  })

  test('backend pace, quota bars and allowance range refresh automatically without a model turn', async ($, on) => {
    const clock = mock.clock(on, { now: NOW }); mock.store(on)
    mock.env(on, { HOME: '/fixture', COCKPIT_BASE_URL: 'http://127.0.0.1:18341', ANTHROPIC_AUTH_TOKEN: 'fixture-client' })
    on('session.id', () => ({ value: 'pool-session' }))
    on('session.start', ($, e) => ({ cwd: e.cwd }))
    on('session.usage', () => ({ value: { startedAt: NOW, context: { window: 200000 }, rateLimits: [] } }))
    on('command.register', ($, e) => ({ value: { command: e.name } }))
    on('ui.panes', () => ({ value: [] }))
    on('fs.exists', () => ({ value: false }))
    on('fs.read', () => { throw new Error('Pooled pace must come from the backend') })
    on('ui.render', ($, e) => { const { Box } = $.ui.resolve(e); return <Box /> })
    const state = { ...initial(), forecast: { seconds: 7200, secondsLow: 3600, secondsHigh: 14400,
      estimatedAccounts: 1, eligibleAccounts: 2, partial: true, paceQuality: 'changing', resetBeforeLimit: true,
      nextReset: { account: 'Gmail', window: '5h', at: NOW + 3600000 },
      accounts: [{ name: 'team', label: 'Work', windows: { fiveHour: { ratePerHour: 12, reason: 'changing' } } }] } }
    state.accounts[1]!.fiveHour.pct = 90
    let polls = 0
    on('http.fetch', () => { polls++; return { value: { status: 200, ok: true, headers: {}, text: JSON.stringify(state) } } })
    await $.session.start({ cwd: '/fixture', surface: 'terminal', isInteractive: true })
    const before = polls
    state.accounts[1]!.fiveHour.pct = 91
    await clock.advance(5_000)
    expect(polls).toBeGreaterThan(before)
    for (const surface of ['terminal', 'desktop'] as const) {
      const pane = await $.ui.mount({ plugin: 'cockpit', component: 'Pane', requestId: 'cockpit-pool', surface,
        viewport: { columns: 100, rows: 40 }, props: { bodyColumns: 100 } } as never)
      expect(await pane.find({ type: 'Text', text: /5h.*━.*91%/ })).toBeDefined()
      expect(await pane.find({ type: 'Text', text: /5h ~12.0%\/h/ })).toBeDefined()
      expect(await pane.find({ type: 'Text', text: /Allowance ~1h–4h.*resets arrive first/ })).toBeDefined()
      expect(await pane.find({ type: 'Text', text: /Live · 5s/ })).toBeDefined()
      await pane.unmount()
    }
    const band = await $.ui.mount({ ...BAND, surface: 'terminal' } as never)
    expect(await band.find({ type: 'Text', text: /switch in 20m/ })).toBeDefined()
    await band.unmount()
    const compact = await $.ui.mount({ plugin: 'cockpit', component: 'Pane', requestId: 'cockpit-pool', surface: 'terminal',
      viewport: { columns: 80, rows: 24 }, props: { bodyColumns: 80 } } as never)
    expect(await compact.find({ type: 'Text', text: /12.0%\/h/ })).toBeDefined()
    expect(await compact.find({ type: 'Text', text: /week.*┄.*not reported/ })).toBeDefined()
    await compact.unmount()
  })

  test('offline data stays visibly stale and is never replaced by native account quota', async ($, on) => {
    mock.clock(on, { now: NOW }); mock.store(on)
    mock.env(on, { COCKPIT_BASE_URL: 'http://127.0.0.1:18341', ANTHROPIC_AUTH_TOKEN: 'fixture-client' })
    on('session.id', () => ({ value: 'pool-session' }))
    let online = true
    on('http.fetch', () => ({ value: { status: online ? 200 : 503, ok: online, headers: {}, text: JSON.stringify(initial()) } }))
    on('session.measure', ($, e) => ({ changed: e.changed }) as never)
    on('ui.render', ($, e) => { const { Box } = $.ui.resolve(e); return <Box /> })
    await $.session.measure({ context: {}, rateLimits: [], changed: ['rateLimits'] } as never)
    online = false
    await $.session.measure({ context: {}, rateLimits: [{ kind: 'five_hour', percentUsed: 99 }], changed: ['rateLimits'] } as never)
    const pane = await $.ui.mount({ plugin: 'cockpit', component: 'Pane', requestId: 'cockpit-pool', surface: 'terminal', props: { bodyColumns: 100 } } as never)
    expect(await pane.find({ type: 'Text', text: /last known state/ })).toBeDefined()
    expect(await pane.find({ type: 'Text', text: /30%/ })).toBeDefined()
    expect(await pane.find({ type: 'Text', text: /99%/ })).toBeUndefined()
    await pane.unmount()
  })

  test('auto-wrap requests one checkpoint only when the entire known pool is limited', async ($, on) => {
    mock.clock(on, { now: NOW }); mock.store(on)
    mock.env(on, { COCKPIT_BASE_URL: 'http://127.0.0.1:18341', ANTHROPIC_AUTH_TOKEN: 'fixture-client' })
    on('session.id', () => ({ value: 'pool-session' }))
    const state = initial()
    const notes: string[] = []
    on('http.fetch', () => ({ value: { status: 200, ok: true, headers: {}, text: JSON.stringify(state) } }))
    on('session.append', ($, e, next) => { notes.push(JSON.stringify(e.message)); return next(e) })
    on('turn.step', async function* ($, e) { return { turnId: e.turnId, index: e.index, answer: '', toolUses: [], stopReason: 'end_turn', usage: null } })
    const step = async (index: number) => { const stream = $.turn.step({ turnId: 'test', index, model: 'fixture', messageCount: 0 }); for await (const chunk of stream) void chunk }
    state.accounts[0]!.status = 'limited'
    await step(0); expect(notes.length).toBe(0)
    state.accounts[1]!.status = 'unknown'
    await step(1); expect(notes.length).toBe(0)
    state.accounts[1]!.status = 'limited'; state.ready = 0
    await step(2); await step(3); expect(notes.length).toBe(1)
    expect(notes[0]).toMatch(/record completed work and next actions/)
  })
  test('routing, session overview and renewal/workload panes retain both surfaces', async ($, on) => {
    mock.clock(on, { now: NOW }); mock.store(on)
    mock.env(on, { COCKPIT_BASE_URL: 'http://127.0.0.1:18341', ANTHROPIC_AUTH_TOKEN: 'fixture-client' })
    on('session.id', () => ({ value: 'pool-session' }))
    const state = { ...initial(), session: 'pool-session', routing: { reason: 'shared capacity', account: 'team', peers: 1 },
      sessions: [{ session: 'pool-session', account: 'team', model: 'claude-opus-5-5', effort: 'high', busy: true, reason: 'shared capacity', pin: null }],
      forecast: { seconds: null, estimatedAccounts: 0, eligibleAccounts: 2, partial: true, nextReset: null,
        renewals: { hours: 24, coveredSeconds: 3600, possibleSeconds: 7200, partial: true, segments: [{ at: NOW, until: NOW + 3600000, available: ['gmail'], uncertain: ['team'] }], resets: [] },
        workloads: [{ workload: 'opus/high', requests: 4, cacheReadShare: .8, medianThinkingTokens: 300, windows: [] }] } }
    on('http.fetch', () => ({ value: { status: 200, ok: true, headers: {}, text: JSON.stringify(state) } }))
    on('session.measure', ($, e) => ({ changed: e.changed }) as never)
    on('ui.render', ($, e) => { const { Box } = $.ui.resolve(e); return <Box /> })
    await $.session.measure({ context: {}, rateLimits: [], changed: ['rateLimits'] } as never)
    for (const surface of ['terminal', 'desktop'] as const) {
      for (const [requestId, expected] of [['cockpit-pool', /Route · Work · shared capacity/], ['cockpit-pool-sessions', /claude-opus-5-5.*high.*working/], ['cockpit-pool-forecast', /Projected coverage.*partial/]] as const) {
        const pane = await $.ui.mount({ plugin: 'cockpit', component: 'Pane', requestId, surface, viewport: { columns: 80, rows: 24 }, props: { bodyColumns: 80 } } as never)
        expect(await pane.find({ type: 'Text', text: expected })).toBeDefined()
        if (requestId === 'cockpit-pool-forecast') expect(await pane.find({ type: 'Text', text: /Collecting quota evidence/ })).toBeDefined()
        await pane.unmount()
      }
    }
  })

  test('health and bounded incident details render on both surfaces and refresh without a turn', async ($, on) => {
    mock.clock(on, { now: NOW }); mock.store(on)
    mock.env(on, { COCKPIT_BASE_URL: 'http://127.0.0.1:18341', ANTHROPIC_AUTH_TOKEN: 'fixture-client' })
    on('session.id', () => ({ value: 'pool-session' }))
    const state = { ...initial(), health: { backend: 'unresponsive', supervisor: 'launchd', failures: 3, checkedAt: NOW,
      draining: false, activeRequests: 1, requestLimit: 16, bufferedBytes: 0, storageError: false },
      incidents: [{ at: NOW - 60000, code: 'restart_deferred', message: 'Restart deferred until active responses finish', account: 'team' }] }
    on('http.fetch', () => ({ value: { status: 200, ok: true, headers: {}, text: JSON.stringify(state) } }))
    on('session.measure', ($, e) => ({ changed: e.changed }) as never)
    on('ui.open', () => ({ value: { isPlaced: true } }))
    on('ui.render', ($, e) => { const { Box } = $.ui.resolve(e); return <Box /> })
    await $.session.measure({ context: {}, rateLimits: [], changed: ['rateLimits'] } as never)
    for (const surface of ['terminal', 'desktop'] as const) {
      const pane = await $.ui.mount({ plugin: 'cockpit', component: 'Pane', requestId: 'cockpit-pool', surface,
        viewport: { columns: 80, rows: 24 }, props: { bodyColumns: 80 } } as never)
      expect(await pane.find({ type: 'Button', key: 'pool-health' })).toBeDefined()
      expect(await pane.find({ type: 'Text', text: /Health.*backend unresponsive.*1\/16/ })).toBeDefined()
      await pane.press({ key: 'pool-health' }); await pane.unmount()
      const health = await $.ui.mount({ plugin: 'cockpit', component: 'Pane', requestId: 'cockpit-pool-health', surface,
        viewport: { columns: 80, rows: 24 }, props: { bodyColumns: 80 } } as never)
      expect(await health.find({ type: 'Text', text: /launchd.*backend unresponsive.*3 failed checks/ })).toBeDefined()
      expect(await health.find({ type: 'Text', text: /Restart deferred.*Work/ })).toBeDefined()
      await health.unmount()
    }
    state.health.backend = 'ready'; state.health.failures = 0
    await $.session.measure({ context: {}, rateLimits: [], changed: ['rateLimits'] } as never)
    const health = await $.ui.mount({ plugin: 'cockpit', component: 'Pane', requestId: 'cockpit-pool-health', surface: 'terminal', props: { bodyColumns: 80 } } as never)
    expect(await health.find({ type: 'Text', text: /backend ready.*0 failed checks/ })).toBeDefined()
    await health.unmount()
  })

  test('native subscription mode reads only the designated local control token', async ($, on) => {
    mock.clock(on, { now: NOW }); mock.store(on)
    const tokenFile = '/fixture/.shunt/accounts/claude/.pool-client-token'
    mock.env(on, { HOME: '/fixture', COCKPIT_BASE_URL: 'http://127.0.0.1:18341', COCKPIT_TOKEN_FILE: tokenFile })
    on('session.id', () => ({ value: 'pool-session' }))
    const state = { ...initial(), connectorIdentity: 'gmail' }
    const reads: string[] = []
    on('fs.read', ($, e) => { reads.push(e.path); return { value: 'fixture-client\n' } })
    on('http.fetch', ($, e) => {
      expect(e.init?.headers?.Authorization).toBe('Bearer fixture-client')
      return { value: { status: 200, ok: true, headers: {}, text: JSON.stringify(state) } }
    })
    on('session.measure', ($, e) => ({ changed: e.changed }) as never)
    on('ui.render', ($, e) => { const { Box } = $.ui.resolve(e); return <Box /> })
    await $.session.measure({ context: {}, rateLimits: [], changed: ['rateLimits'] } as never)
    const pane = await $.ui.mount({ plugin: 'cockpit', component: 'Pane', requestId: 'cockpit-pool', surface: 'terminal', props: { bodyColumns: 100 } } as never)
    expect(await pane.find({ type: 'Text', text: /Subscription pool.*connectors Gmail/ })).toBeDefined()
    expect(reads).toEqual([tokenFile])
    await pane.unmount()
  })

  test('an arbitrary control credential path is rejected without reading or transmitting it', async ($, on) => {
    mock.clock(on, { now: NOW }); mock.store(on)
    mock.env(on, { HOME: '/fixture', COCKPIT_BASE_URL: 'http://127.0.0.1:18341', COCKPIT_TOKEN_FILE: '/unrelated/private-file' })
    on('session.id', () => ({ value: 'pool-session' }))
    on('fs.read', () => { throw new Error('Unexpected credential read') })
    on('http.fetch', () => { throw new Error('Unexpected credential transmission') })
    on('session.measure', ($, e) => ({ changed: e.changed }) as never)
    on('ui.render', ($, e) => { const { Box } = $.ui.resolve(e); return <Box /> })
    await $.session.measure({ context: {}, rateLimits: [], changed: ['rateLimits'] } as never)
    const pane = await $.ui.mount({ plugin: 'cockpit', component: 'Pane', requestId: 'cockpit-pool', surface: 'terminal', props: { bodyColumns: 100 } } as never)
    expect(await pane.find({ type: 'Text', text: /Pool unavailable/ })).toBeDefined()
    await pane.unmount()
  })

})
