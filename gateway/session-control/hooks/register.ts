import type { EngineInterface, Register } from 'claude-code'

import { controlURL, tokenFile as TOKEN, hookPath as HOOK, phoneURL, python, configFile } from './connection'
const BASE = controlURL + '/internal/'

type State = { token: string; task: string; session: string; generation: string; ticking: boolean; queue: { kind: string; text: string; id: string }[] }

  async function request($: EngineInterface, s: State, path: string, body: Record<string, unknown>) {
    const result = await $.http.fetch(BASE + path, { method: 'POST', headers: { Authorization: 'Bearer ' + s.token, 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
    if (!result.ok) throw new Error('Phone controls unavailable')
    return JSON.parse(result.text)
  }
  async function connect($: EngineInterface, s: State, cwd: string) {
    await $.env.set('CLAUDE_POOL_CONFIG', configFile)
    const current = await $.session.id()
    if (s.generation && s.session === current) return
    s.token = (await $.fs.read(TOKEN)).trim()
    s.task = await $.env.get('COCKPIT_CONTROL_TASK') || await $.env.get('COCKPIT_CONTROL_SOURCE') || current
    const child = await $.process.run([python, HOOK, 'register'], {
      stdin: JSON.stringify({ task: s.task, session: current, project: cwd.split('/').filter(Boolean).pop() || 'Claude' }), timeoutMs: 5000,
    })
    const value = JSON.parse(child.stdout)
    if (child.exitCode !== 0 || !value.generation || value.session !== current) throw new Error('Phone task registration failed')
    s.session = current; s.generation = value.generation; s.task = value.task
    await $.env.set('COCKPIT_CONTROL_TASK', s.task)
    await $.env.set('COCKPIT_CONTROL_GENERATION', s.generation)
    await $.env.set('COCKPIT_CONTROL_SESSION', s.session)
  }
  async function tick($: EngineInterface, s: State) {
    if (s.ticking || !s.generation) return
    s.ticking = true
    try {
      for (const item of s.queue.slice(0, 8)) {
        await request($, s, 'worker', { task: s.task, generation: s.generation, operation: 'event', ...item })
        s.queue.shift()
      }
      const result = await request($, s, 'worker', { task: s.task, generation: s.generation, operation: 'poll' })
      if (result.command) {
        let accepted = false
        try {
          const sent = await $.prompt.submit({ text: 'Instruction from your private phone Cockpit:\n' + result.command.text })
          accepted = sent.drop === undefined
        } finally {
          await request($, s, 'worker', { task: s.task, generation: s.generation, operation: 'receipt', id: result.command.id, accepted })
        }
      }
    } catch { /* A control outage must never interrupt Native work or replay an instruction. */ }
    finally { s.ticking = false }
  }

export const register: Register = on => {
  const s: State = { token: '', task: '', session: '', generation: '', ticking: false, queue: [] }
  on('session.start', async ($, e, next) => {
    const result = await next(e)
    try { await connect($, s, e.cwd) } catch { /* The ordinary task stays available. */ }
    $.clock.every(2500, () => void tick($, s))
    await $.command.register({ name: 'phone', description: 'Open this task in the private phone Cockpit', immediate: true })
    return result
  })
  on('command.run', { command: 'phone' }, async ($) => {
    if (!s.task || !s.generation) return { text: 'Phone controls are unavailable. Run claude phone in a terminal.' }
    return { text: 'Private Cockpit · ' + phoneURL + '#task=' + s.task }
  })
  on('session.append', async ($, e, next) => {
    const result = await next(e)
    if (e.agentId || !['user', 'assistant'].includes(e.message.type)) return result
    const parts: string[] = []
    for (const block of e.message.content) {
      if (block.type === 'text' && typeof block.text === 'string') parts.push(block.text)
      else if (block.type === 'tool_use' && typeof block.name === 'string') parts.push('Tool · ' + block.name)
      else if (block.type === 'tool_result') parts.push('Tool result · ' + (block.is_error ? 'failed' : 'returned'))
    }
    const text = parts.join('\n').slice(0, 4096)
    if (text) s.queue = [...s.queue, { kind: e.message.type, id: e.uuid, text }].slice(-48)
    return result
  })
  on('turn.start', async ($, e, next) => {
    try {
      await connect($, s, await $.env.get('PWD') || '')
      await request($, s, 'worker', { task: s.task, generation: s.generation, operation: 'status', status: 'working' })
    } catch { /* Observation is optional. */ }
    return next(e)
  })
  on('turn.complete', async ($, e, next) => {
    const result = await next(e)
    try { await request($, s, 'worker', { task: s.task, generation: s.generation, operation: 'status', status: 'idle' }) } catch { /* Observation is optional. */ }
    return result
  })
  on('session.end', async ($, e, next) => {
    try { await request($, s, 'worker', { task: s.task, generation: s.generation, operation: 'status', status: 'offline' }) } catch { /* Observation is optional. */ }
    s.generation = ''
    return next(e)
  })
}
