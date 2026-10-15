import { describe, expect, mock, test } from 'claude-code/testing'
const SESSION = '11223344-1122-4334-8122-112233445566'
const PATH = '/tmp/pool-respawn-controller-fixture/result.json'
const START = { cwd: '/fixture', surface: 'terminal', isInteractive: false } as const

describe('targeted continuation controller', () => {
  for (const rejected of [0, 2, 20]) {
    test('retries only explicit non-delivery: ' + rejected, async ($, on) => {
      mock.env(on, { COCKPIT_RESPAWN_SESSION: SESSION, COCKPIT_RESPAWN_RESULT: PATH })
      on('clock.sleep', () => ({ value: undefined }))
      on('session.start', () => ({ cwd: '/fixture' }))
      let sends = 0, writes = 0, modelCalls = 0
      on('session.send', ($, e) => {
        expect(e.to).toBe(SESSION)
        expect(e.text.includes('Do not repeat')).toBe(true)
        return sends++ >= rejected ? { isDelivered: true } : { isDelivered: false, reason: 'fixture not ready' }
      })
      on('fs.write', ($, e) => {
        writes++; expect(e.path).toBe(PATH)
        expect(JSON.parse(e.text)).toEqual({ delivered: rejected < 20, session: SESSION })
        return { value: undefined }
      })
      on('turn.step', async function* ($, e) { modelCalls++; return { turnId: e.turnId, index: 0, answer: 'MODEL', toolUses: [], stopReason: 'end_turn', usage: null } })
      await $.session.start(START)
      for await (const chunk of $.turn.step({ turnId: 'qa', index: 0, model: 'fixture', messageCount: 0 })) void chunk
      expect(sends).toBe(Math.min(rejected + 1, 20)); expect(writes).toBe(1); expect(modelCalls).toBe(0)
    })
  }
  for (const env of [{}, { COCKPIT_RESPAWN_SESSION: 'wrong', COCKPIT_RESPAWN_RESULT: PATH }, { COCKPIT_RESPAWN_SESSION: SESSION, COCKPIT_RESPAWN_RESULT: '/tmp/other/result.json' }, { COCKPIT_RESPAWN_SESSION: SESSION, COCKPIT_RESPAWN_RESULT: '/tmp/../pool-respawn-controller-fixture/result.json' }] as Record<string, string>[]) {
    test('invalid controller identity or receipt path sends nothing ' + JSON.stringify(env), async ($, on) => {
      mock.env(on, env)
      on('session.start', () => ({ cwd: '/fixture' }))
      let sends = 0, writes = 0
      on('session.send', () => { sends++; return { isDelivered: true } })
      on('fs.write', () => { writes++; return { value: undefined } })
      await $.session.start(START)
      expect(sends).toBe(0); expect(writes).toBe(0)
    })
  }
  test('transport failure is never resent', async ($, on) => {
    mock.env(on, { COCKPIT_RESPAWN_SESSION: SESSION, COCKPIT_RESPAWN_RESULT: PATH })
    on('session.start', () => ({ cwd: '/fixture' }))
    let sends = 0
    on('session.send', () => { sends++; throw new Error('unknown outcome') })
    try { await $.session.start(START) } catch {}
    expect(sends).toBe(1)
  })
})
