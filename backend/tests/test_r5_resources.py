"""Byte boundaries for the user-agreed test gates; no jobs or SLAM."""
import unittest
from parallel_short import check_windows_thresholds, INITIAL_THRESHOLDS_BYTES, EMERGENCY_MINIMUM_BYTES


class R5ResourceTests(unittest.TestCase):
    def check_boundaries(self, limits):
        def sample(values):
            return {"C": {"available_bytes": values["C_free"]}, "memory": {
                "available_bytes": None, "commit_limit_bytes": None, "committed_bytes": None}}
        self.assertEqual(check_windows_thresholds(sample(limits), limits, "Test"), limits)
        for name, minimum in limits.items():
            with self.subTest(metric=name), self.assertRaisesRegex(RuntimeError, f"{minimum-1} bytes < {minimum} bytes"):
                check_windows_thresholds(sample(limits | {name: minimum-1}), limits, "Test")

    def test_initial_reserves(self):
        self.assertEqual(INITIAL_THRESHOLDS_BYTES, {"C_free": 10737418240})
        self.check_boundaries(INITIAL_THRESHOLDS_BYTES)

    def test_emergency_reserves(self):
        self.assertEqual(EMERGENCY_MINIMUM_BYTES, {"C_free": 5368709120})
        self.check_boundaries(EMERGENCY_MINIMUM_BYTES)

    def test_memory_pressure_or_unavailable_memory_never_blocks_disk_gate(self):
        for memory in ({"available_bytes": 0, "commit_limit_bytes": 1, "committed_bytes": 2},
                       {"available_bytes": None, "commit_limit_bytes": None, "committed_bytes": None,
                        "error": 5}):
            win = {"C": {"available_bytes": 10737418240}, "memory": memory}
            self.assertEqual(check_windows_thresholds(win, INITIAL_THRESHOLDS_BYTES, "Initial"),
                             {"C_free": 10737418240})
