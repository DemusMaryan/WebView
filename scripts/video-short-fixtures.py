"""Two explicit RGB-only control clips; no resize, no SLAM, no overwrites."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import av

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from mtw.capacity import ensure_space
from mtw.config import CV_ROOT
from mtw.files import atomic_json, read_json


def main():
    source = CV_ROOT / "datasets/freiburg1_xyz-rgb-30fps.mp4"
    assert hashlib.file_digest(source.open("rb"), "sha256").hexdigest() == "37df61791eaefdd063bb4f94791fbbfac1cdb048c7468fde8c57b13c60103fcf"
    parameters = read_json(source.with_suffix(".parameters.json"))
    for seconds in (10, 12):
        ensure_space()
        destination = source.with_name(f"freiburg1_xyz-rgb-30fps-short-{seconds}s.mp4")
        if destination.exists():
            raise ValueError(f"Preserve existing clip: {destination}")
        command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-n",
                   "-i", str(source), "-map", "0:v:0", "-frames:v", str(seconds * 30), "-an",
                   "-c:v", "libx264", "-threads", "2", "-crf", "18", "-pix_fmt", "yuv420p",
                   "-movflags", "+faststart", str(destination)]
        subprocess.run(command, check=True, timeout=60)
        count = 0
        with av.open(str(destination)) as video:
            assert len(video.streams.video) == 1
            for frame in video.decode(video=0):
                assert (frame.width, frame.height) == (640, 480)
                assert frame.pts is not None and abs(float(frame.pts * frame.time_base) - count/30) < 1e-9
                count += 1
        assert count == seconds * 30
        provenance = {"source": str(source), "command": command, "frames": count,
                      "interval_seconds": [0, seconds], "end_exclusive": True,
                      "resolution": [640, 480], "resize": False, "timestamps": "i/30; first PTS=0",
                      "encoding": "H.264 CRF18 yuv420p; re-encoded from verified RGB-only fixture",
                      "bytes": destination.stat().st_size,
                      "sha256": hashlib.file_digest(destination.open("rb"), "sha256").hexdigest()}
        atomic_json(destination.with_suffix(".parameters.json"), parameters)
        atomic_json(destination.with_suffix(".provenance.json"), provenance)
        print(json.dumps(provenance), flush=True)


if __name__ == "__main__":
    main()
