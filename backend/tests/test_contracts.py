import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import av
from PIL import Image
from mtw.contracts import JobParameters, write_calibration
from mtw.files import JsonlTail
from mtw.video import decode
from mtw.worker import check_pose
from mtw.capacity import ensure_space

CALIBRATION = {"width": 64, "height": 48, "fx": 50, "fy": 50, "cx": 32, "cy": 24,
               "k1": 0, "k2": 0, "p1": 0, "p2": 0, "source": "synthetic decoder fixture; no SLAM claim"}


class Contracts(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="mtw-test-")
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def video(self):
        path = self.root / "input.mp4"
        with av.open(str(path), "w") as container:
            stream = container.add_stream("libx264", rate=30)
            stream.width, stream.height, stream.pix_fmt = 64, 48, "yuv420p"
            for i in range(3):
                frame = av.VideoFrame.from_image(Image.new("RGB", (64, 48), (i*30, 20, 50)))
                for packet in stream.encode(frame):
                    container.mux(packet)
            for packet in stream.encode():
                container.mux(packet)
        return path

    def parameters(self, **values):
        return JobParameters(calibration=CALIBRATION, **values)

    def test_decode_preserves_resolution_order_pts_and_explicit_calibration(self):
        metadata = decode(self.video(), self.root, self.parameters())
        self.assertEqual(metadata["frames"], 3)
        rows = (self.root / "sequence/rgb.txt").read_text().splitlines()
        self.assertEqual([float(row.split()[0]) for row in rows], [0, .033333333, .066666667])
        with Image.open(self.root / "sequence/rgb/000001.png") as image:
            self.assertEqual(image.size, (64, 48))
        settings = (self.root / "camera.yaml").read_text()
        self.assertIn("Camera1.fx: 50.0", settings)
        self.assertIn("Camera.fps: 30\n", settings)
        self.assertIn("Camera.RGB: 0", settings)

    def test_frame_cap_checks_decoded_frames(self):
        with self.assertRaisesRegex(ValueError, "Decoded frame count"):
            decode(self.video(), self.root, self.parameters(frames=2))
        self.assertEqual(len(list((self.root / "sequence/rgb").glob("*.png"))), 2)

    def test_resolution_cap_checks_actual_decoded_frame(self):
        with self.assertRaisesRegex(ValueError, "Decoded resolution"):
            decode(self.video(), self.root, self.parameters(width=32))

    def test_duration_cap_checks_decoded_pts(self):
        with self.assertRaisesRegex(ValueError, "Decoded duration"):
            decode(self.video(), self.root, self.parameters(duration_seconds=.04))

    def test_calibration_is_required_and_cannot_be_guessed(self):
        with self.assertRaises(ValueError):
            JobParameters.model_validate({})
        params = self.parameters()
        params.calibration.width = 63
        with self.assertRaisesRegex(ValueError, "explicit calibration"):
            decode(self.video(), self.root, params)

    def test_tail_retains_partial_record_and_does_not_repeat_bytes(self):
        path = self.root / "data.jsonl"
        path.write_bytes(b'{"frame_id":0}\n{"frame')
        tail = JsonlTail(path)
        self.assertEqual(list(tail.rows()), [{"frame_id": 0}])
        self.assertEqual(list(tail.rows()), [])
        with path.open("ab") as output:
            output.write(b'_id":1}\n')
        self.assertEqual(list(tail.rows()), [{"frame_id": 1}])
        self.assertEqual(tail.pending, b"")
        self.assertEqual(tail.offset, path.stat().st_size)

    def test_pose_null_and_rotation_quaternion_consistency(self):
        check_pose({"Twc": None, "quaternion_xyzw": None})
        row = {"Twc": [[1,0,0,1],[0,1,0,2],[0,0,1,3],[0,0,0,1]], "quaternion_xyzw": [0,0,0,1]}
        check_pose(row)
        with self.assertRaisesRegex(ValueError, "disagree"):
            check_pose(row | {"quaternion_xyzw": [1,0,0,0]})
        with self.assertRaises(ValueError):
            check_pose(row | {"Twc": None})

    def test_backing_volume_can_be_full_while_native_vhd_reports_free_space(self):
        from collections import namedtuple
        usage = namedtuple("usage", "total used free")
        with patch("mtw.capacity.shutil.disk_usage", side_effect=[usage(10**12, 0, 10**12), usage(10**12, 10**12, 0)]):
            with self.assertRaisesRegex(ValueError, "Insufficient free space on /mnt/c"):
                ensure_space()


if __name__ == "__main__":
    unittest.main()
