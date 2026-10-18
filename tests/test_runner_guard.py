"""scripts/test.py keeps every test away from real tools: stubs first, then only the allowed tools, then the system."""
import os
from pathlib import Path
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]


class RunnerGuardTests(unittest.TestCase):
    def setUp(self):
        self.dirs = os.environ.get('PATH', '').split(os.pathsep)
        if not self.dirs[0].endswith('fake-bin'): self.skipTest('run through scripts/test.py')

    def test_stubs_come_first_then_only_node_then_the_system(self):
        stubs = Path(self.dirs[0])
        self.assertEqual(self.dirs[1:], [str(stubs.parent / 'allowed-bin'), '/usr/bin', '/bin'])
        for name in ['herdr', 'claude', 'claude-pool', 'launchctl', 'systemctl', 'gh', 'tailscale', 'cua']:
            self.assertEqual(subprocess.run([name], capture_output=True).returncode, 97, name)
        self.assertTrue(set(os.listdir(self.dirs[1])) <= {'node'})


if __name__ == '__main__': unittest.main()
