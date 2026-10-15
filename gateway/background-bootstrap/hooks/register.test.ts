import { describe, expect, mock, test } from 'claude-code/testing'
import { account, origin } from './account'
import { waitForQuota } from './register'

const SESSION = '11223344-1122-4334-8122-112233445566'
const START = { cwd: '/fixture/project', surface: 'terminal', isInteractive: false } as const

describe('background pool bootstrap', () => {
  test('uses the actual worker ID, clears competing auth, and preserves a later manual unpin', async ($, on) => {
    const env: Record<string, string | undefined> = { ANTHROPIC_API_KEY: 'fixture', CLAUDE_CODE_USE_VERTEX: '1' }
    on('env.get', ($, e) => ({ value: env[e.name] }))
    on('env.set', ($, e) => { env[e.name] = e.value; return { value: undefined } })
    mock.store(on)
    on('session.id', () => ({ value: SESSION }))
    on('fs.read', () => ({ value: 'x'.repeat(48) }))
    on('session.start', ($, e) => ({ cwd: e.cwd }))
    const requests: string[] = []
    on('http.fetch', ($, e) => {
      if (e.url.includes('/pool/capacity?')) return { value: { status: 404, ok: false, headers: {}, text: '{}' } }
      expect(e.url).toBe('http://127.0.0.1:18341/cockpit/' + SESSION)
      requests.push(e.init?.method ?? 'GET')
      const state = e.init?.method === 'POST' ? { mode: 'pinned', pin: account, accounts: [] } : { mode: 'auto', pin: null, accounts: [] }
      if (e.init?.method === 'POST') expect(JSON.parse(e.init.body as string)).toEqual({ action: 'pin', account })
      return { value: { status: 200, ok: true, headers: {}, text: JSON.stringify(state) } }
    })
    let calls = 0
    on('turn.step', async function* ($, e) {
      calls++
      return { turnId: e.turnId, index: e.index, answer: 'MODEL', toolUses: [], stopReason: 'end_turn', usage: null }
    })
    await $.session.start(START)
    for await (const chunk of $.turn.step({ turnId: 'qa', index: 0, model: 'fixture', messageCount: 0 })) void chunk
    expect(calls).toBe(1)
    expect(env.ANTHROPIC_BASE_URL).toBe('http://127.0.0.1:18341')
    expect(env.COCKPIT_BASE_URL).toBe('http://127.0.0.1:18341')
    expect(env.COCKPIT_PROJECT).toBe('project')
    expect(env.ANTHROPIC_API_KEY).toBeUndefined()
    expect(env.CLAUDE_CODE_USE_VERTEX).toBeUndefined()
    await $.session.start(START)
    expect(requests).toEqual([account === null ? 'GET' : 'POST', 'GET'])
  })
  for (const failure of ['offline', 'unauthorized', 'malformed', 'wrong-pin', 'missing-token', 'short-token', 'store-failed', ...(origin ? ['source-store-failed'] : [])]) {
    test('blocks inference on ' + failure + ' and permits recovery', async ($, on) => {
      on('env.set', () => ({ value: undefined }))
      let broken = true, calls = 0
      if (failure === 'store-failed' || failure === 'source-store-failed') {
        on('store.get', () => ({ value: undefined }))
        on('store.set', ($, e) => { if (broken && (failure === 'store-failed' || e.key.startsWith('origin:'))) throw new Error('Withheld'); return { value: undefined } })
      } else mock.store(on)
      on('session.id', () => ({ value: SESSION }))
      on('fs.read', () => {
        if (broken && failure === 'missing-token') throw new Error('Withheld')
        return { value: broken && failure === 'short-token' ? 'short' : 'x'.repeat(48) }
      })
      on('session.start', ($, e) => ({ cwd: e.cwd }))
      on('http.fetch', ($, e) => {
        if (e.url.includes('/pool/capacity?')) return { value: { status: 404, ok: false, headers: {}, text: '{}' } }
        if (broken && failure === 'offline') throw new Error('Withheld')
        const valid = { mode: account === null ? 'auto' : 'pinned', pin: account, accounts: [] }
        return { value: { status: 200, ok: !(broken && failure === 'unauthorized'), headers: {}, text:
          broken && failure === 'malformed' ? '[' : JSON.stringify(broken && failure === 'wrong-pin' ?
            { ...valid, mode: account === null ? 'unknown' : 'pinned', pin: 'unexpected' } : valid) } }
      })
      on('turn.step', async function* ($, e) {
        calls++
        return { turnId: e.turnId, index: e.index, answer: 'MODEL', toolUses: [], stopReason: 'end_turn', usage: null }
      })
      await $.session.start({ ...START, isInteractive: origin !== null })
      const chunks: unknown[] = []
      for await (const chunk of $.turn.step({ turnId: 'qa', index: 0, model: 'fixture', messageCount: 0 })) chunks.push(chunk)
      expect(calls).toBe(0)
      expect(chunks).toEqual([{ kind: 'text', index: 0, text: 'Pool startup could not be confirmed. Run claude doctor, then retry this session.' }])
      broken = false
      for await (const chunk of $.turn.step({ turnId: 'qa', index: 1, model: 'fixture', messageCount: 0 })) void chunk
      expect(calls).toBe(1)
    })
  }
  for (const scenario of ['reset', 'no-reset-time', 'offline', 'malformed']) test('quota wait: ' + scenario, async ($, on) => {
    on('env.set', () => ({ value: undefined })); mock.store(on)
    on('session.id', () => ({ value: SESSION }))
    on('fs.read', () => ({ value: 'x'.repeat(48) }))
    on('session.start', ($, e) => ({ cwd: e.cwd }))
    let checks = 0, calls = 0
    const statuses: (string | undefined)[] = []
    on('ui.status', ($, e) => { statuses.push(e.text); return { value: undefined } })
    on('http.fetch', ($, e) => {
      if (!e.url.includes('/pool/capacity?')) return { value: { ok: true, status: 200, headers: {}, text: JSON.stringify({ mode: account ? 'pinned' : 'auto', pin: account, accounts: [] }) } }
      checks++
      if (checks === 2) {
        expect(calls).toBe(0)
        expect(e.url).toContain('&wait=1')
        if (scenario === 'offline') throw new Error('offline')
      }
      return { value: { ok: true, status: 200, headers: {}, text: scenario === 'malformed' && checks === 2 ? '{}' : JSON.stringify({ state: checks === 1 ? 'quota_wait' : 'ready', nextResetAt: scenario === 'no-reset-time' ? null : 1900000000000 }) } }
    })
    on('turn.step', async function* ($, e) {
      calls++
      expect(e.model).toBe('claude-opus-5-5'); expect(e.effort).toBe('high')
      return { turnId: e.turnId, index: e.index, answer: 'MODEL', toolUses: [], stopReason: 'end_turn', usage: null }
    })
    await $.session.start(START)
    const run = async () => { for await (const chunk of $.turn.step({ turnId: 'qa', index: 0, model: 'claude-opus-5-5', effort: 'high', messageCount: 0 })) void chunk }
    if (scenario === 'offline' || scenario === 'malformed') { try { await run() } catch { /* expected */ }; expect(calls).toBe(0) }
    else { await run(); expect(calls).toBe(1) }
    expect(checks).toBe(2)
    expect(statuses[0]).toContain('automatically continues')
    expect(statuses[statuses.length - 1]).toBeUndefined()
  })
  test('cancelling the quota wait cannot start another request', async () => {
    const controller = new AbortController()
    const status: (string | undefined)[] = []
    let fetched = false
    const api: Parameters<typeof waitForQuota>[0] = {
      fs: { read: async () => 'x'.repeat(48) },
      ui: { status: async text => { status.push(text) } },
      http: { fetch: async () => {
        fetched = true
        controller.abort()
        return { ok: true, status: 200, headers: {}, text: JSON.stringify({ state: 'ready', nextResetAt: null }) }
      } },
    }
    let cancelled = false
    try { await waitForQuota(api, 'claude-opus-5-5', controller.signal, { state: 'quota_wait', nextResetAt: null }) } catch { cancelled = true }
    expect(fetched).toBe(true)
    expect(cancelled).toBe(true)
    expect(status[0]).toContain('automatically continues')
    expect(status[status.length - 1]).toBeUndefined()
  })
  test('future suites get local worker caps without a global gate', async ($, on) => {
    on('env.get', ($, e) => ({ value: e.name === 'SHELL' ? '/bin/zsh' : undefined }))
    on('process.run', ($, e) => ({ value: { exitCode: 0, stdout: JSON.stringify({ command: 'export CLAUDE_POOL_SUITE_LIMITS=2 CARGO_BUILD_JOBS=2\n' + e.init?.stdin }), stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }))
    const commands: string[] = []
    on('tool.call', { tool: 'Bash' }, ($, e) => {
      commands.push(e.command)
      expect(e.timeout).toBe(600000)
      expect(e.run_in_background).toBe(true)
      expect(e.dangerouslyDisableSandbox).toBeUndefined()
      return { result: 'fixture' }
    })
    for (const command of ['npm run check', 'cargo +1.96.1 test --lib', 'cd fixture && npm run test:integration', 'git status']) {
      await $.tool.call({ tool: 'Bash', command, timeout: 600000, run_in_background: true })
    }
    for (const command of commands.slice(0, 3)) { expect(command).toContain('CARGO_BUILD_JOBS=2'); expect(command).not.toContain('--gate'); expect(command).not.toContain('CLAUDE_POOL_HEAVY_QUEUE') }
    expect(commands[3]).toBe('git status')
  })
  if (origin !== null) for (const selection of ['backup', null]) test('handoff copies current routing '+selection+'; respawn keeps a later unpin', async ($, on) => {
    on('env.set', () => ({ value: undefined })); mock.store(on)
    on('fs.read', () => ({ value: 'x'.repeat(48) }))
    let session = SESSION, sourcePin = account
    const worker = '22334455-1122-4334-8122-112233445566'
    const states: Record<string, { mode: string; pin: string | null; accounts: unknown[] }> = {}
    on('session.id', () => ({ value: session }))
    on('session.start', ($, e) => ({ cwd: e.cwd }))
    on('http.fetch', ($, e) => {
      if (e.url.includes('/pool/capacity?')) return { value: { status: 404, ok: false, headers: {}, text: '{}' } }
      const id = e.url.split('/').pop()!
      if (e.init?.method === 'POST') states[id] = { mode: 'pinned', pin: JSON.parse(e.init.body!).account, accounts: [] }
      states[id] ??= { mode: 'auto', pin: null, accounts: [] }
      const state = id === SESSION ? { mode: sourcePin ? 'pinned' : 'auto', pin: sourcePin, accounts: [] } : states[id] ?? { mode: 'auto', pin: null, accounts: [] }
      return { value: { ok: true, status: 200, headers: {}, text: JSON.stringify(state) } }
    })
    await $.session.start({ ...START, isInteractive: true })
    sourcePin = selection
    session = worker
    await $.session.start(START)
    expect(states[worker]?.pin).toBe(selection)
    states[worker] = { mode: 'auto', pin: null, accounts: [] }
    await $.session.start(START)
    expect(states[worker]?.mode).toBe('auto')
    expect(states[worker]?.pin).toBeNull()
  })
})
