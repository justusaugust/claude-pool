import { describe, expect, mock, test } from 'claude-code/testing'

const T0 = Date.parse('2026-10-02T12:00:00Z')
const MIN = 60_000

const account = (id: string, email: string) => JSON.stringify({ oauthAccount: { accountUuid: id, emailAddress: email } })
const GMAIL = account('a-gmail', 'someone@gmail.com')
const WORK = account('a-work', 'someone@example.invalid')

const limits = (five: number, fiveResets: number, week: number) => [
  { kind: 'five_hour', percentUsed: five, resetsAt: new Date(fiveResets).toISOString() },
  { kind: 'seven_day', percentUsed: week, resetsAt: new Date(T0 + 4 * 24 * 60 * MIN).toISOString() },
]

const BAND = {
  plugin: 'cockpit',
  component: 'AbovePrompt',
  props: { hasSurvey: false, isWorking: false, maxRows: 4, bodyColumns: 120 },
} as const

describe('cockpit quota band', () => {
  test('shows pace, both accounts and a switch hint near the limit', async ($, on) => {
    const clock = mock.clock(on, { now: T0 })
    mock.store(on)
    mock.env(on, { HOME: '/h' })
    let config = GMAIL
    on('fs.read', () => ({ value: config }) as never)
    on('session.measure', ($, e) => ({ changed: e.changed }) as never)
    on('ui.render', ($, e) => {
      const { Box } = $.ui.resolve(e)
      return <Box />
    })
    const measure = (five: number, week: number, resets: number) =>
      $.session.measure({ context: {}, rateLimits: limits(five, resets, week), changed: ['rateLimits'] } as never)

    // The gmail session reported earlier; now this session runs on the work account.
    await measure(20, 40, T0 + 180 * MIN)
    config = WORK
    await measure(70, 30, T0 + 120 * MIN)
    await clock.advance(20 * MIN)
    await measure(80, 31, T0 + 120 * MIN)
    await clock.advance(10 * MIN)
    await measure(88, 32, T0 + 120 * MIN)

    const ui = await $.ui.mount({ ...BAND, surface: 'terminal' } as never)
    // 70% -> 80% in 20 min, then -> 88% in 10: speeding up. The weighted fit
    // leans on the recent burn (0.62%/min rather than the flat 0.6), so the
    // 12% left lasts 19 min, well before the reset in 90.
    expect(await ui.find({ type: 'Text', text: /out in 19m/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /↻ 1h 30m/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /→ \/login gmail/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /20% used/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /someone@/ })).toBeUndefined()
    await ui.unmount()

    // The app gets tiles: one Svg each, described in its alt.
    const app = await $.ui.mount({ ...BAND, surface: 'desktop' } as never)
    const alts = (await app.findAll({ type: 'Svg' })).map(c => String(c.props.alt))
    expect(alts.some(a => /Session 88% used, resets 1h 30m; out in 19m/.test(a))).toBe(true)
    expect(alts.some(a => /\/login gmail, 20% used there/.test(a))).toBe(true)
    expect(alts.some(a => /someone@/.test(a))).toBe(false)
    await app.unmount()
  })

  test('a quiet line while healthy', async ($, on) => {
    mock.clock(on, { now: T0 })
    mock.store(on)
    mock.env(on, { HOME: '/h' })
    on('fs.read', () => ({ value: WORK }) as never)
    on('session.measure', ($, e) => ({ changed: e.changed }) as never)
    on('ui.render', ($, e) => {
      const { Box } = $.ui.resolve(e)
      return <Box />
    })
    await $.session.measure({ context: {}, rateLimits: limits(30, T0 + 60 * MIN, 10), changed: ['rateLimits'] } as never)

    const ui = await $.ui.mount({ ...BAND, surface: 'terminal' } as never)
    expect(await ui.find({ type: 'Text', text: /30%/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /\/login/ })).toBeUndefined()
    // When the window resets and the clock time the next one starts.
    expect(await ui.find({ type: 'Text', text: /(resets in|↻) 1h · \d\d:\d\d/ })).toBeDefined()
    await ui.unmount()

    // It steps with the width: words and the reset spelled out when wide,
    // short labels and no bars when narrow.
    const wide = await $.ui.mount({ ...BAND, surface: 'terminal', props: { ...BAND.props, bodyColumns: 180 } } as never)
    expect(await wide.find({ type: 'Text', text: /resets in 1h · \d\d:\d\d/ })).toBeDefined()
    expect(await wide.find({ type: 'Text', text: /━|─/ })).toBeDefined()
    await wide.unmount()
    const narrow = await $.ui.mount({ ...BAND, surface: 'terminal', props: { ...BAND.props, bodyColumns: 44 } } as never)
    expect(await narrow.find({ type: 'Text', text: /^5h $/ })).toBeDefined()
    expect(await narrow.find({ type: 'Text', text: /Session/ })).toBeUndefined()
    expect(await narrow.find({ type: 'Text', text: /━|─/ })).toBeUndefined()
    await narrow.unmount()

    // The app says the same in its quiet line.
    const app = await $.ui.mount({ ...BAND, surface: 'desktop' } as never)
    const sources = (await app.findAll({ type: 'Svg' })).map(c => String(c.props.source))
    expect(sources.some(src => /resets in 1h<\/tspan><tspan class="faint"> · \d\d:\d\d/.test(src))).toBe(true)
    await app.unmount()
  })

  test('keeps the bands of mods beneath it (image-view thumbnails)', async ($, on) => {
    mock.clock(on, { now: T0 })
    mock.store(on)
    mock.env(on, { HOME: '/h' })
    on('fs.read', () => ({ value: WORK }) as never)
    on('session.measure', ($, e) => ({ changed: e.changed }) as never)
    on('ui.render', ($, e) => {
      const { Text } = $.ui.resolve(e)
      return <Text>beneath-band</Text>
    })
    await $.session.measure({ context: {}, rateLimits: limits(30, T0 + 60 * MIN, 10), changed: ['rateLimits'] } as never)

    for (const surface of ['terminal', 'desktop'] as const) {
      const ui = await $.ui.mount({ ...BAND, surface } as never)
      expect(await ui.find({ type: 'Text', text: /beneath-band/ })).toBeDefined()
      await ui.unmount()
    }
  })

  test('auto-wrap switch: toggles ~/.claude/NOWRAP and shows the guard wrapping up', async ($, on) => {
    mock.clock(on, { now: T0 })
    mock.store(on)
    mock.env(on, { HOME: '/h' })
    // Every file and process call stays in memory: nothing touches the real ~/.claude.
    const files = new Set<string>()
    const ran: string[][] = []
    on('fs.read', () => ({ value: WORK }) as never)
    on('fs.exists', ($, e) => ({ value: files.has(e.path) }) as never)
    on('fs.write', ($, e) => {
      files.add(e.path)
      return { value: undefined } as never
    })
    on('process.run', ($, e) => {
      ran.push([...e.argv])
      if (e.argv[0] === 'rm') files.delete(e.argv[2]!)
      return { value: { exitCode: 0, stdout: '', stderr: '' } } as never
    })
    on('session.measure', ($, e) => ({ changed: e.changed }) as never)
    on('ui.render', ($, e) => {
      const { Box } = $.ui.resolve(e)
      return <Box />
    })
    await $.session.measure({ context: {}, rateLimits: limits(30, T0 + 60 * MIN, 10), changed: ['rateLimits'] } as never)
    const NOWRAP = '/h/.claude/NOWRAP'
    const label = async (ui: { find: (q: object) => Promise<{ props: Record<string, unknown> } | undefined> }) =>
      (await ui.find({ type: 'Button', key: 'cockpit-wrap' }))?.props.label

    for (const surface of ['terminal', 'desktop'] as const) {
      const ui = await $.ui.mount({ ...BAND, surface } as never)
      expect(String(await label(ui as never))).toMatch(/^(auto-)?wrap on$/)
      await ui.press({ key: 'cockpit-wrap' })
      expect(files.has(NOWRAP)).toBe(true)
      expect(String(await label(ui as never))).toMatch(/^(auto-)?wrap off$/)
      await ui.press({ key: 'cockpit-wrap' })
      expect(files.has(NOWRAP)).toBe(false)
      expect(ran.some(a => a[0] === 'rm' && a[2] === NOWRAP)).toBe(true)
      await ui.unmount()
    }

    // /wrap does the same.
    await $.command.run({ command: 'wrap', args: '' } as never)
    expect(files.has(NOWRAP)).toBe(true)

    // While a guard's WRAPUP file exists, the band says so.
    files.add('/h/.claude/WRAPUP')
    await $.command.run({ command: 'wrap', args: '' } as never)
    const term = await $.ui.mount({ ...BAND, surface: 'terminal' } as never)
    expect(await term.find({ type: 'Text', text: /wrapping up/ })).toBeDefined()
    await term.unmount()
    const app = await $.ui.mount({ ...BAND, surface: 'desktop' } as never)
    const alts = (await app.findAll({ type: 'Svg' })).map(c => String(c.props.alt))
    expect(alts.some(a => /Agents: wrapping up/.test(a))).toBe(true)
    await app.unmount()
  })

  test('the forecast: eases off through a break, ignores an old window, rounds what is far', async ($, on) => {
    const clock = mock.clock(on, { now: T0 })
    mock.store(on)
    mock.env(on, { HOME: '/h' })
    on('fs.read', () => ({ value: WORK }) as never)
    on('session.measure', ($, e) => ({ changed: e.changed }) as never)
    on('ui.render', ($, e) => {
      const { Box } = $.ui.resolve(e)
      return <Box />
    })
    const measure = (five: number, resets: number) =>
      $.session.measure({ context: {}, rateLimits: limits(five, resets, 20), changed: ['rateLimits'] } as never)
    const band = async () => {
      const ui = await $.ui.mount({ ...BAND, surface: 'terminal', props: { ...BAND.props, bodyColumns: 200 } } as never)
      const out = await ui.find({ type: 'Text', text: /out in/ })
      await ui.unmount()
      return out?.text
    }

    // A previous window that ended at 60%: its readings must not bend this one.
    const oldReset = T0 + 5 * MIN
    await measure(40, oldReset)
    await clock.advance(4 * MIN)
    await measure(60, oldReset)
    await clock.advance(2 * MIN)

    // This window: 10% -> 46% in 60 min, 0.6%/min, with a reset 4h out.
    const reset = T0 + 6 * MIN + 240 * MIN
    for (let i = 0; i <= 6; i++) {
      await measure(10 + i * 6, reset)
      if (i < 6) await clock.advance(10 * MIN)
    }
    // 54% left at 0.6%/min is 90 min: far, so rounded to five minutes and marked.
    expect(await band()).toMatch(/out in ~1h 30m/)

    // Then a break: 40 min with no readings. The burn eases off and the
    // window no longer runs out before its reset.
    // (A reading without the 5h window only refreshes the band, as its 30 s timer does.)
    await clock.advance(40 * MIN)
    await $.session.measure({ context: {}, rateLimits: [], changed: ['rateLimits'] } as never)
    expect(await band()).toBeUndefined()
  })
})
