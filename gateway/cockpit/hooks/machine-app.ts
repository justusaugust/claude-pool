import { clip, css, esc, heat, square, SCALE, width } from './theme'
import type { Chip, Ink } from './theme'
import type { Group, Machine, SessionLoad } from '../types'

// The /machine pane for the app: one SVG per section (load, sessions, apps,
// memory), stacked. A warm scale, deep orange to yellow with browns for the
// quiet end; status colors only where they mean something. Tight sans
// numbers, tiny uppercase meta, hairline lists, everything on one grid.

// The section width, set per draw from the pane's width (machineSections).
let W = 420
const X = 20 // left edge of every line
let R = W - 20 // right edge of every value
function svg(height: number, body: string, defs = '') {
  return `<svg xmlns="http://www.w3.org/2000/svg" width="${W}" height="${height}" viewBox="0 0 ${W} ${height}">${css()}${defs}${body}</svg>`
}

// Every section opens the same way: a hairline above (but the first), the
// title on the left, an uppercase note on the right, led by a dot.
function head(title: string, note: string | undefined, first: boolean, dot?: Ink) {
  const noteText = note?.toUpperCase()
  return (
    (first ? '' : `<rect class="line" x="${X}" y="0" width="${R - X}" height="1"/>`) +
    `<text class="ttl" x="${X}" y="30">${esc(title)}</text>` +
    (noteText
      ? `<text class="cap" x="${R}" y="30" text-anchor="end">` +
        (dot ? `<tspan class="${dot}" font-size="8" dy="-1">●</tspan><tspan dx="5" dy="1">` : '<tspan>') +
        `${esc(noteText)}</tspan></text>`
      : '')
  )
}

// One hairline-separated row: square, label, quiet detail · value on the right.
function listRow(y: number, ink: Ink, label: string, value: string, opts: { count?: string; valueInk?: Ink } = {}) {
  const base = y + 26
  return (
    `<rect class="line" x="${X}" y="${y}" width="${R - X}" height="1"/>` +
    square(X, base - 4.5, ink, 7) +
    `<text class="t" x="${X + 17}" y="${base}">${esc(clip(label, R - X - 90))}` +
    (opts.count ? `<tspan class="xs faint" dx="6">${opts.count}</tspan>` : '') +
    `</text>` +
    `<text class="t m${opts.valueInk ? ` ${opts.valueInk}` : ''}" x="${R}" y="${base}" text-anchor="end">${value}</text>`
  )
}

// Each section `width` px wide: load and memory, then sessions and apps, so a
// wide pane can set them in two columns of two.
export function machineSections(m: Machine, now: number, width = 420): { left: Chip[]; right: Chip[] } {
  W = Math.round(width)
  R = W - 20
  return {
    left: [loadSection(m, now), memorySection(m)],
    right: [sessionsSection(m.sessions.slice(0, 6), now), appsSection(m.groups)],
  }
}

const BARS = 60

