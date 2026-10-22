"""Memory that large requests leave behind goes back to the system; checks run on loopback with synthetic bodies."""
import json
import threading
import time
import unittest
from unittest.mock import patch

import memory
import launch
import test_bridge


class AllocatorTests(unittest.TestCase):
    def test_arena_limit_and_trim_work_on_glibc_and_are_harmless_elsewhere(self):
        self.assertTrue(memory.limit_arenas(2))
        self.assertTrue(memory.trim())
        with patch.object(memory, 'LIBC', None):
            self.assertFalse(memory.limit_arenas(2))
            self.assertFalse(memory.trim())

    def test_backend_gets_two_arenas_unless_the_user_chose_a_value(self):
        env = launch.backend_env({'PATH': '/bin'}, 'client', 'admin')
        self.assertEqual(env['MALLOC_ARENA_MAX'], '2')
        self.assertEqual(env['POOL_CLIENT_TOKENS'], 'cockpit:client')
        self.assertEqual(launch.backend_env({'MALLOC_ARENA_MAX': '4'}, 'client', 'admin')['MALLOC_ARENA_MAX'], '4')


class BridgeTrimTests(unittest.TestCase):
    setUp, tearDown, call = test_bridge.BridgeTests.setUp, test_bridge.BridgeTests.tearDown, test_bridge.BridgeTests.call

    def finished(self):
        end = time.monotonic() + 3
        while self.bridge.requests and time.monotonic() < end: time.sleep(.02)
        self.assertFalse(self.bridge.requests)

    def test_a_finished_large_request_returns_memory_but_small_ones_do_not(self):
        large = json.dumps({'messages': [{'role': 'user', 'content': 'x' * (2 * 1024 * 1024)}]}).encode()
        with patch('bridge.memory.trim', return_value=True) as trim:
            self.assertEqual(self.call('/v1/messages', b'{"messages":[]}')[0], 200)
            self.finished()
            trim.assert_not_called()
            self.assertEqual(self.call('/v1/messages', large)[0], 200)
            self.finished()
            trim.assert_called_once_with()

    def test_trimming_waits_between_bursts_of_large_requests(self):
        large = json.dumps({'messages': [{'role': 'user', 'content': 'x' * (2 * 1024 * 1024)}]}).encode()
        with patch('bridge.memory.trim', return_value=True) as trim:
            for _ in range(3):
                self.assertEqual(self.call('/v1/messages', large)[0], 200)
            self.finished()
            self.assertEqual(trim.call_count, 1)
            self.bridge.trimmed_at -= self.bridge.trim_interval
            self.assertEqual(self.call('/v1/messages', large)[0], 200)
            self.finished()
            self.assertEqual(trim.call_count, 2)


if __name__ == '__main__':
    unittest.main()
