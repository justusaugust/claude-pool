import { describe, expect, test } from 'claude-code/testing'
import { commands, lsofCwd, parse, PS, SYS, VM } from './machine'

const NOW = Date.parse('2026-10-17T12:00:00Z')

// What the Linux command set prints: the macOS shapes, built from /proc.
const LINUX_SYS = '0.42 0.30 0.25\n16\n33587892224\nused = 1983.00M\n'
const LINUX_VM = 'page size of 1\nPages active: 13765804032\nPages wired down: 0\nPages occupied by compressor: 0\n'
const LINUX_PS = '    1     0  0.0 /usr/lib/systemd/systemd --system\n  900     1  0.0 /usr/lib/systemd/systemd --user\n 1200   900 12.5 claude --resume\n 1300  1200 50.0 node build.js\n'
const LINUX_CWD = 'p1200\nn/srv/someone/dev/app\n'


describe('machine pane host commands', () => {
  test('macOS keeps its original sysctl, vm_stat, ps and lsof commands', async () => {
    const host = commands('darwin')
    expect(host.sys).toEqual(SYS)
    expect(host.vm).toEqual(VM)
    expect(host.ps).toEqual(PS)
    expect(host.cwd([12, 34])).toEqual(lsofCwd([12, 34]))
    expect(host.findTranscripts('/h/.claude/projects/x')).toContain('-f')
  })

  test('Linux reads /proc and GNU stat, one pid per shell argument', async () => {
    const host = commands('linux')
    expect(host.sys[0]).toBe('sh')
    expect(host.sys[2]).toContain('/proc/loadavg')
    expect(host.vm[2]).toContain('page size of 1')
    expect(host.ps[0]).toBe('sh')
    expect(host.ps.join(' ')).toContain('/proc/')
    expect(host.cwd([12, 34]).slice(-2)).toEqual(['12', '34'])
    expect(host.cwd([12, 34])[2]).toContain('/proc/$p/cwd')
    expect(host.findTranscripts('/h/.claude/projects/x')).toContain('%Y %n')
    expect(host.findAgents('/h/.claude/projects/x/s')).not.toContain('-f')
    for (const argv of [host.sys, host.vm, host.ps, host.cwd([1]), host.findTranscripts('/d'), host.findAgents('/d')]) {
      expect(argv.some(a => a.includes('lsof') || a.includes('sysctl') || a.includes('vm_stat'))).toBe(false)
    }
  })

  test('parse reads the Linux shapes like the macOS ones', async () => {
    const machine = parse({ sys: LINUX_SYS, vm: LINUX_VM, ps: LINUX_PS, lsof: LINUX_CWD, tails: new Map(), agents: new Map() }, NOW, [])
    expect(machine.load).toEqual([0.42, 0.3, 0.25])
    expect(machine.cores).toBe(16)
    expect(machine.memTotal).toBe(33587892224)
    expect(machine.memUsed).toBe(13765804032)
    expect(machine.swapUsed).toBe(1983 * 1024 * 1024)
    expect(machine.sessions.map(s => s.project)).toEqual(['app'])
    expect(machine.sessions[0]?.count).toBe(1)
  })
})
