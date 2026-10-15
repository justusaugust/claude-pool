// A cell buffer for the terminal's Raster element: every cell a glyph, a
// foreground and a background. DEFAULT is the terminal's own color, so
// cells left at it show the terminal's (translucent) background.
export const DEFAULT = 0x01000000

export const hex = (color: string) => parseInt(color.slice(1), 16)

const EIGHTHS = ['', '▏', '▎', '▍', '▌', '▋', '▊', '▉']
const RISE = ['', '▁', '▂', '▃', '▄', '▅', '▆', '▇']

export class Canvas {
  readonly cells: Uint32Array

  constructor(
    readonly columns: number,
    readonly rows: number,
    readonly background = DEFAULT,
  ) {
    this.cells = new Uint32Array(columns * rows * 3)
    for (let i = 0; i < columns * rows; i++) this.cells.set([0x20, DEFAULT, background], i * 3)
  }

  put(x: number, y: number, glyph: string, fg: number, bg = this.background) {
    if (x < 0 || y < 0 || x >= this.columns || y >= this.rows) return
    const code = glyph.codePointAt(0) ?? 0x20
    this.cells.set([code > 0xffff ? 0x3f : code, fg, bg], (y * this.columns + x) * 3)
  }

  text(x: number, y: number, text: string, fg: number) {
    ;[...text].forEach((ch, i) => this.put(x + i, y, ch, fg))
    return x + [...text].length
  }

  // Right-aligned so its last character sits at x (inclusive).
  textRight(x: number, y: number, text: string, fg: number) {
    return this.text(x - [...text].length + 1, y, text, fg)
  }

  // A horizontal bar with eighth-cell precision over a dotted track.
  bar(x: number, y: number, width: number, pct: number, fill: number, track: number) {
    const units = Math.round((Math.max(0, Math.min(100, pct)) / 100) * width * 8)
    for (let i = 0; i < width; i++) {
      const left = units - i * 8
      if (left >= 8) this.put(x + i, y, '█', fill)
      else if (left > 0) this.put(x + i, y, EIGHTHS[left] ?? '█', fill)
      else this.put(x + i, y, '·', track)
    }
  }

  // An area chart, `height` rows tall, eighth-row precision; values scaled
  // to `top`, newest on the right. `colorAt` colors each row by the value at
  // its height, so the fill reads as bands: calm low, hot high.
  area(x: number, y: number, width: number, height: number, values: number[], top: number, colorAt: (v: number) => number) {
    const shown = values.slice(-width)
    const offset = width - shown.length
    shown.forEach((v, i) => {
      const units = Math.round((Math.max(0, v) / top) * height * 8)
      for (let row = 0; row < height; row++) {
        const left = units - row * 8
        const glyph = left >= 8 ? '█' : left > 0 ? RISE[left] : ''
        if (glyph) this.put(x + offset + i, y + height - 1 - row, glyph, colorAt(((row + 0.5) / height) * top))
      }
    })
  }

  // The same drawing on `rows` rows: cut, or padded with background.
  resize(rows: number) {
    const out = new Canvas(this.columns, Math.max(1, rows), this.background)
    out.cells.set(this.cells.subarray(0, Math.min(this.cells.length, out.cells.length)))
    return out
  }

  // A dotted guide across empty cells of one row.
  guide(x: number, y: number, width: number, color: number) {
    for (let i = 0; i < width; i++) {
      const at = (y * this.columns + x + i) * 3
      if (this.cells[at] === 0x20) this.put(x + i, y, '┄', color)
    }
  }

  toBase64() {
    const bytes = new Uint8Array(this.cells.buffer)
    return (bytes as Uint8Array & { toBase64: () => string }).toBase64()
  }

  // For checking a drawing outside the engine: the glyphs, one line per row.
  toText() {
    const lines: string[] = []
    for (let y = 0; y < this.rows; y++) {
      let line = ''
      for (let x = 0; x < this.columns; x++) line += String.fromCodePoint(this.cells[(y * this.columns + x) * 3] ?? 0x20)
      lines.push(line.trimEnd())
    }
    return lines.join('\n')
  }
}
