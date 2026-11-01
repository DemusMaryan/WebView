#!/usr/bin/env python3
"""Create the explicit TUM RGB-only video fixture, outside the repository."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile

repo = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(repo / "backend"))
from mtw.config import CV_ROOT
from mtw.files import atomic_json

source = CV_ROOT / "datasets/rgbd_dataset_freiburg1_xyz"
destination = CV_ROOT / "datasets/freiburg1_xyz-rgb-30fps.mp4"
rows = [line.split() for line in (source / "rgb.txt").read_text().splitlines()
        if line.strip() and not line.startswith("#")]
assert len(rows) == 798
if destination.exists():
    raise SystemExit(f"Fixture exists; preserve it: {destination}")
with tempfile.TemporaryDirectory(prefix="mtw-fixture-", dir=CV_ROOT / "datasets") as name:
    links = Path(name)
    for i, row in enumerate(rows):
        (links / f"{i:06d}.png").symlink_to(source / row[1])
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-n", "-framerate", "30",
               "-i", str(links / "%06d.png"), "-an", "-c:v", "libx264", "-crf", "18",
               "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(destination)]
    subprocess.run(command, check=True)
provenance = {"source": "TUM RGB-D freiburg1_xyz RGB only", "source_frames": len(rows),
              "source_resolution": [640, 480], "output": str(destination), "command": command,
              "retiming": "All input frames in rgb.txt order, CFR 30 fps; original TUM timestamps replaced",
              "resize": False, "encoding": "H.264 CRF18 yuv420p (lossy, no depth/ground truth)",
              "sha256": hashlib.sha256(destination.read_bytes()).hexdigest(), "bytes": destination.stat().st_size}
atomic_json(destination.with_suffix(".provenance.json"), provenance)
parameters = {"calibration": {"width": 640, "height": 480, "fx": 517.306408, "fy": 516.469215,
                            "cx": 318.643040, "cy": 255.313989, "k1": .262383, "k2": -.953104,
                            "p1": -.005358, "p2": .002628, "k3": 1.163314,
                            "source": "Official ORB-SLAM3 TUM1.yaml; explicit freiburg1_xyz 640x480 fixture only"}}
atomic_json(destination.with_suffix(".parameters.json"), parameters)
print(json.dumps(provenance, indent=2))
