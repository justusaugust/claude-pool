// The app's ink, shared by the band and the /machine pane: a warm scale from
// deep orange to yellow with browns for the quiet end, status colors only
// where they mean something, tight sans numbers and tiny uppercase notes.
// Light or dark follows the app through prefers-color-scheme.

export type Chip = { source: string; width: number; height: number; alt: string }

// The warm scale, hottest first.
export const SCALE = ['o1', 'o2', 'o3', 'o4', 'o5'] as const
export type Ink = (typeof SCALE)[number] | 'brown' | 'empty' | 'green' | 'amber' | 'red' | 'gray'
const INKS: Ink[] = ['o1', 'o2', 'o3', 'o4', 'o5', 'brown', 'empty', 'green', 'amber', 'red', 'gray']

export function esc(s: string) {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
}

export function css() {
  return (
    `<style>` +
    `:root{--fg:#1C1B19;--dim:#7B7A74;--faint:#A9A8A1;--line:rgba(28,27,25,.09);` +
    `--o1:#F0561D;--o2:#F57A2E;--o3:#F59B3A;--o4:#EDB443;--o5:#E3C35A;--brown:#D2B19A;--empty:rgba(28,27,25,.06);` +
    `--green:#2E8A4B;--amber:#C27A10;--red:#D03A4C;--gray:#A9A8A1}` +
    `@media (prefers-color-scheme:dark){:root{--fg:#F2F1EC;--dim:#8F8E88;--faint:#5F5E59;--line:rgba(242,241,236,.08);` +
    `--o1:#FF5A1F;--o2:#FF7D33;--o3:#FFA040;--o4:#FFC452;--o5:#FFDF7A;--brown:#6B3A1F;--empty:rgba(242,241,236,.06);` +
    `--green:#4FBF6C;--amber:#FFB43A;--red:#FF6B6B;--gray:#5F5E59}}` +
    `text{font-family:-apple-system,BlinkMacSystemFont,"SF Pro Text","Inter","Segoe UI",system-ui,sans-serif;font-variant-numeric:tabular-nums;fill:var(--fg)}` +
    `.ttl{font-size:14px;font-weight:500;letter-spacing:-.01em}` +
    `.cap{font-size:9.5px;font-weight:500;letter-spacing:.09em;fill:var(--dim)}` +
    `.big{font-size:30px;font-weight:500;letter-spacing:-.035em}` +
    `.mid{font-size:20px;font-weight:500;letter-spacing:-.03em}` +
    `.t{font-size:13px}.s{font-size:11.5px}.xs{font-size:10.5px}.b{font-weight:600}.m{font-weight:500}` +
    `.dim{fill:var(--dim)}.faint{fill:var(--faint)}.line{fill:var(--line)}` +
    INKS.map(k => `.${k}{fill:var(--${k})}`).join('') +
    `</style>`
  )
}

// The vertical heat gradient bars draw with: brown at the foot, yellow at the top.
export function heat(id: string, x1: number, y1: number, x2: number, y2: number) {
  return (
    `<linearGradient id="${id}" gradientUnits="userSpaceOnUse" x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}">` +
    `<stop offset="0" style="stop-color:var(--brown)"/>` +
    `<stop offset=".45" style="stop-color:var(--o1)"/>` +
    `<stop offset=".8" style="stop-color:var(--o3)"/>` +
    `<stop offset="1" style="stop-color:var(--o5)"/></linearGradient>`
  )
}

export function square(x: number, cy: number, ink: Ink, size = 6) {
  return `<rect class="${ink}" x="${x}" y="${(cy - size / 2).toFixed(1)}" width="${size}" height="${size}" rx="1.5"/>`
}

// Proportional text, so widths are estimates: generous ones, then an ellipsis.
export function width(text: string, size = 13) {
  return [...text].length * size * 0.55
}

export function clip(text: string, px: number, size = 13) {
  const max = Math.max(1, Math.floor(px / (size * 0.55)))
  const chars = [...text]
  return chars.length > max ? `${chars.slice(0, max - 1).join('')}…` : text
}
