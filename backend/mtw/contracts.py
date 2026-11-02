import math
from typing import Annotated
from pydantic import BaseModel, ConfigDict, Field, model_validator
from .config import LIMITS


class Calibration(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    width: int = Field(gt=0, le=16384)
    height: int = Field(gt=0, le=16384)
    fx: float = Field(gt=0)
    fy: float = Field(gt=0)
    cx: float
    cy: float
    k1: float
    k2: float
    p1: float
    p2: float
    k3: float = 0
    source: str = Field(min_length=1, max_length=300)

    @model_validator(mode="after")
    def principal_point(self):
        if not (0 <= self.cx < self.width and 0 <= self.cy < self.height):
            raise ValueError("Principal point must be inside the calibrated image")
        return self


class JobParameters(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    calibration: Calibration
    file_bytes: int = Field(default=LIMITS["file_bytes"], gt=0, le=LIMITS["file_bytes"])
    duration_seconds: float = Field(default=LIMITS["duration_seconds"], gt=0, le=LIMITS["duration_seconds"])
    frames: int = Field(default=LIMITS["frames"], gt=0, le=LIMITS["frames"])
    width: int = Field(default=LIMITS["width"], gt=0, le=LIMITS["width"])
    height: int = Field(default=LIMITS["height"], gt=0, le=LIMITS["height"])
    execution_seconds: float = Field(default=LIMITS["execution_seconds"], ge=1, le=LIMITS["execution_seconds"])


def empty_snapshot():
    return {"processed_frames": 0, "total_frames": None, "frame_id": None, "timestamp": None,
            "tracking_state": None, "tracking_state_name": None, "pose_valid": False,
            "Twc": None, "quaternion_xyzw": None, "map_id": None, "segment_id": None,
            "points": [], "points_total": 0, "points_exported": 0,
            "points_limit": LIMITS["snapshot_points"], "points_truncated": False,
            "trajectory": [], "trajectory_limit": LIMITS["snapshot_trajectory"], "frame_url": None,
            "coordinate_convention": "Twc", "scale": "relative"}


def write_calibration(calibration: Calibration, path, fps):
    # OpenCV imread(COLOR) supplies BGR. Camera intrinsics are always explicit.
    values = calibration.model_dump()
    text = '%YAML:1.0\nFile.version: "1.0"\nCamera.type: "PinHole"\n'
    for key in ("fx", "fy", "cx", "cy", "k1", "k2", "p1", "p2", "k3", "width", "height"):
        prefix = "Camera" if key in ("width", "height") else "Camera1"
        text += f"{prefix}.{key}: {values[key]}\n"
    # Upstream Settings requires an integer here; actual input timing is in rgb.txt.
    text += f"Camera.fps: {max(1, round(fps))}\nCamera.RGB: 0\n"
    text += """ORBextractor.nFeatures: 1000
ORBextractor.scaleFactor: 1.2
ORBextractor.nLevels: 8
ORBextractor.iniThFAST: 20
ORBextractor.minThFAST: 7
Viewer.KeyFrameSize: 0.05
Viewer.KeyFrameLineWidth: 1.0
Viewer.GraphLineWidth: 0.9
Viewer.PointSize: 2.0
Viewer.CameraSize: 0.08
Viewer.CameraLineWidth: 3.0
Viewer.ViewpointX: 0.0
Viewer.ViewpointY: -0.7
Viewer.ViewpointZ: -1.8
Viewer.ViewpointF: 500.0
"""
    path.write_text(text, encoding="utf-8")
