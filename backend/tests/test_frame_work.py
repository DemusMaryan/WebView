from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4
from mtw import frame_work
from mtw.files import atomic_json


class FrameWork(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mtw-frame-work-")
        self.data = Path(self.temp.name)
        self.directory = self.data / "jobs" / str(uuid4())
        self.frames = self.directory / "sequence/rgb"
        self.frames.mkdir(parents=True)
        self.setting = patch.object(frame_work, "DATA", self.data)
        self.setting.start()
        for i in range(3):
            (self.frames / f"{i:06d}.png").write_bytes(bytes([i]) * (10+i))

    def tearDown(self):
        self.setting.stop()
        self.temp.cleanup()

    def enable(self):
        atomic_json(self.directory / "frame-work-policy.json", frame_work.POLICY)

    def test_legacy_jobs_are_never_cleaned(self):
        self.assertFalse(frame_work.cleanup_decoded_frames(self.directory)["applied"])
        self.assertEqual(len(list(self.frames.iterdir())), 3)
        self.assertFalse((self.directory / "previews").exists())

    def test_all_published_previews_survive_while_work_is_removed(self):
        self.enable()
        original = (self.frames / "000001.png").read_bytes()
        frame_work.retain_preview(self.directory, 0)
        frame_work.retain_preview(self.directory, 1)
        atomic_json(self.directory / "latest.json", {"frame_id": 1})
        (self.directory / "input.video").write_bytes(b"retained source")
        (self.directory / "result.json").write_bytes(b"retained result")
        report = frame_work.cleanup_decoded_frames(self.directory)
        self.assertEqual(report["removed_work_files"], 3)
        self.assertFalse(self.frames.exists())
        self.assertEqual((self.directory / "previews/000001.png").read_bytes(), original)
        self.assertEqual(report["retained_preview_frame_ids"], [0, 1])
        self.assertEqual((self.directory / "input.video").read_bytes(), b"retained source")
        self.assertEqual((self.directory / "result.json").read_bytes(), b"retained result")
        self.assertEqual(frame_work.cleanup_decoded_frames(self.directory)["removed_work_files"], 0)

    def test_active_process_group_blocks_cleanup(self):
        self.enable()
        with patch.object(frame_work, "members", return_value=[{"pid": 123}]):
            with self.assertRaisesRegex(RuntimeError, "process group is active"):
                frame_work.cleanup_decoded_frames(self.directory, 123)
        self.assertEqual(len(list(self.frames.iterdir())), 3)

    def test_unexpected_files_and_symlinks_block_all_deletion(self):
        self.enable()
        (self.frames / "keep.txt").write_bytes(b"unrecognized")
        with self.assertRaisesRegex(ValueError, "Unexpected work file"):
            frame_work.cleanup_decoded_frames(self.directory)
        self.assertEqual(len(list(self.frames.iterdir())), 4)

    def test_failure_before_any_snapshot_still_cleans_partial_work(self):
        self.enable()
        report = frame_work.cleanup_decoded_frames(self.directory)
        self.assertEqual(report["removed_work_files"], 3)
        self.assertIsNone(report["preview_frame_id"])
        self.assertEqual(report["retained_preview_frame_ids"], [])

    def test_symlink_cannot_delete_an_external_file(self):
        self.enable()
        external = self.data / "outside.png"
        external.write_bytes(b"must survive")
        (self.frames / "000003.png").symlink_to(external)
        with self.assertRaisesRegex(ValueError, "Unexpected work file"):
            frame_work.cleanup_decoded_frames(self.directory)
        self.assertEqual(external.read_bytes(), b"must survive")
        self.assertEqual(len(list(self.frames.iterdir())), 4)


if __name__ == "__main__":
    unittest.main()