function loadSection(m: Machine, now: number): Chip {
  const ratio = m.load[0] / m.cores
  const capacity = Math.round(ratio * 100)
  const state = ratio > 1 ? 'over capacity' : ratio > 0.75 ? 'busy' : 'live'
  const stateInk: Ink = ratio > 1 ? 'red' : ratio > 0.75 ? 'amber' : 'o1'

  // Ten minutes in sixty slots, each the busiest sample that fell in it.
  const top0 = 52
  const bottom = 156
  const span = 10 * 60_000
  const slots: (number | undefined)[] = Array.from({ length: BARS }, () => undefined)
  for (const [t, v] of m.trend) {
    const age = now - t
    if (age < 0 || age > span) continue
    const i = Math.min(BARS - 1, Math.floor((1 - age / span) * BARS))
    slots[i] = Math.max(slots[i] ?? 0, v)
  }
  const top = Math.max(m.cores * 1.2, ...slots.map(v => v ?? 0), 1)
  const py = (v: number) => bottom - (v / top) * (bottom - top0)
  const slot = (R - X) / BARS
  const bw = Math.max(1.5, slot * 0.42)
  const filled = slots.filter(v => v !== undefined).length

  let bars = ''
  slots.forEach((v, i) => {
    const x = X + i * slot + (slot - bw) / 2
    if (v === undefined) return
    const y = py(v)
    bars += `<rect fill="url(#heat)" x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${bw.toFixed(1)}" height="${(bottom - y).toFixed(1)}" rx="${(bw / 2).toFixed(2)}"/>`
  })
  const capY = py(m.cores)
  const capLabel = `all ${m.cores} cores busy`

  const statY = 208
  const H = 244
  const body =
    head('Load', state, true, stateInk) +
    bars +
    `<line x1="${X}" x2="${R}" y1="${capY.toFixed(1)}" y2="${capY.toFixed(1)}" stroke="var(--faint)" stroke-dasharray="1 4" stroke-linecap="round"/>` +
    `<rect class="line" x="${X}" y="${bottom}" width="${R - X}" height="1"/>` +
    `<line x1="${R - width(capLabel, 10.5) - 22}" x2="${R - width(capLabel, 10.5) - 8}" y1="${bottom + 13.5}" y2="${bottom + 13.5}" stroke="var(--faint)" stroke-dasharray="1 3" stroke-linecap="round"/>` +
    `<text class="xs faint" x="${R}" y="${bottom + 17}" text-anchor="end">${capLabel}</text>` +
    `<text class="xs faint" x="${X}" y="${bottom + 17}">10 min</text>` +
    (filled < 3 ? `<text class="s dim" x="${W / 2}" y="${(top0 + bottom) / 2}" text-anchor="middle">Collecting the last ten minutes…</text>` : '') +
    // Two stats under the chart, each led by its square.
    square(X, statY - 21, 'o1') +
    `<text class="xs dim" x="${X + 11}" y="${statY - 18}">Load now</text>` +
    `<text x="${X}" y="${statY + 14}"><tspan class="big">${fmt(m.load[0])}</tspan><tspan class="s dim" dx="6">/ ${m.cores} cores</tspan></text>` +
    `<text class="xs ${stateInk === 'o1' ? 'dim' : stateInk}" x="${X}" y="${statY + 32}">${capacity}% of capacity</text>` +
    square(W / 2 + 10, statY - 21, 'o4') +
    `<text class="xs dim" x="${W / 2 + 21}" y="${statY - 18}">Average</text>` +
    `<text x="${W / 2 + 10}" y="${statY + 14}"><tspan class="big">${fmt(m.load[1])}</tspan><tspan class="s dim" dx="6">5 min</tspan></text>` +
    `<text class="xs dim" x="${W / 2 + 10}" y="${statY + 32}">${fmt(m.load[2])} over 15 min</text>`
  const defs = `<defs>${heat('heat', 0, bottom, 0, top0)}</defs>`
  return {
    source: svg(H + 16, body, defs),
    width: W,
    height: H + 16,
    alt: `Load ${fmt(m.load[0])} on ${m.cores} cores, ${capacity}% of capacity`,
  }
}

const STATE: Record<SessionLoad['state'], { word: string; ink: Ink; rank: number }> = {
  needs: { word: 'Needs you', ink: 'amber', rank: 0 },
  working: { word: 'Working', ink: 'green', rank: 1 },
  unknown: { word: 'Idle', ink: 'gray', rank: 2 },
  done: { word: 'Done', ink: 'gray', rank: 3 },
}

const ROW = 58

