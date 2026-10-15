import type { Register } from 'claude-code'

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    const result = await next(e)
    const session = await $.env.get('COCKPIT_RESPAWN_SESSION')
    const path = await $.env.get('COCKPIT_RESPAWN_RESULT')
    if (!session || !/^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/.test(session) ||
        !path || !/^\/.*\/pool-respawn-controller-[A-Za-z0-9_-]+\/result\.json$/.test(path) || path.includes('/../')) return result
    const text = 'Continue the existing task after this requested respawn. Check the last recorded state before acting. Do not repeat a completed or potentially completed side effect just because its response was interrupted. Keep existing permissions and account routing. If the task is already complete, stop; if approval or clarification is needed, ask.'
    let delivered = false
    for (let attempt = 0; attempt < 20; attempt++) {
      const sent = await $.session.send({ to: { sessionId: session }, text })
      if (sent.isDelivered) { delivered = true; break }
      if (sent.isDelivered !== false) throw new Error('Continuation delivery outcome unknown')
      // Retry only explicit non-delivery; an unknown outcome is never resent.
      await $.clock.sleep(250)
    }
    await $.fs.write(path, JSON.stringify({ delivered, session }))
    return result
  })
  on('turn.step', async function* ($, e) {
    const answer = 'Continuation control completed.'
    yield { kind: 'text', index: 0, text: answer }
    return { turnId: e.turnId, index: e.index, answer, toolUses: [], stopReason: 'end_turn', usage: null }
  })
}
