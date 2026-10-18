"""Run the Cockpit machine pane's Linux host commands for real; they print the macOS shapes."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

HOOKS = Path(__file__).resolve().parent / 'cockpit/hooks/machine.ts'
NODE = shutil.which('node')
PROBE = '''
import { commands, parse } from %s
const host = commands('linux')
console.log(JSON.stringify({ sys: host.sys, vm: host.vm, ps: host.ps, cwd: host.cwd([Number(process.argv[2])]), find: host.findTranscripts('/proc/self') }))
process.stdin.resume()
let input = ''
process.stdin.on('data', chunk => { input += chunk })
process.stdin.on('end', () => {
  const out = JSON.parse(input)
  const machine = parse({ sys: out.sys, vm: out.vm, ps: out.ps, lsof: out.cwd, tails: new Map(), agents: new Map() }, Date.now(), [])
  console.log(JSON.stringify({ cores: machine.cores, memTotal: machine.memTotal, memUsed: machine.memUsed, load: machine.load, groups: machine.groups.length }))
})
'''


@unittest.skipUnless(sys.platform.startswith('linux') and NODE, 'Linux host commands run only on Linux with Node')
class LinuxMachineCommands(unittest.TestCase):
    def test_proc_commands_print_the_macos_shapes_and_parse(self):
        with tempfile.TemporaryDirectory() as folder:
            probe = Path(folder) / 'probe.mjs'
            probe.write_text(PROBE % json.dumps(str(HOOKS)))
            env = dict(os.environ, NODE_NO_WARNINGS='1')
            node = subprocess.Popen([NODE, '--experimental-strip-types', str(probe), str(os.getpid())], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, env=env)
            argv = json.loads(node.stdout.readline())
            outputs = {}
            for name in ['sys', 'vm', 'ps', 'cwd', 'find']:
                result = subprocess.run(argv[name], capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, name + ': ' + result.stderr)
                outputs[name] = result.stdout
            sys_lines = outputs['sys'].splitlines()
            self.assertEqual(len(sys_lines), 4, outputs['sys'])
            self.assertRegex(sys_lines[0], r'^\d+\.\d+ \d+\.\d+ \d+\.\d+$'); self.assertRegex(sys_lines[1], r'^\d+$')
            self.assertRegex(sys_lines[2], r'^\d{7,}$'); self.assertRegex(sys_lines[3], r'^used = \d+\.\d\dM$')
            self.assertRegex(outputs['vm'], r'^page size of 1\nPages active: \d+\nPages wired down: 0\nPages occupied by compressor: 0\n$')
            ps_lines = outputs['ps'].splitlines()
            self.assertGreater(len(ps_lines), 5)
            for line in ps_lines: self.assertRegex(line, r'^\s*\d+\s+\d+\s+\d+\.\d\s+\S')
            self.assertTrue(any(line.split()[0] == str(os.getpid()) for line in ps_lines), 'this test process is listed')
            self.assertEqual(outputs['cwd'], 'p%d\nn%s\n' % (os.getpid(), os.getcwd()))
            self.assertEqual(outputs['find'], '')
            stdout, _ = node.communicate(json.dumps(outputs), timeout=20)
            machine = json.loads(stdout)
            self.assertEqual(machine['cores'], os.cpu_count())
            self.assertGreater(machine['memUsed'], 0); self.assertLess(machine['memUsed'], machine['memTotal'])
            self.assertEqual(len(machine['load']), 3); self.assertGreater(machine['groups'], 0)
