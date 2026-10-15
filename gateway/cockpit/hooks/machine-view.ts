import { Canvas, hex } from './canvas'
import type { Machine, SessionLoad } from '../types'

// The /machine pane, drawn cell by cell on the terminal's own background
// color, painted explicitly: inside a pane the terminal default resolves to
// Claude Code's grey pane fill. Ghostty's background-opacity-cells makes
// these painted cells as translucent as the transcript.
const BACKGROUND = hex('#0A1325')
const P = {
  claude: hex('#D77757'),
  text: hex('#E9E6F0'),
  soft: hex('#9AA3BF'),
  faint: hex('#4B5272'),
  track: hex('#343B57'),
  calm: hex('#7AA2F7'),
  session: hex('#E8956F'),
  working: hex('#4EBA65'),
  warning: hex('#FFC107'),
  error: hex('#FF6B80'),
}

const CHART_ROWS = 5

// `height` pads the drawing to the pane's full height, so no grey shows below.
export function drawMachine(m: Machine, columns: number, height = 0, now = m.at) {
  const groups = m.groups.filter(g => g.cpu >= 0.5).slice(0, 8)
  const sessions = m.sessions.slice(0, 6)
  const rows = 2 + CHART_ROWS + 1 + 2 + 2 + sessions.length * 3 + 1 + 2 + groups.length + 2 + 3 + 2
  const c = new Canvas(columns, rows, BACKGROUND)

  const left = 1
  // spare columns on the right: the two overfilled ones and the close mark's
  const right = columns - 6
  const width = right - left + 1
  let y = 0

  // Header: name on the left, load against cores on the right.
  c.text(left, y, '◆', P.claude)
  c.text(left + 2, y, 'machine', P.text)
  const ratio = m.load[0] / m.cores
  const loadColor = ratio > 1 ? P.error : ratio > 0.7 ? P.warning : P.text
  const tail = ` / ${m.cores} cores`
  c.textRight(right, y, tail, P.soft)
  c.textRight(right - tail.length, y, `load ${fmt(m.load[0])}`, loadColor)
  y += 2

  // Load over the last 10 minutes, with the core count as a dotted line.
  const values = m.trend.map(([, v]) => v)
  const top = Math.max(m.cores * 1.25, ...values, 1)
  c.area(left, y, width, CHART_ROWS, values, top, v => (v > m.cores ? P.error : v > m.cores * 0.75 ? P.warning : P.calm))
  const capacityRow = y + CHART_ROWS - 1 - Math.floor((m.cores / top) * CHART_ROWS)
  if (capacityRow >= y) c.guide(left, capacityRow, width, P.faint)
  y += CHART_ROWS
  c.text(left, y, values.length < 3 ? 'collecting…' : '10 min', P.faint)
  c.textRight(right, y, `avg 5m ${fmt(m.load[1])} · 15m ${fmt(m.load[2])} · now`, P.faint)
  y += 2

  // Column layout shared by the lists: name · count · bar · percent.
  const pctWidth = 4
  const countWidth = 4
  const barWidth = Math.max(6, Math.min(22, Math.floor(width * 0.4)))
  const nameWidth = width - countWidth - barWidth - pctWidth - 3
  const barX = left + nameWidth + countWidth + 2
  const level = (pct: number, calm: number) => (pct >= 50 ? P.error : pct >= 25 ? P.warning : calm)

  // Bars share one scale per list: the busiest row, at least 20%, so a quiet
  // machine doesn't draw slivers and a few percent doesn't look like a lot.
  let scale = 100

  const section = (label: string, hint: string) => {
    const end = c.text(left, y, label, P.soft)
    const hintStart = hint ? right - [...hint].length : right + 1
    for (let i = end + 1; i < hintStart; i++) c.put(i, y, '─', P.track)
    if (hint) c.text(hintStart + 1, y, hint, P.faint)
    y += 1
  }

  const row = (name: string, nameColor: number, count: string, pct: number, calm: number) => {
    c.text(left, y, clip(name, nameWidth), nameColor)
    c.textRight(left + nameWidth + countWidth, y, count, P.faint)
    c.bar(barX, y, barWidth, (pct / scale) * 100, level(pct, calm), P.track)
    c.textRight(right, y, `${Math.round(pct)}%`, pct >= 25 ? level(pct, P.text) : P.text)
    y += 1
  }

  // Sessions: who's working, who needs you, who's done, with what they're
  // doing and the last thing you asked.
  section('sessions', 'state · cpu')
  if (sessions.length === 0) {
    c.text(left, y, 'no Claude sessions running', P.faint)
    y += 1
  }
  for (const s of sessions) y = drawSession(c, s, left, right, y, now)
  y += 1

  scale = Math.max(20, ...groups.map(g => g.cpu))
  section('apps', 'procs · cpu')
  for (const g of groups) row(g.name, P.text, g.count > 1 ? `${g.count}` : '', g.cpu, P.calm)
  y += 1

  section('memory', '')
  const memPct = m.memTotal > 0 ? (m.memUsed / m.memTotal) * 100 : 0
  c.bar(left, y, width, memPct, memPct >= 92 ? P.error : memPct >= 80 ? P.warning : P.calm, P.track)
  y += 1
  c.text(left, y, `${gb(m.memUsed)} / ${gb(m.memTotal)} GB`, P.text)
  if (m.swapUsed > 0) c.textRight(right, y, `swap ${gb(m.swapUsed)} GB`, P.soft)
  y += 2

  c.text(left, y, '/machine closes', P.faint)
  return c.resize(Math.max(y + 1, height))
}

