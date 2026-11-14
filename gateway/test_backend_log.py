"""The backend's warnings are kept in one private, bounded file instead of being thrown away."""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import backend_log
import launch


class BackendLogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.log = Path(self.tmp.name) / 'backend.log'

    def run_child(self, text):
        code = 'import sys; sys.stderr.write(%r); sys.stdout.write("to stdout")' % text
        process = backend_log.spawn([sys.executable, '-c', code], dict(os.environ), self.log)
        self.assertEqual(process.wait(timeout=20), 0)

    def test_the_backend_is_asked_for_its_warnings(self):
        self.assertEqual(launch.backend_env({'PATH': '/bin'}, 'client', 'admin')['RUST_LOG'], 'error,shunt=warn')

    def test_what_the_backend_writes_to_stderr_lands_in_a_private_file_and_restarts_append(self):
        self.run_child('WARN refresh rejected (invalid_grant)\n')
        self.run_child('WARN second start\n')
        self.assertEqual(self.log.read_text(), 'WARN refresh rejected (invalid_grant)\nWARN second start\n')
        self.assertEqual(self.log.stat().st_mode & 0o777, 0o600)

    def test_a_file_someone_made_readable_is_private_again_after_the_next_start(self):
        self.log.write_text('old\n'); self.log.chmod(0o644)
        self.run_child('new\n')
        self.assertEqual(self.log.stat().st_mode & 0o777, 0o600)

    def test_a_full_file_moves_its_newest_part_aside_and_the_open_handle_keeps_writing(self):
        fd = backend_log.open_log(self.log)
        self.addCleanup(os.close, fd)
        os.write(fd, b'a' * 600 + b'b' * 500)
        self.assertFalse(backend_log.trim(self.log, limit=2000))
        self.assertTrue(backend_log.trim(self.log, limit=1000))
        old = self.log.with_name('backend.log.1')
        self.assertEqual((self.log.stat().st_size, old.read_bytes(), old.stat().st_mode & 0o777), (0, b'b' * 500, 0o600), 'the newest half is kept')
        os.write(fd, b'after\n')
        self.assertEqual(self.log.read_bytes(), b'after\n')

    def test_a_missing_file_or_folder_is_not_an_error(self):
        self.assertFalse(backend_log.trim(Path(self.tmp.name) / 'none' / 'backend.log'))


if __name__ == '__main__': unittest.main()
