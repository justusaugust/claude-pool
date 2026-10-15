export type Window = { pct: number; resetsAt?: number }

export type Account = {
  id: string
  label: string
  fiveHour?: Window
  sevenDay?: Window
  seenAt: number
}

// The 5h window's pace: the burn in percentage points per ms (a weighted fit
// of the window's recent readings, eased off through quiet spells) and the
// usage it puts us at now.
export type Pace = { rate: number; pctNow: number }

export type Quota = {
  active?: Account
  others: Account[]
  pace?: Pace
  // the usage guard's own rate (first to last reading of the last 45 min), so
  // "wrap-up due" fires exactly when the guard would
  guardRate?: number
}

// This session's own figures, for the desktop's context and cost chips.
export type Live = { contextPct?: number; contextTokens?: number; usd?: number }

// The usage guard's switch: `off` while ~/.claude/NOWRAP exists (keep going
// past the limit), `wrapping` while a guard's WRAPUP file exists.
export type Wrap = { off: boolean; wrapping: boolean }

// cpu is the share of the whole machine (all cores), 0-100
export type Group = { name: string; count: number; cpu: number }
export type SessionState = 'needs' | 'working' | 'done' | 'unknown'
export type SessionLoad = {
  project: string
  count: number
  cpu: number
  state: SessionState
  // when the current state began: the turn's start while working
  since: number
  activity?: string
  prompt?: string
  title?: string
}

export type Machine = {
  at: number
  load: [number, number, number]
  cores: number
  trend: [number, number][]
  groups: Group[]
  sessions: SessionLoad[]
  memUsed: number
  memTotal: number
  swapUsed: number
}

declare module 'claude-code' {
  interface PluginState {
    cockpit: { quota: Quota; preview: boolean; machine: Machine | null; live: Live; wrap: Wrap; pool: PoolData | null; wrapNoted: boolean }
  }
}

export type PoolAccount = Account & { name: string; status: string; plan?: string; paused: boolean; fableWeekly?: Window; quotaLimits?: Record<string, { status: string | null; resetsAt: number | null }> }
export type PoolSwitch = { from: string; to: string; reason: string; at: number; cacheReadTokens?: number; cacheCreationTokens?: number; thinkingDropped?: boolean }
export type PoolPace = { ratePerHour: number | null; reason: string }
export type PoolData = {
  quotaWaiting?: { since: number; resetsAt: number | null }
  connectorIdentity?: string
  session?: string
  active: string | null
  mode: 'auto' | 'pinned'
  pin?: string | null
  wrap: boolean
  ready: number
  accounts: PoolAccount[]
  updatedAt: number
  thinkingDropped?: boolean
  routing?: { account: string | null; reason: string; peers: number }
  sessions?: { session: string; project?: string; account: string | null; model: string; effort: string; workload: string; pin: string | null; busy: boolean; lastRequestAt: number; reason: string }[]
  switch?: PoolSwitch
  history?: PoolSwitch[]
  forecast?: { renewals?: { hours: number; coveredSeconds: number; possibleSeconds: number; partial: boolean; assumption: string; segments: { at: number; until: number; available: string[]; uncertain: string[] }[]; resets: { account: string; window: string; at: number }[] }; workloads?: { workload: string; requests: number; cacheReadShare: number | null; medianOutputTokens: number; medianThinkingTokens: number | null; basis: string; windows: { account: string; label: string; window: string; ratePerHour: number; samples: number; observedAt: number }[] }[]; secondsLow?: number | null; secondsHigh?: number | null; paceQuality?: string; resetBeforeLimit?: boolean; accounts?: { name: string; label: string; windows: { fiveHour?: PoolPace; sevenDay?: PoolPace } }[]; seconds: number | null; estimatedAccounts: number; eligibleAccounts: number; partial: boolean; nextReset: { account: string; window: string; at: number } | null }
  health?: { backend: string; supervisor: string; failures: number; checkedAt: number | null; draining: boolean; activeRequests: number; requestLimit: number; bufferedBytes: number; storageError: boolean }
  incidents?: { at: number; code: string; message: string; account?: string; status?: number }[]
  recovery?: { state: string; restarts: number; restoredBackup?: boolean }
  persistenceError?: boolean
  interrupted?: boolean
  error?: boolean
}