const STATE: Record<SessionLoad['state'], { dot: string; word: string; color: number }> = {
  needs: { dot: '●', word: 'needs you', color: P.warning },
  working: { dot: '●', word: 'working', color: P.working },
  done: { dot: '●', word: 'done', color: P.faint },
  unknown: { dot: '●', word: '?', color: P.faint },
}

function drawSession(c: Canvas, s: SessionLoad, left: number, right: number, y: number, now: number) {
  const st = STATE[s.state]
  const cpu = `${Math.round(s.cpu)}%`
  // row 1: ● project · title          working 12m   6%
  // fixed columns on the right: state word (9), time (3), cpu (4)
  c.put(left, y, st.dot, st.color)
  // done reads quieter: dim dot, soft word
  const cpuX = right - 3
  const statusX = cpuX - 2 - 3 - 1 - 9
  const nameEnd = c.text(left + 2, y, clip(s.project, statusX - left - 4), P.session)
  if (s.title && statusX - nameEnd > 6) c.text(nameEnd, y, clip(` · ${s.title}`, statusX - nameEnd - 1), P.faint)
  c.text(statusX, y, st.word, s.state === 'done' ? P.soft : st.color)
  c.textRight(cpuX - 2, y, ago(now - s.since), P.soft)
  c.textRight(right, y, cpu, s.cpu >= 25 ? P.warning : P.text)
  // row 2: what it's doing, while it's doing something
  let row = y + 1
  if (s.state !== 'done' && s.activity) {
    c.text(left + 2, row, clip(s.activity, right - left - 1), s.state === 'needs' ? P.warning : P.soft)
    row += 1
  }
  // last row: the last thing you asked
  if (s.prompt) {
    c.text(left + 2, row, clip(`“${s.prompt.replace(/\s+/g, ' ')}”`, right - left - 1), P.faint)
    row += 1
  }
  return row
}

function ago(ms: number) {
  const m = Math.max(0, Math.round(ms / 60_000))
  if (m < 60) return `${m}m`
  const h = Math.floor(m / 60)
  return h < 24 ? `${h}h` : `${Math.floor(h / 24)}d`
}

function clip(text: string, width: number) {
  return [...text].length > width ? `${[...text].slice(0, width - 1).join('')}…` : text
}

function fmt(load: number) {
  return load >= 10 ? load.toFixed(0) : load.toFixed(1)
}

function gb(bytes: number) {
  return (bytes / 1024 ** 3).toFixed(1)
}
