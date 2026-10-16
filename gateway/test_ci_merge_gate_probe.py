"""Temporary red phase for live merge-gate verification; removed before merge."""
import unittest


class RequiredCIMergeGateProbe(unittest.TestCase):
    def test_intentional_failure_blocks_merge(self):
        self.fail("Intentional CI failure: verify required checks block merging.")
