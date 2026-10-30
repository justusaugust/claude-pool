"""The launcher becomes Native Claude instead of waiting on it as a child. Offline, fakes only."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

GATEWAY = Path(__file__).resolve().parents[1] / 'gateway'

LAUNCHER = """
import sys
sys.path.insert(0, sys.argv[1])
import launch
launch.NATIVE_CLAUDE = sys.argv[2]
print('before', flush=False)
launch.run_native(['--flag', 'two words'], {'MARK': 'pool', 'PATH': '/usr/bin:/bin'})
print('unreachable')
"""

NATIVE = """#!{python}
import json, os, sys
print(json.dumps({{'pid': os.getpid(), 'argv': sys.argv, 'mark': os.environ.get('MARK')}}))
raise SystemExit(7)
"""


class RunNativeTests(unittest.TestCase):
    def test_launcher_process_becomes_native_claude(self):
        with tempfile.TemporaryDirectory() as tmp:
            native = Path(tmp) / 'claude'
            native.write_text(NATIVE.format(python=sys.executable))
            native.chmod(0o755)
            process = subprocess.Popen([sys.executable, '-c', LAUNCHER, str(GATEWAY), str(native)],
                                       stdout=subprocess.PIPE, text=True)
            out, _ = process.communicate(timeout=30)
            self.assertEqual(process.returncode, 7)
            lines = out.splitlines()
            self.assertEqual(lines[0], 'before')
            seen = json.loads(lines[1])
            self.assertEqual(seen['pid'], process.pid)
            self.assertEqual(seen['argv'], [str(native), '--flag', 'two words'])
            self.assertEqual(seen['mark'], 'pool')
            self.assertNotIn('unreachable', out)


if __name__ == '__main__':
    unittest.main()
