"""The pool service runs `device check` on its own device every few hours, so the check the Board shows stays fresh
(the Board greys a check older than a day). Fake processes only."""
from pathlib import Path
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'gateway'))
import launch


class Proc:
    def __init__(self): self.code, self.killed = None, False
    def poll(self): return self.code
    def kill(self): self.killed, self.code = True, -9
    def wait(self, timeout=None): return self.code


class SelfCheckTests(unittest.TestCase):
    def setUp(self): self.started = []

    def popen(self, args, **kwargs):
        self.started.append((args, kwargs)); return Proc()

    def check(self, running=None, name='vps'): return launch.self_check(running, name, popen=self.popen)

    def test_starts_the_device_check_of_this_device_in_the_background(self):
        running = self.check()
        args, kwargs = self.started[0]
        self.assertEqual(args, [*launch.service_definition()['ProgramArguments'][:2], 'device', 'check', 'vps'])
        self.assertEqual((kwargs['stdin'], kwargs['stdout'], kwargs['stderr']), (subprocess.DEVNULL,) * 3)
        self.assertIsInstance(running, Proc)

    def test_a_machine_that_is_no_registered_device_starts_nothing(self):
        self.assertIsNone(self.check(name=None)); self.assertEqual(self.started, [])

    def test_a_check_still_running_at_the_next_turn_is_stopped_and_started_again(self):
        first = self.check()
        second = self.check(first)
        self.assertTrue(first.killed); self.assertIsNot(second, first); self.assertEqual(len(self.started), 2)
        second.code = 0
        self.check(second)
        self.assertFalse(second.killed)

    def test_the_service_runs_it_every_six_hours_from_ten_minutes_after_start(self):
        self.assertEqual((launch.SELF_CHECK_FIRST, launch.SELF_CHECK_EVERY), (600, 6 * 3600))


if __name__ == '__main__':
    unittest.main()
