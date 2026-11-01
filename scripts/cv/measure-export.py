#!/usr/bin/env python3
"""Observe complete real frame records while the native process is running."""
import datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

orb, binary, sequence, output = map(Path, sys.argv[1:])
if any(output.iterdir()):
    raise SystemExit('Run directory must be new and empty.')
input_rows = [line.split() for line in (sequence / 'rgb.txt').read_text().splitlines()
              if line.strip() and not line.startswith('#')]
timestamps = [float(row[0]) for row in input_rows]
calibration_source = orb / 'Examples/Monocular/TUM1.yaml'
calibration = output / 'TUM1-bgr.yaml'
source_text = calibration_source.read_text()
assert 'Camera.RGB: 1' in source_text
calibration.write_text(source_text.replace('Camera.RGB: 1', 'Camera.RGB: 0'))
command = [str(binary), str(orb / 'Vocabulary/ORBvoc.txt'), str(calibration), str(sequence), str(output)]
env = os.environ.copy()
env.pop('DISPLAY', None)
env.pop('WAYLAND_DISPLAY', None)
libraries = [orb.parent.parent / 'prefix/lib', orb / 'lib',
             orb / 'Thirdparty/DBoW2/lib', orb / 'Thirdparty/g2o/lib']
env['LD_LIBRARY_PATH'] = ':'.join(map(str, libraries)) + (
    ':' + env['LD_LIBRARY_PATH'] if env.get('LD_LIBRARY_PATH') else '')

class FrameObserver:
    def __init__(self, path):
        self.path = path
        self.source = None
        self.pending = b''
        self.records = 0
        self.last_frame_id = None

    def read_available(self):
        if self.source is None:
            if not self.path.exists():
                return
            self.source = self.path.open('rb')
        data = self.pending + self.source.read()
        lines = data.split(b'\n')
        self.pending = lines.pop()
        for line in lines:
            row = json.loads(line)
            if row.get('type') != 'frame' or row.get('frame_id') != self.records:
                raise ValueError('Non-frame, duplicated or missing input frame record at ordinal ' + str(self.records))
            if self.records >= len(timestamps) or abs(row['timestamp'] - timestamps[self.records]) > 1e-6:
                raise ValueError('Frame timestamp differs from RGB input at ordinal ' + str(self.records))
            self.last_frame_id = row['frame_id']
            self.records += 1

    def close(self):
        if self.source is not None:
            self.source.close()

started = datetime.datetime.now(datetime.timezone.utc).isoformat()
start = time.perf_counter()
observations = []
timed_out = threading.Event()
observer = FrameObserver(output / 'snapshots.jsonl')
observer_error = None
with (output / 'process.log').open('w') as log:
    process = subprocess.Popen(command, cwd=output, env=env, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, encoding='utf-8', errors='replace', bufsize=1)
    def timeout():
        if process.poll() is None:
            timed_out.set()
            process.kill()
    timer = threading.Timer(600, timeout)
    timer.daemon = True
    timer.start()
    try:
        for line in process.stdout:
            log.write(line)
            if line.startswith('[cv-export]'):
                log.flush()
                alive_before = process.poll() is None
                no_final_before = not (output / 'trajectory.json').exists()
                observer.read_available()
                observations.append({'event': line.strip(), 'elapsed_seconds': time.perf_counter() - start,
                                     'process_running': alive_before and process.poll() is None,
                                     'frame_records': observer.records, 'last_frame_id': observer.last_frame_id,
                                     'snapshot_bytes': observer.path.stat().st_size if observer.path.exists() else 0,
                                     'final_trajectory_exists': not no_final_before or (output / 'trajectory.json').exists()})
        exit_code = process.wait()
        observer.read_available()
        if observer.pending:
            raise ValueError('Final JSONL contains a partial record without a newline')
    except Exception as error:
        observer_error = type(error).__name__ + ': ' + str(error)
        log.write('\nObserver failure: ' + observer_error + '\n')
        if process.poll() is None:
            process.kill()
        exit_code = process.wait()
    finally:
        timer.cancel()
        process.stdout.close()
        observer.close()
        if process.poll() is None:
            process.kill()
            process.wait()
elapsed = time.perf_counter() - start
live_counts = sorted({event['frame_records'] for event in observations
                      if event['process_running'] and event['frame_records'] > 0 and not event['final_trajectory_exists']})
intermediate = len(live_counts) >= 2
summary = {'command': command, 'started_utc': started, 'wall_seconds': elapsed,
           'exit_code': exit_code, 'timed_out': timed_out.is_set(), 'observer_error': observer_error,
           'expected_input_frames': len(timestamps), 'frame_records_observed': observer.records,
           'viewer_enabled': False, 'display_variables_removed': True,
           'pacing_removed': True, 'shutdown': 'upstream; export-only completion observation',
           'calibration_source': str(calibration_source),
           'calibration_sha256': hashlib.sha256(calibration.read_bytes()).hexdigest(),
           'calibration_change': 'Camera.RGB=0 for OpenCV BGR decoding; intrinsics/distortion unchanged',
           'local_library_directories': list(map(str, libraries)),
           'depth_or_ground_truth_inputs': False,
           'intermediate_received_while_running': intermediate,
           'distinct_live_frame_counts': live_counts, 'observations': observations}
(output / 'run-summary.json').write_text(json.dumps(summary, indent=2) + '\n')
print(json.dumps({key: value for key, value in summary.items() if key != 'observations'}, indent=2))
if exit_code or timed_out.is_set() or observer_error:
    raise SystemExit('Export process failed: inspect ' + str(output / 'process.log'))
if not intermediate or observer.records != len(timestamps):
    raise SystemExit('Complete frame records were not independently observed during the run.')
for name in ('trajectory.json', 'map_points.json', 'native-summary.json'):
    if not (output / name).is_file():
        raise SystemExit('Missing output: ' + name)