function sessionsSection(all: SessionLoad[], now: number): Chip {
  const sessions = [...all].sort((a, b) => STATE[a.state].rank - STATE[b.state].rank || b.since - a.since)
  let body = head('Sessions', sessions.length ? `${sessions.length} open` : undefined, false)
  let y = 38
  if (sessions.length === 0) {
    body += `<text class="s dim" x="${X}" y="${y + 18}">No Claude sessions running</text>`
    y += 36
  }

  sessions.forEach((s, i) => {
    const st = STATE[s.state]
    const quiet = st.ink === 'gray'
    const l1 = y + 24
    const l2 = y + 43
    const tx = X + 16
    if (i > 0) body += `<rect class="line" x="${tx}" y="${y}" width="${R - tx}" height="1"/>`

    // Left edge: an amber rail for the one waiting on you, a square otherwise.
    body +=
      s.state === 'needs'
        ? `<rect class="amber" x="${X}" y="${y + 12}" width="3" height="${ROW - 22}" rx="1.5"/>`
        : square(X, l1 - 4.5, st.ink, 7)

    // Line 1: project and title · the state as a small tag.
    const tag = st.word.toUpperCase()
    const tagW = tag.length * 7 + 14
    const room = R - tx - tagW - 12
    const name = clip(s.project, room)
    const titleRoom = room - width(name) - 8
    body +=
      `<text class="t" x="${tx}" y="${l1}"><tspan class="m${quiet ? ' dim' : ''}">${esc(name)}</tspan>` +
      (s.title && titleRoom > 36 ? `<tspan class="faint" dx="7">${esc(clip(s.title, titleRoom))}</tspan>` : '') +
      `</text>` +
      `<rect class="${st.ink}" fill-opacity="${quiet ? '.12' : '.14'}" x="${(R - tagW).toFixed(1)}" y="${l1 - 12}" width="${tagW.toFixed(1)}" height="16" rx="4"/>` +
      `<text class="cap" x="${R - tagW / 2}" y="${l1 - 0.5}" text-anchor="middle" style="fill:var(--${quiet ? 'dim' : st.ink})">${tag}</text>`

    // Line 2: what it's doing, or what you last asked · time, and cpu as a tiny bar.
    const detail = !quiet && s.activity ? s.activity : s.prompt ? `“${s.prompt.replace(/\s+/g, ' ')}”` : ''
    const time = ago(now - s.since)
    const cpu = `${Math.round(s.cpu)}%`
    const meterW = 28
    const metaW = width(time, 10.5) + 10 + meterW + 6 + width(cpu, 10.5)
    if (detail)
      body += `<text class="s ${s.state === 'needs' && !detail.startsWith('“') ? 'amber' : 'dim'}" x="${tx}" y="${l2}">${esc(clip(detail, R - tx - metaW - 16, 11.5))}</text>`
    const meterX = R - width(cpu, 10.5) - 6 - meterW
    body +=
      `<text class="xs faint" x="${meterX - 10}" y="${l2}" text-anchor="end">${time}</text>` +
      `<rect class="empty" x="${meterX.toFixed(1)}" y="${l2 - 5}" width="${meterW}" height="3" rx="1.5"/>` +
      (s.cpu >= 1
        ? `<rect class="${s.cpu >= 50 ? 'red' : s.cpu >= 25 ? 'amber' : 'o2'}" x="${meterX.toFixed(1)}" y="${l2 - 5}" width="${Math.max(3, Math.min(1, s.cpu / 50) * meterW).toFixed(1)}" height="3" rx="1.5"/>`
        : '') +
      `<text class="xs ${s.cpu >= 1 ? 'dim' : 'faint'}" x="${R}" y="${l2}" text-anchor="end">${cpu}</text>`
    y += ROW
  })
  const H = y + 8
  const active = sessions.filter(s => s.state === 'working' || s.state === 'needs').length
  return { source: svg(H, body), width: W, height: H, alt: `${sessions.length} Claude sessions, ${active} active` }
}

function appsSection(all: Group[]): Chip {
  const groups = all.filter(g => g.cpu >= 0.5).slice(0, 5)
  const total = all.reduce((sum, g) => sum + g.cpu, 0)
  const shown = groups.reduce((sum, g) => sum + g.cpu, 0)
  const rest = Math.max(0, total - shown)
  let body = head('CPU by app', `${Math.round(total)}% of all cores`, false, total >= 75 ? 'red' : total >= 50 ? 'amber' : 'o1')
  let y = 48

  // The whole machine as one bar: each app its segment, the rest brown, idle empty.
  const len = R - X
  const gap = 3
  const segs = [...groups.map((g, i) => ({ v: g.cpu, ink: SCALE[i] as Ink })), { v: rest, ink: 'brown' as Ink }].filter(
    s => (s.v / 100) * len >= 3,
  )
  let x = X
  for (const s of segs) {
    const w = (s.v / 100) * len - gap
    body += `<rect class="${s.ink}" x="${x.toFixed(1)}" y="${y}" width="${Math.max(2, w).toFixed(1)}" height="8" rx="2"/>`
    x += w + gap
  }
  if (x < R) body += `<rect class="empty" x="${x.toFixed(1)}" y="${y}" width="${(R - x).toFixed(1)}" height="8" rx="2"/>`
  y += 22

  if (groups.length === 0) {
    body += `<text class="s dim" x="${X}" y="${y + 20}">Nothing busy right now</text>`
    y += 32
  }
  groups.forEach((g, i) => {
    const hot: Ink | undefined = g.cpu >= 50 ? 'red' : g.cpu >= 25 ? 'amber' : undefined
    body += listRow(y, SCALE[i]!, g.name, `${Math.round(g.cpu)}%`, { count: g.count > 1 ? `×${g.count}` : undefined, valueInk: hot })
    y += 40
  })
  const H = y + 6
  const busiest = groups[0]
  return {
    source: svg(H, body),
    width: W,
    height: H,
    alt: busiest ? `Busiest app ${busiest.name} at ${Math.round(busiest.cpu)}% CPU` : 'No app is busy',
  }
}

