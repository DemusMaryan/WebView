"""Small point-metadata contract checks; no SLAM runs or plot fixtures."""
import copy
import importlib.util
from pathlib import Path
import unittest


_spec = importlib.util.spec_from_file_location(
    "analyze_run", Path(__file__).with_name("analyze-run.py")
)
analyze_run = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(analyze_run)


class PointsContractTest(unittest.TestCase):
    def record(self, total=3):
        return {
            "points_total": total,
            "points_exported": 2,
            "points_limit": 2,
            "points_truncated": total > 2,
            "points": [
                {"id": 1, "xyz": [0.0, 1.0, 2.0]},
                {"id": 2, "xyz": [3.0, 4.0, 5.0]},
            ],
        }

    def validate(self, record, limit=2):
        return analyze_run.validate_points_contract(record, "test.points", limit)

    def test_valid_truncated_array_keeps_total_separate_from_exported(self):
        record = self.record(total=3)
        original = copy.deepcopy(record)
        self.assertEqual(self.validate(record), [[0.0, 1.0, 2.0], [3.0, 4.0, 5.0]])
        self.assertEqual(record, original)

    def test_valid_nontruncated_array(self):
        self.assertEqual(
            self.validate(self.record(total=2)),
            [[0.0, 1.0, 2.0], [3.0, 4.0, 5.0]],
        )

    def test_wrong_flags_counts_and_limits_are_rejected(self):
        cases = [
            ("missing truncation flag", {"points_truncated": False}),
            ("nonboolean truncation flag", {"points_truncated": 1}),
            ("exported count differs from array", {"points_exported": 1}),
            ("total smaller than exported", {"points_total": 1}),
            ("negative total", {"points_total": -1}),
            ("declared limit differs from configured limit", {"points_limit": 1}),
            ("zero limit", {"points_limit": 0}),
            ("boolean limit", {"points_limit": True}),
        ]
        for description, changed_fields in cases:
            with self.subTest(description=description):
                record = self.record()
                record.update(changed_fields)
                with self.assertRaises(analyze_run.ValidationError):
                    self.validate(record)

        with self.subTest(description="false claim of truncation on complete array"):
            record = self.record(total=2)
            record["points_truncated"] = True
            with self.assertRaises(analyze_run.ValidationError):
                self.validate(record)

    def test_oversized_and_underfilled_arrays_are_rejected(self):
        with self.subTest(description="array exceeds configured limit"):
            record = self.record()
            record["points"].append({"id": 3, "xyz": [6.0, 7.0, 8.0]})
            record["points_exported"] = 3
            record["points_truncated"] = False
            with self.assertRaises(analyze_run.ValidationError):
                self.validate(record)

        with self.subTest(description="array below min(total, limit)"):
            record = self.record()
            record["points"] = record["points"][:1]
            record["points_exported"] = 1
            with self.assertRaises(analyze_run.ValidationError):
                self.validate(record)

    def test_boolean_values_cannot_impersonate_integer_counts_or_limit(self):
        record = self.record(total=1)
        record["points"] = record["points"][:1]
        record["points_exported"] = 1
        record["points_limit"] = 1
        record["points_truncated"] = False
        for key in ("points_total", "points_exported", "points_limit"):
            with self.subTest(key=key):
                invalid = copy.deepcopy(record)
                invalid[key] = True
                with self.assertRaises(analyze_run.ValidationError):
                    self.validate(invalid, limit=1)


if __name__ == "__main__":
    unittest.main()
