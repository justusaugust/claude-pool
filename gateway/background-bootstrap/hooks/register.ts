import type { EngineInterface, Register } from 'claude-code'
import { account, origin } from './account'
import { baseURL, tokenFile, configFile } from './connection'
import { registerSuiteLimits } from './limits'

async function routing($: EngineInterface, token: string, session: string, body?: { action: string; account?: string }) {
  const response = await $.http.fetch(baseURL + '/cockpit/' + session, {
    method: body ? 'POST' : 'GET',
    headers: { Authorization: 'Bearer ' + token, 'Content-Type': 'application/json' },
    body: body ? JSON.stringify(body) : undefined,
  })
  if (!response.ok) return null
  const state = JSON.parse(response.text)
  return Array.isArray(state.accounts) && ['auto', 'pinned'].includes(state.mode) ? state : null
}

async function connect($: EngineInterface, interactive: boolean): Promise<boolean> {
  await $.env.set('CLAUDE_POOL_CONFIG', configFile)
  await $.env.set('ANTHROPIC_BASE_URL', baseURL)
  await $.env.set('COCKPIT_BASE_URL', baseURL)
  await $.env.set('COCKPIT_TOKEN_FILE', tokenFile)
  await $.env.set('CLAUDE_CODE_GATEWAY_HINT_HEADERS', '1')
  await $.env.set('ANTHROPIC_API_KEY', undefined)
  await $.env.set('ANTHROPIC_AUTH_TOKEN', undefined)
  await $.env.set('CLAUDE_CODE_OAUTH_TOKEN', undefined)
  await $.env.set('CLAUDE_CODE_USE_BEDROCK', undefined)
  await $.env.set('CLAUDE_CODE_USE_VERTEX', undefined)
  await $.env.set('CLAUDE_CODE_USE_FOUNDRY', undefined)
  const session = await $.session.id()
  if (!/^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/.test(session)) return false
  const token = (await $.fs.read(tokenFile)).trim()
  if (!/^[A-Za-z0-9_-]{32,128}$/.test(token)) return false
  const initialized = await $.store.get(session)
  const source = origin ? await $.store.get('origin:' + origin) : undefined
  if (source !== undefined && (typeof source !== 'string' || !/^[a-f0-9-]{36}$/.test(source))) return false
  let selected = account
  // Handoff assigns a new UUID; copy current routing once, never on respawn.
  if (!initialized && typeof source === 'string' && source !== session) {
    const previous = await routing($, token, source)
    if (!previous || (previous.mode === 'pinned' && typeof previous.pin !== 'string')) return false
    selected = previous.mode === 'pinned' ? previous.pin : null
  }
  const body = !initialized && selected !== null ? { action: 'pin', account: selected } : undefined
  const state = await routing($, token, session, body)
  if (!state || (body && (state.mode !== 'pinned' || state.pin !== selected))) return false
  if (!initialized) await $.store.set(session, true)
  await $.env.set('COCKPIT_CONTROL_SOURCE', typeof source === 'string' ? source : session)
  if (origin && interactive) await $.store.set('origin:' + origin, session)
  return true
}

type Capacity = { state: 'ready' | 'quota_wait' | 'unavailable'; nextResetAt: number | null }
type QuotaEngine = { fs: { read(path: string): Promise<string> }; http: Pick<EngineInterface['http'], 'fetch'>; ui: Pick<EngineInterface['ui'], 'status'> }

async function capacity($: QuotaEngine, model: string, wait: boolean, signal: AbortSignal): Promise<Capacity | null> {
  signal.throwIfAborted()
  const token = (await $.fs.read(tokenFile)).trim()
  if (!/^[A-Za-z0-9_-]{32,128}$/.test(token)) throw new Error('Pool token unavailable')
  let abort: () => void = () => undefined
  const cancelled = new Promise<never>((_, reject) => {
    abort = () => reject(new Error('Pool quota wait cancelled'))
    signal.addEventListener('abort', abort, { once: true })
    if (signal.aborted) abort()
  })
  try {
    // HTTP waits do not consume the SDK's 10s hook budget; clock.sleep does.
    const response = await Promise.race([$.http.fetch(baseURL + '/pool/capacity?model=' + encodeURIComponent(model) + (wait ? '&wait=1' : ''), {
      headers: { Authorization: 'Bearer ' + token },
    }), cancelled])
    signal.throwIfAborted()
    if (response.status === 404) return null // Previous bridge: Native retains its normal error handling.
    if (!response.ok) throw new Error('Pool quota availability could not be confirmed')
    const state = JSON.parse(response.text)
    if (!['ready', 'quota_wait', 'unavailable'].includes(state.state) ||
        !(state.nextResetAt === null || typeof state.nextResetAt === 'number' && Number.isFinite(state.nextResetAt))) throw new Error('Invalid pool quota availability')
    return state
  } finally { signal.removeEventListener('abort', abort) }
}

export async function waitForQuota($: QuotaEngine, model: string, signal: AbortSignal, initial?: Capacity | null): Promise<boolean> {
  let state = initial === undefined ? await capacity($, model, false, signal) : initial
  let waited = false
  try {
    while (state?.state === 'quota_wait') {
      waited = true
      await $.ui.status('Pool quota exhausted · automatically continues when capacity returns · Esc to cancel')
      state = await capacity($, model, true, signal)
    }
    signal.throwIfAborted()
    return waited && state?.state === 'ready'
  } finally { if (waited) await $.ui.status(undefined) }
}

export const register: Register = on => {
  let ready = false
  let interactive = false
  registerSuiteLimits(on)
  on('session.start', async ($, e, next) => {
    await $.env.set('COCKPIT_PROJECT', e.cwd.split('/').filter(Boolean).pop())
    interactive = e.isInteractive
    try { ready = await connect($, interactive) } catch { ready = false }
    return next(e)
  })
  on('turn.step', async function* ($, e, next) {
    if (!ready) {
      try { ready = await connect($, interactive) } catch { ready = false }
    }
    if (ready) {
      let emitted = false
      try {
        await waitForQuota($, e.model, next.signal)
        try {
          const stream = next(e)
          try {
            while (true) {
              const item = await stream.next()
              if (item.done) return item.value
              emitted = true
              yield item.value
            }
          } finally { await stream.return(undefined as never) }
        } catch (error) {
          // Only an uncommitted quota error can retry; never replay streamed output or tools.
          if (emitted || next.signal.aborted || !(error instanceof Error) || !('status' in error) || error.status !== 429) throw error
          if (!await waitForQuota($, e.model, next.signal)) throw error
          return yield* next(e)
        }
      } catch {
        // SDK hook exceptions skip to inference; return a stopped step instead.
        const answer = next.signal.aborted ? '' : 'Pool request stopped. Check claude doctor and the last step before retrying; no tools were replayed.'
        if (answer) yield { kind: 'text', index: 0, text: answer }
        return { turnId: e.turnId, index: e.index, answer, toolUses: [], stopReason: 'end_turn', usage: null }
      }
    }
    const answer = 'Pool startup could not be confirmed. Run claude doctor, then retry this session.'
    yield { kind: 'text', index: 0, text: answer }
    return { turnId: e.turnId, index: e.index, answer, toolUses: [], stopReason: 'end_turn', usage: null }
  })
}