function memorySection(m: Machine): Chip {
  const pct = m.memTotal > 0 ? (m.memUsed / m.memTotal) * 100 : 0
  const totalGb = m.memTotal / 1024 ** 3
  const usedGb = m.memUsed / 1024 ** 3
  const swapGb = m.swapUsed / 1024 ** 3
  const free = Math.max(0, totalGb - usedGb)

  // One square per GB (per 2 or 4 on big machines), used ones in the warm
  // scale from hot to cool, swap in brown after them, free ones empty.
  const per = totalGb > 96 ? 4 : totalGb > 48 ? 2 : 1
  const cells = Math.max(1, Math.round(totalGb / per))
  const cols = cells <= 24 ? cells : cells <= 48 ? Math.ceil(cells / 2) : Math.ceil(cells / Math.ceil(cells / 24))
  const rows = Math.ceil(cells / cols)
  const gap = 4
  const size = Math.min(22, (R - X - gap * (cols - 1)) / cols)
  const used = Math.round(usedGb / per)
  const swap = Math.min(cells - used, Math.round(swapGb / per))
  const hot = pct >= 92 ? 'red' : pct >= 80 ? 'amber' : 'o1'

  let body = head('Memory', `${Math.round(pct)}% used`, false, hot as Ink)
  body += `<text x="${X}" y="76"><tspan class="big">${gb(m.memUsed)}</tspan><tspan class="s dim" dx="6">/ ${gb(m.memTotal)} GB</tspan></text>`
  const gridY = 94
  for (let i = 0; i < cells; i++) {
    const col = i % cols
    const row = Math.floor(i / cols)
    // Column-major fill, so the scale sweeps left to right across the grid.
    const order = col * rows + row
    const ink: Ink =
      order < used ? SCALE[Math.min(SCALE.length - 1, Math.floor((order / Math.max(1, used)) * SCALE.length))]! : order < used + swap ? 'brown' : 'empty'
    body += `<rect class="${ink}" x="${(X + col * (size + gap)).toFixed(1)}" y="${(gridY + row * (size + gap)).toFixed(1)}" width="${size.toFixed(1)}" height="${size.toFixed(1)}" rx="2.5"/>`
  }
  const footY = gridY + rows * (size + gap) + 18
  body +=
    `<text class="s m" x="${X}" y="${footY}"><tspan class="o1">${gb(m.memUsed)} used</tspan>` +
    `<tspan class="faint" dx="8">|</tspan><tspan class="o4" dx="8">${free.toFixed(1)} free</tspan>` +
    (m.swapUsed > 0 ? `<tspan class="faint" dx="8">|</tspan><tspan class="dim" dx="8">${gb(m.swapUsed)} swap</tspan>` : '') +
    `</text>` +
    `<text class="xs faint" x="${R}" y="${footY}" text-anchor="end">${per === 1 ? '1 square = 1 GB' : `1 square = ${per} GB`}</text>`
  const H = footY + 16
  return { source: svg(H, body), width: W, height: H, alt: `Memory ${gb(m.memUsed)} of ${gb(m.memTotal)} GB used` }
}

function ago(ms: number) {
  const m = Math.max(0, Math.round(ms / 60_000))
  if (m < 60) return `${m}m`
  const h = Math.floor(m / 60)
  return h < 24 ? `${h}h` : `${Math.floor(h / 24)}d`
}

function fmt(load: number) {
  return load >= 10 ? load.toFixed(0) : load.toFixed(1)
}

function gb(bytes: number) {
  return (bytes / 1024 ** 3).toFixed(1)
}
