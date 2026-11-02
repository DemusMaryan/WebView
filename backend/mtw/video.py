"""Decode real video PTS into the existing RGB-only native runner's input format."""
import json
import time
from pathlib import Path
import av
from .contracts import JobParameters, write_calibration
from .files import atomic_json
from .capacity import ensure_space


def decode(video: Path, directory: Path, params: JobParameters, progress=lambda value: None):
    ensure_space()
    if video.stat().st_size > params.file_bytes:
        raise ValueError("Video exceeds file_bytes limit")
    sequence = directory / "sequence"
    (sequence / "rgb").mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    timestamps = []
    first_pts = None
    last_progress = 0
    with av.open(str(video)) as container, (sequence / "rgb.txt").open("w", encoding="utf-8") as index:
        if len(container.streams.video) != 1:
            raise ValueError("Exactly one video stream is required; audio/depth are not consumed")
        stream = container.streams.video[0]
        for frame in container.decode(stream):
            ensure_space()
            # Check actual decoded frames, not only untrusted header metadata.
            count = len(timestamps)
            if count >= params.frames:
                raise ValueError(f"Decoded frame count exceeds frames={params.frames}")
            if frame.width > params.width or frame.height > params.height:
                raise ValueError(f"Decoded resolution {frame.width}x{frame.height} exceeds limit")
            if (frame.width, frame.height) != (params.calibration.width, params.calibration.height):
                raise ValueError("Decoded resolution differs from explicit calibration; no automatic resizing")
            if frame.pts is None or frame.time_base is None:
                raise ValueError("Video frame has no presentation timestamp")
            pts = float(frame.pts * frame.time_base)
            if first_pts is None:
                first_pts = pts
            timestamp = pts - first_pts
            if timestamps and timestamp <= timestamps[-1]:
                raise ValueError("Video presentation timestamps are not strictly increasing")
            if timestamp > params.duration_seconds:
                raise ValueError(f"Decoded duration exceeds duration_seconds={params.duration_seconds}")
            if time.monotonic() - started > params.execution_seconds:
                raise TimeoutError("Decode execution time exceeded")
            # Only RGB pixels; no ground truth or depth. Source resolution is preserved.
            frame.to_image().save(sequence / "rgb" / f"{count:06d}.png", compress_level=1)
            index.write(f"{timestamp:.9f} rgb/{count:06d}.png\n")
            timestamps.append(timestamp)
            if time.monotonic() - last_progress >= 0.5:
                progress({"stage": "decoding", "decoded_frames": len(timestamps), "processed_frames": 0,
                          "total_frames": None, "video_timestamp": timestamp})
                last_progress = time.monotonic()
        if not timestamps:
            raise ValueError("Video contains no decoded RGB frames")
        last_duration = float(frame.duration * frame.time_base) if frame.duration else 0.0
        duration = timestamps[-1] + max(0, last_duration)
        if duration > params.duration_seconds + 1e-6:
            raise ValueError(f"Decoded duration exceeds duration_seconds={params.duration_seconds}")
    fps = (len(timestamps) - 1) / timestamps[-1] if len(timestamps) > 1 else 30.0
    write_calibration(params.calibration, directory / "camera.yaml", fps)
    result = {"frames": len(timestamps), "duration_seconds": duration, "first_source_pts": first_pts,
              "timestamp_origin": "first decoded presentation timestamp", "width": params.calibration.width,
              "height": params.calibration.height, "resize": False, "dropped_frames": 0,
              "decode_seconds": time.monotonic() - started}
    atomic_json(directory / "video.json", result)
    return result
