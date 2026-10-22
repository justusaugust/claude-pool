"""Agent scratch on disk instead of a RAM-backed /tmp, and pruning of what ended sessions left; all in temporary folders."""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

import scratch

MOUNTS = 'tmpfs /tmp tmpfs rw,nosuid,nodev,size=16G 0 0\n/dev/sda1 / ext4 rw,relatime 0 0\n'
DAY = 86400
ENDED, LIVE, FRESH = 'aaaaaaaa-1111-4111-8111-111111111111', 'bbbbbbbb-2222-4222-8222-222222222222', 'cccccccc-3333-4333-8333-333333333333'


class TmpdirTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)

    def test_agents_get_a_private_folder_on_disk_when_tmp_lives_in_memory(self):
        path = scratch.agent_tmpdir({}, home=self.home, tempdir='/tmp', mounts=MOUNTS)
        self.assertEqual(path, self.home / '.cache' / 'claude-pool' / 'tmp')
        self.assertEqual(path.stat().st_mode & 0o777, 0o700)

    def test_tmp_on_disk_an_explicit_choice_or_no_mount_table_change_nothing(self):
        self.assertIsNone(scratch.agent_tmpdir({}, home=self.home, tempdir='/var/tmp', mounts=MOUNTS))
        self.assertIsNone(scratch.agent_tmpdir({'CLAUDE_CODE_TMPDIR': '/srv/t'}, home=self.home, tempdir='/tmp', mounts=MOUNTS))
        self.assertIsNone(scratch.agent_tmpdir({}, home=self.home, tempdir='/tmp', mounts=None))

    def test_the_cache_folder_follows_xdg(self):
        path = scratch.agent_tmpdir({'XDG_CACHE_HOME': str(self.home / 'xdg')}, home=self.home, tempdir='/tmp', mounts=MOUNTS)
        self.assertEqual(path, self.home / 'xdg' / 'claude-pool' / 'tmp')


class PruneTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'root'
        self.base = self.root / ('claude-' + str(os.getuid()))
        self.now = time.time()

    def session(self, project, session, age_days):
        path = self.base / project / session / 'scratchpad'
        path.mkdir(parents=True)
        (path / 'big.bin').write_bytes(b'x' * 1000)
        old = self.now - age_days * DAY
        for p in [path / 'big.bin', path, path.parent]: os.utime(p, (old, old))
        return path.parent

    def test_only_stale_folders_of_ended_sessions_go(self):
        ended = self.session('-home-a-projects-x', ENDED, 5)
        live = self.session('-home-a-projects-x', LIVE, 5)
        fresh = self.session('-home-a-projects-y', FRESH, 1)
        skills = self.session('bundled-skills', '2.1.306', 5)
        removed = scratch.prune([self.root], live={LIVE}, now=self.now)
        self.assertTrue(skills.exists(), 'bundled skills of running versions stay')
        self.assertFalse(ended.exists()); self.assertTrue(live.exists()); self.assertTrue(fresh.exists())
        self.assertEqual(removed['folders'], 1); self.assertEqual(removed['bytes'], 1000)

    def test_a_recent_file_deep_inside_keeps_the_folder(self):
        folder = self.session('-home-a-projects-x', ENDED, 5)
        (folder / 'scratchpad' / 'new.txt').write_text('heute')
        scratch.prune([self.root], live=set(), now=self.now)
        self.assertTrue(folder.exists())

    def test_old_edit_diffs_go_and_symlinks_are_never_followed(self):
        diffs = self.base / 'bash-edit-diff'; diffs.mkdir(parents=True)
        (diffs / 'old.diff').write_text('x'); os.utime(diffs / 'old.diff', (self.now - 2 * DAY,) * 2)
        (diffs / 'new.diff').write_text('y')
        outside = Path(self.tmp.name) / 'outside'; outside.mkdir(); (outside / 'keep.txt').write_text('k')
        link = self.base / '-home-a-projects-z'; link.symlink_to(outside)
        scratch.prune([self.root], live=set(), now=self.now)
        self.assertFalse((diffs / 'old.diff').exists()); self.assertTrue((diffs / 'new.diff').exists())
        self.assertTrue((outside / 'keep.txt').exists())

    def test_live_sessions_come_from_the_registry_of_running_claude_processes(self):
        folder = Path(self.tmp.name) / 'sessions'; folder.mkdir()
        (folder / (str(os.getpid()) + '.json')).write_text(json.dumps({'sessionId': 'eeee-mine'}))
        (folder / '999999999.json').write_text(json.dumps({'sessionId': 'ffff-gone'}))
        self.assertEqual(scratch.live_sessions(folder), {'eeee-mine'})


if __name__ == '__main__':
    unittest.main()
