#!/usr/bin/env python3
"""Run the official monocular example with compilation fixes and no viewer."""
import datetime
import hashlib
import json
import math
import os
import re
from pathlib import Path
import subprocess
import sys
import time

orb, sequence, output = map(Path, sys.argv[1:])
if any(output.iterdir()):
    raise SystemExit('Choose a new output directory; an existing result will not be overwritten.')
calibration_source = orb / 'Examples/Monocular/TUM1.yaml'
calibration = output / 'TUM1-bgr.yaml'
source_text = calibration_source.read_text()
assert 'Camera.RGB: 1' in source_text
# cv::imread decodes color PNGs as BGR. Geometry and distortion stay unchanged.
calibration.write_text(source_text.replace('Camera.RGB: 1', 'Camera.RGB: 0'))
command = [str(orb / 'Examples/Monocular/mono_tum'), str(orb / 'Vocabulary/ORBvoc.txt'),
           str(calibration), str(sequence)]
env = os.environ.copy()
env.pop('DISPLAY', None)
env.pop('WAYLAND_DISPLAY', None)
native_libraries = [orb.parent.parent / 'prefix/lib', orb / 'lib',
                    orb / 'Thirdparty/DBoW2/lib', orb / 'Thirdparty/g2o/lib']
env['LD_LIBRARY_PATH'] = ':'.join(map(str, native_libraries)) + (
    ':' + env['LD_LIBRARY_PATH'] if env.get('LD_LIBRARY_PATH') else '')
started = datetime.datetime.now(datetime.timezone.utc).isoformat()
start = time.perf_counter()
with (output / 'process.log').open('w') as log:
    try:
        result = subprocess.run(command, cwd=output, env=env, stdout=log, stderr=subprocess.STDOUT, timeout=600)
        exit_code = result.returncode
    except subprocess.TimeoutExpired:
        exit_code = 124
        log.write('\nBaseline exceeded the 600-second safety timeout.\n')
elapsed = time.perf_counter() - start
trajectory = output / 'KeyFrameTrajectory.txt'
pose_rows = [line.split() for line in trajectory.read_text().splitlines() if line.strip()] if trajectory.is_file() else []
valid_rows = []
for row in pose_rows:
    values = list(map(float, row))
    if len(values) == 8 and all(map(math.isfinite, values)) and abs(sum(q*q for q in values[4:]) - 1) < 0.02:
        valid_rows.append(values)
poses = len(valid_rows)
frames = sum(bool(line.strip()) and not line.startswith('#') for line in (sequence / 'rgb.txt').read_text().splitlines())
log_text = (output / 'process.log').read_text()
processed = re.search(r'^Processed frames: (\d+)$', log_text, re.MULTILINE)
processed_frames = int(processed.group(1)) if processed else None
completed_loop = exit_code == 0 and 'mean tracking time:' in log_text
summary = {'command': command, 'started_utc': started, 'wall_seconds': elapsed,
           'exit_code': exit_code, 'frames_in_sequence': frames, 'keyframe_poses': poses,
           'invalid_keyframe_rows': len(pose_rows) - poses,
           'processed_frames': processed_frames,
           'completed_standard_loop': completed_loop,
           'frame_count_note': 'Input count is separate from a measured processed count; stock mono_tum has no per-frame export.',
           'calibration_source': str(calibration_source),
           'calibration_sha256': hashlib.sha256(calibration.read_bytes()).hexdigest(),
           'calibration_change': 'Camera.RGB=0 for OpenCV BGR decoding; intrinsics/distortion unchanged',
           'viewer_enabled': False, 'display_variables_removed': True, 'pacing_removed': False,
           'local_library_directories': list(map(str, native_libraries)),
           'shutdown': 'upstream; no custom shutdown patch',
           'depth_or_ground_truth_inputs': False}
(output / 'baseline-summary.json').write_text(json.dumps(summary, indent=2) + '\n')
print(json.dumps(summary, indent=2))
if exit_code or poses < 2 or len(pose_rows) != poses or not completed_loop:
    raise SystemExit('Baseline failed: inspect ' + str(output / 'process.log'))
