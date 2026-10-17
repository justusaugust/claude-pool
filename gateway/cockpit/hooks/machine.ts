import type { Group, Machine, SessionLoad, SessionState } from '../types'

const TREND_MS = 10 * 60_000

// What the host commands printed; register.tsx runs them (macOS: sysctl,
// vm_stat, ps, lsof) and this file only reads the text.
export const SYS = ['sysctl', '-n', 'vm.loadavg', 'hw.ncpu', 'hw.memsize', 'vm.swapusage']
export const VM = ['vm_stat']
export const PS = ['ps', '-axo', 'pid=,ppid=,pcpu=,args=']
export const lsofCwd = (pids: number[]) => ['lsof', '-a', '-d', 'cwd', '-p', pids.join(','), '-Fpn']

// Linux prints the same four shapes from /proc, nproc and GNU stat, so parse()
// stays one function: load/cores/memsize/swap lines, a vm_stat-shaped memory
// report (page size 1, used bytes as "Pages active"), lsof -Fpn pairs from
// /proc/<pid>/cwd, and "mtime path" stat lines.
const LINUX_SYS = ['sh', '-c', [
  'cut -d" " -f1-3 /proc/loadavg',
  'nproc',
  'awk \'/^MemTotal:/{printf "%.0f\\n", $2*1024}\' /proc/meminfo',
  'awk \'/^SwapTotal:/{t=$2} /^SwapFree:/{f=$2} END{printf "used = %.2fM\\n", (t-f)/1024}\' /proc/meminfo',
].join('; ')]
const LINUX_VM = ['sh', '-c', 'awk \'/^MemTotal:/{t=$2} /^MemAvailable:/{a=$2} END{printf "page size of 1\\nPages active: %.0f\\nPages wired down: 0\\nPages occupied by compressor: 0\\n", (t-a)*1024}\' /proc/meminfo']
const linuxCwd = (pids: number[]) => [
  'sh', '-c', 'for p in "$@"; do d=$(readlink "/proc/$p/cwd" 2>/dev/null) && printf "p%s\\nn%s\\n" "$p" "$d"; done', 'sh',
  ...pids.map(String),
]
const linuxFindTranscripts = (dir: string) => ['find', dir, '-maxdepth', '1', '-name', '*.jsonl', '-exec', 'stat', '-c', '%Y %n', '{}', '+']
const linuxFindAgents = (folder: string) => ['find', `${folder}/subagents`, '-name', '*.jsonl', '-mmin', '-15', '-exec', 'stat', '-c', '%Y %n', '{}', '+']

export type Commands = {
  sys: string[]
  vm: string[]
  ps: string[]
  cwd: (pids: number[]) => string[]
  findTranscripts: (dir: string) => string[]
  findAgents: (folder: string) => string[]
}

// The command set for the host; connection.ts names the platform ('darwin' | 'linux').
export function commands(platform: string): Commands {
  if (platform === 'linux') return { sys: LINUX_SYS, vm: LINUX_VM, ps: PS, cwd: linuxCwd, findTranscripts: linuxFindTranscripts, findAgents: linuxFindAgents }
  return { sys: SYS, vm: VM, ps: PS, cwd: lsofCwd, findTranscripts, findAgents }
}

// `tails`: the last lines of each session's transcript, by its pid; `agents`:
// its subagents' and workflow agents' transcripts written in the last 15 min.
export type Outputs = {
  sys: string
  vm: string
  ps: string
  lsof: string
  tails: Map<number, string>
  agents: Map<number, AgentFile[]>
}

export type AgentFile = { file: string; mtime: number; tail: string; description?: string; workflow?: string }

// Where a session keeps its transcripts: ~/.claude/projects/<cwd, every
// character but letters and digits turned into '-'>.
export const projectDir = (home: string, cwd: string) => `${home}/.claude/projects/${cwd.replace(/[^A-Za-z0-9]/g, '-')}`
export const findTranscripts = (dir: string) => [
  'find', dir, '-maxdepth', '1', '-name', '*.jsonl', '-exec', 'stat', '-f', '%m %N', '{}', '+',
]
export const tailTranscript = (file: string) => ['tail', '-n', '80', file]
// A session's own folder sits beside its transcript: <id>.jsonl and <id>/.
export const sessionFolder = (transcript: string) => transcript.replace(/\.jsonl$/, '')
export const findAgents = (folder: string) => [
  'find', `${folder}/subagents`, '-name', '*.jsonl', '-mmin', '-15', '-exec', 'stat', '-f', '%m %N', '{}', '+',
]
// A workflow run's name, from its record: workflows/wf_<id>.json.
export const workflowNameOf = (folder: string, id: string) => ['jq', '-r', '.workflowName // empty', `${folder}/workflows/${id}.json`]
export const tailAgent = (file: string) => ['tail', '-n', '4', file]

