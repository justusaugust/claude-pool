import { esc } from './theme'
import type { Chip } from './theme'

// The desktop's band: one quiet line of readings, each a small SVG drawn as an
// image. A grey label, the number, a hairline bar with a tick where the
// clock is, a faint note. Monochrome; a muted amber or a dusty red only when
// something needs attention. Light or dark follows the app's appearance.

export type Tone = 'fg' | 'warn' | 'crit' | 'ok'

export type Item = {
  label: string
  value: string
  tone?: Tone
  bar?: { pct: number; mark?: number }
  note?: string
  noteTone?: Tone | 'faint'
  // a second, faint note after the first ("· 14:30")
  extra?: string
  // a small dot before the value, as for the account in use
  dot?: boolean
}

const H = 24
const BASE = 16
const GAP = 7
const BAR = 44
const TAIL = 26 // room after an item, before the next

function css() {
  return (
    `<style>` +
    `:root{--fg:#3D3C38;--dim:#8A8984;--faint:#AEADA7;--track:rgba(28,27,25,.09);--fill:rgba(28,27,25,.34);` +
    `--warn:#A27B3F;--crit:#B0584F;--ok:#5E8F6A}` +
    `@media (prefers-color-scheme:dark){:root{--fg:#D6D5D0;--dim:#7E7D78;--faint:#5F5E5A;--track:rgba(242,241,236,.08);` +
    `--fill:rgba(242,241,236,.34);--warn:#CDA66A;--crit:#D9877F;--ok:#7FAE89}}` +
    `text{font-family:-apple-system,BlinkMacSystemFont,"SF Pro Text","Inter",system-ui,sans-serif;font-variant-numeric:tabular-nums}` +
    `.l{font-size:11.5px;fill:var(--dim)}.v{font-size:12.5px;font-weight:500}.n{font-size:11px}` +
    `.fg{fill:var(--fg)}.faint{fill:var(--faint)}.warn{fill:var(--warn)}.crit{fill:var(--crit)}.ok{fill:var(--ok)}` +
    `</style>`
  )
}

// Proportional text, so widths are estimates by kind of character (SF's
// tabular digits are wide, '%' wider, lowercase narrow), erring generous.
function w(text: string, size: number) {
  let em = 0
  for (const c of text) {
    if (/[0-9]/.test(c)) em += 0.62
    else if (c === '%') em += 0.86
    else if (c === ' ') em += 0.28
    else if (/[.,:·'|]/.test(c)) em += 0.3
    else if (/[A-Z$→]/.test(c)) em += 0.68
    else if (/[mw]/.test(c)) em += 0.84
    else if (/[iljft/]/.test(c)) em += 0.3
    else em += 0.55
  }
  return em * size
}

export function item(it: Item, alt: string): Chip {
  const tone = it.tone ?? 'fg'
  const parts: string[] = []
  let x = 0

  parts.push(`<text class="l" x="${x}" y="${BASE}">${esc(it.label)}</text>`)
  x += w(it.label, 11.5) + GAP

  if (it.dot) {
    parts.push(`<circle class="${it.tone === 'fg' || !it.tone ? 'ok' : tone}" cx="${x + 2.5}" cy="${BASE - 4}" r="2.5" fill-opacity=".8"/>`)
    x += 10
  }
  parts.push(`<text class="v ${tone}" x="${x}" y="${BASE}">${esc(it.value)}</text>`)
  x += w(it.value, 12.5) + GAP + 1

  if (it.bar) {
    const pct = Math.max(0, Math.min(100, it.bar.pct))
    const fill = (pct / 100) * BAR
    const color = tone === 'fg' ? 'var(--fill)' : `var(--${tone})`
    parts.push(`<rect x="${x.toFixed(1)}" y="${BASE - 5}" width="${BAR}" height="2" rx="1" style="fill:var(--track)"/>`)
    if (fill > 0)
      parts.push(`<rect x="${x.toFixed(1)}" y="${BASE - 5}" width="${Math.max(2, fill).toFixed(1)}" height="2" rx="1" style="fill:${color}"/>`)
    if (it.bar.mark !== undefined) {
      const mx = x + Math.max(0, Math.min(1, it.bar.mark)) * BAR
      parts.push(`<rect x="${(mx - 0.5).toFixed(1)}" y="${BASE - 7}" width="1" height="6" style="fill:var(--dim)"/>`)
    }
    x += BAR + GAP
  }

  if (it.note) {
    parts.push(
      `<text class="n" x="${x.toFixed(1)}" y="${BASE}"><tspan class="${it.noteTone ?? 'faint'}">${esc(it.note)}</tspan>` +
        (it.extra ? `<tspan class="faint"> · ${esc(it.extra)}</tspan>` : '') +
        `</text>`,
    )
    x += w(it.note, 11) + (it.extra ? w(` · ${it.extra}`, 11) : 0)
  }

  const width = Math.ceil(x + TAIL)
  const source =
    `<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="${H}" viewBox="0 0 ${width} ${H}">` +
    css() +
    parts.join('') +
    `</svg>`
  return { source, width, height: H, alt }
}
