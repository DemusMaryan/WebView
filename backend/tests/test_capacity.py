"""Guard byte boundary and measurement failures, without starting a worker."""
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from mtw import capacity


class CapacityTests(unittest.TestCase):
    def test_correct_mount_and_exact_byte_boundary(self):
        config = {"storage": {"native_reserve_bytes": 2 * 1024**3,
                              "backing_mount": "/mnt/c", "backing_reserve_bytes": 5 * 1024**3}}
        with patch.object(capacity, "CONFIG", config), patch.object(capacity.shutil, "disk_usage") as usage:
            usage.side_effect = [SimpleNamespace(free=2 * 1024**3), SimpleNamespace(free=5 * 1024**3)]
            capacity.ensure_space()
            self.assertEqual([call.args[0] for call in usage.call_args_list], [capacity.DATA, "/mnt/c"])
            usage.side_effect = [SimpleNamespace(free=2 * 1024**3), SimpleNamespace(free=5 * 1024**3 - 1)]
            with self.assertRaisesRegex(ValueError, "5368709119 bytes free; reserve 5368709120"):
                capacity.ensure_space()

    def test_measurement_error_never_becomes_zero_or_success(self):
        for error in (OSError("unavailable mount"), TimeoutError("measurement timed out")):
            with self.subTest(error=type(error).__name__), patch.object(capacity.shutil, "disk_usage") as usage:
                usage.side_effect = [SimpleNamespace(free=10 * 1024**3), error]
                with self.assertRaises(type(error)) as raised:
                    capacity.ensure_space()
                self.assertIs(raised.exception, error)


if __name__ == "__main__":
    unittest.main()
