"""The pool service writes each reserve window's Shunt options at start and every minute, so they switch at the window's
edges (Shunt has no clock). Fake Shunt process, bridge and clock; nothing starts and no port opens."""
from datetime import datetime
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'gateway'))
import accounts_cli
import launch

ZONE = 'Europe/Stockholm'
WINDOW = {'reserveAt': 90, 'reserveHours': [6, 24], 'reserveTimezone': ZONE, 'reserveSoft': True}
BASE = ('[providers.anthropic]\nkind = "anthropic"\nauth = "claude_oauth"\n\n'
        '[[providers.anthropic.accounts]]\nname = "ops"\n\n'
        '[[providers.anthropic.accounts]]\nname = "intern"\n\n[server]\nbind = "x"\n')
DAY_TOML = BASE.replace('name = "intern"\n', 'name = "intern"\npriority = 200\n')


def at(text):
    """Wall time in Stockholm as epoch seconds."""
    return datetime.fromisoformat(text).replace(tzinfo=ZoneInfo(ZONE)).timestamp()


class Clock:
    """launch's `time`: the test moves the monotonic and the wall clock; the loop's own 0.5 s waits stay real."""
    def __init__(self, wall): self.mono, self.wall = 1000.0, wall
    def monotonic(self): return self.mono
    def time(self): return self.wall


class Shunt:
    pid = 4242
    def poll(self): return None
    def terminate(self): pass
    def wait(self, timeout=None): return 0
    def kill(self): pass


class Bridge:
    """What the service loop asks of the PoolBridge; it serves until the test is done."""
    made = []

    def __init__(self, *args, **kwargs):
        self.health, self.recovery, self.requests, self.draining = {'failures': 0, 'backend': 'ready'}, {'restarts': 0}, [], False
        self.journal, self.done = Mock(), threading.Event()
        Bridge.made.append(self)

    def serve_forever(self): self.done.wait(30)

    def __getattr__(self, name): return lambda *args, **kwargs: None


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.runtime, self.stores, self.config = root / 'state', root / 'stores', root / 'cfg' / 'config.json'
        self.config.parent.mkdir(mode=0o700)
        self.config.write_text(json.dumps({'accounts': [{'name': 'ops'}, {'name': 'intern', **WINDOW}]}))
        self.app = root / 'app'; (self.app / 'bin').mkdir(parents=True); (self.app / 'bin/shunt').write_text('')
        self.toml = self.runtime / 'shunt.toml'

    def wait_for(self, text, seconds=5):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self.toml.exists() and self.toml.read_text() == text: return True
            time.sleep(.05)
        return False

    def test_the_service_writes_the_window_at_start_and_switches_it_within_a_minute(self):
        clock, Bridge.made = Clock(at('2026-11-03T10:00')), []
        with patch.object(launch, 'RUNTIME', self.runtime), patch.object(launch, 'STORES', self.stores), \
             patch.object(launch, 'CONFIG', self.config), patch.object(launch, 'HERE', self.app), patch.object(launch, 'time', clock), \
             patch.object(launch, 'backend_config', return_value=BASE), patch.object(launch, 'recover_orphan', return_value=False), \
             patch.object(launch.subprocess, 'Popen', return_value=Shunt()), patch.object(launch.memory, 'limit_arenas'), \
             patch.object(launch, 'PoolBridge', Bridge), patch.object(launch, 'NativeAuth'), patch.object(launch, 'Alerts'), \
             patch.object(launch.readings, 'Puller'), patch.object(launch.readings, 'AuthProbe'), patch.object(launch.signal, 'signal'):
            service = threading.Thread(target=launch.daemon, args=('client', 'admin'), daemon=True)
            service.start()
            try:
                # Written as at night (BASE); the loop's first turn writes this hour's options: Intern's soft reserve holds.
                self.assertTrue(self.wait_for(DAY_TOML), self.toml.read_text() if self.toml.exists() else 'no runtime file')
                clock.wall, clock.mono = at('2026-11-04T00:00'), clock.mono + 30
                time.sleep(1.2)
                self.assertEqual(self.toml.read_text(), DAY_TOML)  # half a minute: not yet looked again
                clock.mono += 31
                self.assertTrue(self.wait_for(BASE), self.toml.read_text())  # midnight: no reserve until 06:00
            finally:
                for bridge in Bridge.made: bridge.done.set()
                service.join(20)
        self.assertFalse(service.is_alive())
        self.assertEqual(launch.RESERVE_WINDOW_EVERY, 60)

    def test_it_writes_the_runtime_file_that_local_paths_env_names_and_survives_a_bad_one(self):
        self.runtime.mkdir()
        paths, _ = accounts_cli.local_paths_env(self.runtime, self.stores)
        self.assertEqual(paths, [self.toml])
        self.toml.write_text(BASE)
        with patch.object(launch, 'RUNTIME', self.runtime), patch.object(launch, 'STORES', self.stores), patch.object(launch, 'CONFIG', self.config):
            self.assertTrue(launch.reserve_windows(at('2026-11-03T06:00')))
            self.assertEqual(self.toml.read_text(), DAY_TOML)
            self.assertFalse(launch.reserve_windows(at('2026-11-03T06:01')))
            self.toml.write_text('not toml [')
            self.assertFalse(launch.reserve_windows(at('2026-11-03T07:00')))  # the loop goes on; the next minute tries again
            self.assertEqual(self.toml.read_text(), 'not toml [')


if __name__ == '__main__':
    unittest.main()
