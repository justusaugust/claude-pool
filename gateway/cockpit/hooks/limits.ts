import type { Register } from 'claude-code'
import { python, suiteHelper } from './connection'

export const registerSuiteLimits = (on: Parameters<Register>[0]) => {
  on('tool.call', { tool: 'Bash' }, async ($, e, next) => {
    if (e.command.startsWith('export CLAUDE_POOL_SUITE_LIMITS=2 ') || !/\b(?:cargo|rustc|go|npm|pnpm|yarn|bun|vitest|jest|pytest|playwright|xcodebuild)\b/.test(e.command)) return next(e)
    try {
      const result = await $.process.run([python, suiteHelper, '--prepare'], { stdin: e.command, timeoutMs: 3000 })
      const prepared = JSON.parse(result.stdout)
      if (result.exitCode === 0 && typeof prepared.command === 'string' && prepared.command.startsWith('export CLAUDE_POOL_SUITE_LIMITS=2 ')) return next({ ...e, command: prepared.command })
    } catch {}
    return { deny: 'Unable to apply suite worker limits; retry this command.' }
  })
}
