"""Byte boundaries for the user-agreed test gates; no jobs or SLAM."""
import unittest
from parallel_short import check_windows_thresholds, INITIAL_THRESHOLDS_BYTES, EMERGENCY_MINIMUM_BYTES


class R5ResourceTests(unittest.TestCase):
    def check_boundaries(self, limits):
        def sample(values):
            return {"C": {"available_bytes": values["C_free"]}, "memory": {
                "available_bytes": values["Windows_available_RAM"],
                "commit_limit_bytes": 32*1024**3,
                "committed_bytes": 32*1024**3-values["commit_headroom"]}}
        self.assertEqual(check_windows_thresholds(sample(limits), limits, "Test"), limits)
        for name, minimum in limits.items():
            with self.subTest(metric=name), self.assertRaisesRegex(RuntimeError, f"{minimum-1} bytes < {minimum} bytes"):
                check_windows_thresholds(sample(limits | {name: minimum-1}), limits, "Test")

    def test_initial_reserves(self):
        self.assertEqual(INITIAL_THRESHOLDS_BYTES,
            {"C_free": 10737418240, "Windows_available_RAM": 1610612736, "commit_headroom": 2147483648})
        self.check_boundaries(INITIAL_THRESHOLDS_BYTES)

    def test_emergency_reserves(self):
        self.assertEqual(EMERGENCY_MINIMUM_BYTES,
            {"C_free": 5368709120, "Windows_available_RAM": 536870912, "commit_headroom": 1073741824})
        self.check_boundaries(EMERGENCY_MINIMUM_BYTES)
