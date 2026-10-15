import type { EngineInterface, Register } from 'claude-code'
import { controlURL, tokenFile, phoneURL, python } from './connection'

async function call($: EngineInterface, path: string, body: Record<string, unknown> = {}) {
  const token = (await $.fs.read(tokenFile)).trim()
  const r = await $.http.fetch(controlURL + '/internal/' + path, { method: 'POST', headers: { Authorization: 'Bearer ' + token, 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
  const value = JSON.parse(r.text)
  if (!r.ok) throw new Error(value.error || 'Phone controls are unavailable')
  return value
}

async function answer($: EngineInterface, text: string): Promise<string> {
  const state = await call($, 'overview')
  const match = /^send\s+([a-f0-9-]{8,36})\s+([\s\S]+)$/i.exec(text.trim())
  if (match) {
    const rows = state.tasks.filter((t: { task: string; status: string; legacy?: boolean }) => t.task.startsWith(match[1]!) && t.status !== 'offline' && !t.legacy)
    if (rows.length !== 1) return 'Choose one connected task by its unique ID from the task list. No instruction sent.'
    const id = await $.process.run([python, '-c', 'import uuid; print(uuid.uuid4())'])
    const result = await call($, 'message', { id: id.stdout.trim(), task: rows[0].task, generation: rows[0].generation, text: match[2] })
    return 'Instruction ' + result.status + ' for ' + rows[0].task.slice(0, 8) + '. Open that task in private Cockpit to follow its conversation.'
  }
  if (/^(approve|deny|allow)\b/i.test(text.trim())) return 'Approve or deny the exact request in private Cockpit. Chat messages never grant permission.'
  if (/^usage\b/i.test(text.trim())) {
    return state.pool ? state.pool.accounts.map((a: { label: string; status: string; fiveHour: { pct: number } | null; sevenDay: { pct: number } | null; fableWeekly?: { pct: number } | null }) =>
      a.label + ' · ' + a.status + ' · 5h ' + (a.fiveHour ? Math.round(100 - a.fiveHour.pct) + '% left' : 'unknown') + ' · week all ' + (a.sevenDay ? Math.max(0, Math.round(100 - a.sevenDay.pct)) + '% left' : 'not reported by Anthropic') + ' · Fable weekly ' + (a.fableWeekly ? Math.max(0, Math.round(100 - a.fableWeekly.pct)) + '% left' : 'reading unavailable')).join('\n') + '\nForecast uses all-model windows; Fable weekly is separate. Missing weekly readings can mean capacity runs out sooner.' : 'Pool readings are unavailable.'
  }
  const tasks = state.tasks.filter((t: { status: string }) => t.status !== 'offline').map((t: { task: string; project: string; title: string; status: string }) => t.task.slice(0, 8) + ' · ' + t.project + ' · ' + t.status + (t.title ? '\n  ' + t.title : '')).join('\n')
  return 'Claude pool · ' + state.pending.length + ' pending decision(s)\n\n' + (tasks || 'No connected tasks yet. Start claude on the Mac.') +
    '\n\nType tasks or usage. Send an instruction with: send <task-id> <instruction>\nDetails and approvals: ' + phoneURL
}

export const register: Register = on => {
  let input = '', human = false
  on('prompt.submit', async ($, e, next) => {
    human = ['composer', 'bridge', 'sdk'].includes(e.origin.kind)
    input = human ? e.text : ''
    return next(e)
  })
  on('turn.step', async function* ($, e) {
    let text = 'Overview ready. Type tasks or usage; approvals are available in private Cockpit.'
    if (human) {
      try { text = await answer($, input) } catch (error) { text = error instanceof Error ? error.message : 'Controls unavailable. Inspect the task before retrying.' }
      human = false
    }
    yield { kind: 'text', index: 0, text }
    return { turnId: e.turnId, index: e.index, answer: text, toolUses: [], stopReason: 'end_turn', usage: null }
  })
}