// "1790957850 /path/agent-x.jsonl" lines, as find -exec stat prints them.
export function statLines(out: string) {
  return out
    .split('\n')
    .map(line => line.match(/^(\d+) (.+)$/))
    .filter((m): m is RegExpMatchArray => m !== null)
    .map(m => ({ mtime: Number(m[1]) * 1000, file: m[2] ?? '' }))
    // agent transcripts only; a workflow folder's journal.jsonl is not one
    .filter(f => /\/agent-[^/]+\.jsonl$/.test(f.file))
}

// The run a workflow agent belongs to: subagents/workflows/wf_<id>/agent-*.jsonl
export function workflowIdOf(file: string) {
  return file.match(/\/workflows\/(wf_[^/]+)\//)?.[1]
}

// The running sessions, each with its working directory and CPU, from ps and lsof.
export function sessionsOf(ps: string, lsof: string) {
  const cwds = sessionDirs(lsof)
  return processes(ps)
    .filter(p => isClaude(p.args))
    .map(p => ({ pid: p.pid, cpu: p.cpu, cwd: cwds.get(p.pid) }))
}

// Which transcript is whose: in each directory, the N most recently written
// files for its N sessions, the busiest session paired with the newest file.
export function assignTranscripts(sessions: { pid: number; cpu: number; cwd?: string }[], found: Map<string, string>) {
  const files = new Map<number, string>()
  const byCwd = new Map<string, { pid: number; cpu: number }[]>()
  for (const s of sessions) if (s.cwd) byCwd.set(s.cwd, [...(byCwd.get(s.cwd) ?? []), s])
  for (const [cwd, group] of byCwd) {
    const newest = (found.get(cwd) ?? '')
      .split('\n')
      .map(line => line.match(/^(\d+) (.+\.jsonl)$/))
      .filter((m): m is RegExpMatchArray => m !== null)
      .sort((a, b) => Number(b[1]) - Number(a[1]))
      .map(m => m[2] ?? '')
    ;[...group].sort((a, b) => b.cpu - a.cpu).forEach((s, i) => {
      const file = newest[i]
      if (file) files.set(s.pid, file)
    })
  }
  return files
}

export function claudePids(ps: string) {
  return processes(ps)
    .filter(p => isClaude(p.args))
    .map(p => p.pid)
}

function processes(ps: string) {
  return ps
    .split('\n')
    .map(line => line.match(/^\s*(\d+)\s+(\d+)\s+([\d.]+)\s+(.*)$/))
    .filter((m): m is RegExpMatchArray => m !== null)
    .map(m => ({ pid: Number(m[1]), ppid: Number(m[2]), cpu: Number(m[3]), args: m[4] ?? '' }))
}

// One snapshot of the machine: load, what's using the CPU grouped by app, which
// Claude session spawned what, memory and swap.
export function parse({ sys, vm, ps, lsof, tails, agents }: Outputs, now: number, trend: [number, number][]): Machine {

  const [loadLine = '', coresLine = '1', memLine = '0', swapLine = ''] = sys.split('\n')
  const load = (loadLine.match(/[\d.]+/g) ?? []).slice(0, 3).map(Number)
  const cores = Number(coresLine) || 1
  const capacity = cores * 100

  const procs = processes(ps)

  // CPU by app group.
  const groups = new Map<string, Group>()
  for (const p of procs) {
    const name = groupOf(p.args)
    const g = groups.get(name) ?? { name, count: 0, cpu: 0 }
    g.cpu += (p.cpu / capacity) * 100
    if (!p.args.includes('--type=')) g.count += 1
    groups.set(name, g)
  }

  // CPU by the Claude session whose process tree it runs under.
  const parent = new Map(procs.map(p => [p.pid, p.ppid]))
  const claudes = new Set(procs.filter(p => isClaude(p.args)).map(p => p.pid))
  const owner = (pid: number) => {
    for (let at = pid, hops = 0; at > 1 && hops < 40; hops++) {
      if (claudes.has(at)) return at
      at = parent.get(at) ?? 0
    }
    return undefined
  }
  const bySession = new Map<number, { cpu: number; childCpu: number; count: number }>()
  for (const p of procs) {
    const o = owner(p.pid)
    if (o === undefined) continue
    const s = bySession.get(o) ?? { cpu: 0, childCpu: 0, count: 0 }
    s.cpu += (p.cpu / capacity) * 100
    if (p.pid !== o) s.childCpu += p.cpu
    if (p.pid !== o && !p.args.includes('--type=')) s.count += 1
    bySession.set(o, s)
  }
  const cwds = sessionDirs(lsof)
  const sessions: SessionLoad[] = [...bySession]
    .map(([pid, s]) => {
      const t = withAgents(readTranscript(tails.get(pid) ?? '', now, s.childCpu), agents.get(pid) ?? [], now)
      return { project: basename(cwds.get(pid) ?? `pid ${pid}`), cpu: s.cpu, count: s.count, ...t }
    })
    // the ones that need you first, then working, then idle; busiest first within
    .sort((a, b) => RANK[a.state] - RANK[b.state] || b.cpu - a.cpu)
  // Two untitled sessions in one project: oneoffs, oneoffs #2
  const seen = new Map<string, number>()
  for (const s of sessions) {
    const n = (seen.get(s.project) ?? 0) + 1
    seen.set(s.project, n)
    if (n > 1 && !s.title) s.project = `${s.project} #${n}`
  }

  const page = Number(vm.match(/page size of (\d+)/)?.[1] ?? 16384)
  const pages = (label: string) => Number(vm.match(new RegExp(`${label}:\\s+(\\d+)`))?.[1] ?? 0)
  const memUsed = (pages('Pages active') + pages('Pages wired down') + pages('Pages occupied by compressor')) * page
  const swapUsed = Number(swapLine.match(/used = ([\d.]+)M/)?.[1] ?? 0) * 1024 * 1024

  const load1 = load[0] ?? 0
  return {
    at: now,
    load: [load1, load[1] ?? 0, load[2] ?? 0],
    cores,
    trend: [...trend.filter(([t]) => now - t < TREND_MS), [now, load1]],
    groups: [...groups.values()].sort((a, b) => b.cpu - a.cpu).slice(0, 10),
    sessions: sessions.slice(0, 8),
    memUsed,
    memTotal: Number(memLine) || 0,
    swapUsed,
  }
}

const RANK: Record<SessionState, number> = { needs: 0, working: 1, done: 2, unknown: 3 }
const ASKS = new Set(['AskUserQuestion', 'ExitPlanMode'])
const GATED = new Set(['Bash', 'Edit', 'Write', 'MultiEdit', 'NotebookEdit', 'WebFetch'])

type Entry = {
  type?: string
  subtype?: string
  isMeta?: boolean
  isSidechain?: boolean
  timestamp?: string
  lastPrompt?: string
  aiTitle?: string
  customTitle?: string
  message?: { content?: unknown; stop_reason?: string }
}
type Block = { type?: string; name?: string; id?: string; tool_use_id?: string; input?: Record<string, unknown>; text?: string }

// A session's state from the end of its transcript: a tool running (or a
// question waiting for you), the model thinking, or its turn done.
type Read = { state: SessionState; since: number; activity?: string; title?: string; prompt?: string }

export function readTranscript(tail: string, now: number, childCpu: number): Read {
  const entries: Entry[] = []
  for (const line of tail.split('\n')) {
    try {
      entries.push(JSON.parse(line))
    } catch {}
  }
  const main = entries.filter(e => !e.isSidechain)
  const title = [...main].reverse().find(e => e.customTitle || e.aiTitle)
  const prompt = [...main].reverse().find(e => e.type === 'last-prompt')?.lastPrompt
  // Only what moves a conversation: your prompts, Claude's messages, tool
  // results, the turn's end. Hook summaries, notices, local commands (/machine
  // itself) and reminders are bookkeeping, not activity.
  const real = main.filter(e => e.timestamp && isReal(e))
  const last = real[real.length - 1]
  const lastAt = last?.timestamp ? Date.parse(last.timestamp) : undefined
  const base = {
    title: title?.customTitle ?? title?.aiTitle,
    prompt: prompt && !prompt.startsWith('<') ? prompt : undefined,
  }
  if (!last || lastAt === undefined) return { ...base, state: 'unknown' as SessionState, since: now }

  // The turn's start: the last prompt (not a tool result).
  const turnStart = [...real].reverse().find(e => e.type === 'user' && !isToolResult(e.message?.content))
  const turnAt = turnStart?.timestamp ? Date.parse(turnStart.timestamp) : lastAt

  if (last.type === 'system') return { ...base, state: 'done' as SessionState, since: lastAt }
  if (last.type === 'assistant' && last.message?.stop_reason === 'end_turn') return { ...base, state: 'done' as SessionState, since: lastAt }
  if (last.type === 'user' && textOf(last.message?.content).startsWith('[Request interrupted'))
    return { ...base, state: 'done' as SessionState, since: lastAt }

  // Tool calls that have no result yet.
  const answered = new Set(
    real.flatMap(e => blocks(e.message?.content).filter(b => b.type === 'tool_result').map(b => b.tool_use_id)),
  )
  const pending = real.flatMap(e => blocks(e.message?.content).filter(b => b.type === 'tool_use' && !answered.has(b.id)))
  const tool = last.type === 'assistant' ? pending[pending.length - 1] : undefined
  if (tool?.name) {
    const activity = describe(tool)
    const waiting = now - lastAt
    if (ASKS.has(tool.name)) return { ...base, state: 'needs' as SessionState, since: lastAt, activity: `asks: ${activity}` }
    // A gated tool pending a while with nothing running underneath is, almost
    // always, a permission prompt.
    if (GATED.has(tool.name) && waiting > 30_000 && childCpu < 1)
      return { ...base, state: 'needs' as SessionState, since: lastAt, activity: `permission? ${activity}` }
    return { ...base, state: 'working' as SessionState, since: turnAt, activity }
  }
  return { ...base, state: 'working' as SessionState, since: turnAt, activity: 'thinking' }
}

function plural(n: number, word: string) {
  return `${n} ${word}${n === 1 ? '' : 's'}`
}

// Subagents and workflow agents still running keep a session busy even when
// its own turn is done.
function withAgents(t: Read, files: AgentFile[], now: number): Read {
  const running = files.filter(f => now - f.mtime < 60_000 || (now - f.mtime < 15 * 60_000 && isAwaitingTool(f.tail)))
  if (running.length === 0) return t
  const workflows = [...new Set(running.map(f => f.workflow).filter((w): w is string => w !== undefined))]
  const loose = running.filter(f => !f.workflow)
  const parts = [
    ...workflows.map(w => `workflow ${w} · ${plural(running.filter(f => f.workflow === w).length, 'agent')}`),
    ...(loose.length === 1 ? [`agent · ${loose[0]?.description ?? 'running'}`] : loose.length > 1 ? [plural(loose.length, 'agent')] : []),
  ]
  const agents = parts.join(' + ')
  if (t.state === 'done' || t.state === 'unknown') return { ...t, state: 'working', activity: agents }
  return { ...t, activity: t.activity ? `${t.activity} + ${agents}` : agents }
}

// The agent's last line is a tool call still waiting for its result (tool
// calls are written as they stream, so there's no stop reason to go on).
function isAwaitingTool(tail: string) {
  const lines = tail.trim().split('\n')
  try {
    const last = JSON.parse(lines[lines.length - 1] ?? '') as Entry
    return last.type === 'assistant' && blocks(last.message?.content).some(b => b.type === 'tool_use')
  } catch {
    return false
  }
}

function isReal(e: Entry) {
  if (e.type === 'assistant') return true
  if (e.type === 'system') return e.subtype === 'turn_duration'
  if (e.type !== 'user' || e.isMeta) return false
  if (isToolResult(e.message?.content)) return true
  const text = textOf(e.message?.content)
  return !/^<(command-|local-command|bash-|system-reminder)/.test(text)
}

function textOf(content: unknown) {
  if (typeof content === 'string') return content
  return blocks(content)
    .map(b => b.text ?? '')
    .join(' ')
}

function blocks(content: unknown): Block[] {
  return Array.isArray(content) ? (content as Block[]) : []
}

function isToolResult(content: unknown) {
  return blocks(content).some(b => b.type === 'tool_result')
}

function describe(tool: Block) {
  const input = tool.input ?? {}
  const str = (key: string) => (typeof input[key] === 'string' ? (input[key] as string) : undefined)
  const name = (tool.name ?? '').replace(/^mcp__[^_]+(?:_[^_]+)*?__/, '')
  const detail =
    str('description') ??
    str('command') ??
    (str('file_path') && basename(str('file_path') ?? '')) ??
    str('pattern') ??
    str('url') ??
    str('skill') ??
    str('prompt')
  return detail ? `${name} · ${detail.replace(/\s+/g, ' ')}` : name
}

function sessionDirs(lsof: string) {
  const dirs = new Map<number, string>()
  let pid = 0
  for (const line of lsof.split('\n')) {
    if (line.startsWith('p')) pid = Number(line.slice(1))
    if (line.startsWith('n')) dirs.set(pid, line.slice(1))
  }
  return dirs
}

function isClaude(args: string) {
  return basename(args.split(' ')[0] ?? '') === 'claude'
}

// Name the thing a person would recognise: the outermost .app bundle, or the
// tool, with headless browsers and renders pulled out on their own.
function groupOf(args: string) {
  const exe = args.split(' ')[0] ?? ''
  const base = basename(exe)
  if (args.includes('ms-playwright') || args.includes('--headless')) return 'headless Chrome'
  if (base === 'ffmpeg' || base === 'ffprobe') return 'ffmpeg'
  if (args.includes('remotion')) return 'Remotion'
  if (base === 'claude') return 'Claude Code'
  if (base === 'node' || base === 'bun' || base === 'deno') return 'node/bun'
  if (args.includes('CoreSimulator')) return 'iOS Simulator'
  if (args.includes('WebKit')) return 'WebKit'
  const app = args.match(/\/([^/]+)\.app\//)?.[1]
  return app ?? base
}

function basename(path: string) {
  return path.split('/').filter(Boolean).pop() ?? path
}
