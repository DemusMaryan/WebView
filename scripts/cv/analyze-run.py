#!/usr/bin/env python3
"""Validate real monocular exports and plot their unmodified relative geometry.

Usage: python analyze-run.py RUN_DIR [FIGURE_OUTPUT] --sequence SEQUENCE_DIR
Writes RUN_DIR/validation.json on success or failure. Never invents input data.
"""
import argparse
from collections import Counter
import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys


STATE_NAMES = {-1: "SYSTEM_NOT_READY", 0: "NO_IMAGES_YET", 1: "NOT_INITIALIZED",
               2: "OK", 3: "RECENTLY_LOST", 4: "LOST", 5: "OK_KLT"}


class ValidationError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise ValidationError(message)


def integer(value, label):
    require(type(value) is int and value >= 0, f"{label}: expected nonnegative integer")
    return value


def finite(value, label):
    require(type(value) in (int, float) and math.isfinite(value), f"{label}: expected finite number")
    return float(value)


def reject_constant(value):
    raise ValidationError(f"Nonfinite JSON constant: {value}")


def parse_float(value):
    parsed = float(value)
    require(math.isfinite(parsed), f"Nonfinite JSON number: {value}")
    return parsed


def read_object(path):
    with path.open(encoding="utf-8") as source:
        data = json.load(source, parse_constant=reject_constant, parse_float=parse_float)
    require(isinstance(data, dict), f"{path.name}: expected JSON object")
    return data


def load_input_index(sequence, report):
    index = sequence / "rgb.txt"
    contents = index.read_bytes()
    frames = []
    for line_number, line in enumerate(contents.decode("utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split()
        require(len(fields) == 2, f"rgb.txt:{line_number}: expected timestamp and image path")
        timestamp = finite(float(fields[0]), f"rgb.txt:{line_number}.timestamp")
        require(not frames or timestamp > frames[-1]["timestamp"], "Input RGB timestamps must strictly increase")
        frames.append({"timestamp": timestamp, "image": fields[1]})
    require(bool(frames), "Input RGB index is empty")
    report["input"] = {"sequence": str(sequence), "rgb_index": str(index),
                       "rgb_index_sha256": hashlib.sha256(contents).hexdigest(),
                       "expected_input_frames": len(frames),
                       "first_timestamp": frames[0]["timestamp"], "last_timestamp": frames[-1]["timestamp"]}
    report["checks"]["independent_input_rgb_index"] = True
    return frames


def validate_pose(value, label, report):
    require(isinstance(value, list) and len(value) == 4, f"{label}: expected nested 4x4 Twc")
    require(all(isinstance(row, list) and len(row) == 4 for row in value), f"{label}: expected nested 4x4 Twc")
    matrix = [[finite(cell, label) for cell in row] for row in value]
    bottom_error = max(abs(matrix[3][i] - (1.0 if i == 3 else 0.0)) for i in range(4))
    rotation = [row[:3] for row in matrix[:3]]
    orthogonality_error = max(
        abs(sum(rotation[i][k] * rotation[j][k] for k in range(3)) - (1.0 if i == j else 0.0))
        for i in range(3) for j in range(3)
    )
    a, b, c = rotation
    determinant = (a[0] * (b[1] * c[2] - b[2] * c[1])
                   - a[1] * (b[0] * c[2] - b[2] * c[0])
                   + a[2] * (b[0] * c[1] - b[1] * c[0]))
    metrics = report["pose_checks"]
    metrics["matrices_checked"] += 1
    metrics["max_bottom_row_error"] = max(metrics["max_bottom_row_error"], bottom_error)
    metrics["max_orthogonality_error"] = max(metrics["max_orthogonality_error"], orthogonality_error)
    metrics["max_determinant_error"] = max(metrics["max_determinant_error"], abs(determinant - 1.0))
    require(bottom_error <= 1e-6, f"{label}: invalid homogeneous bottom row")
    require(orthogonality_error <= 1e-3, f"{label}: rotation is not orthonormal")
    require(abs(determinant - 1.0) <= 1e-3, f"{label}: rotation determinant is not +1")
    return [matrix[0][3], matrix[1][3], matrix[2][3]]


def validate_points(value, label):
    require(isinstance(value, list), f"{label}: expected point array")
    identifiers = set()
    xyz = []
    for index, point in enumerate(value):
        prefix = f"{label}[{index}]"
        require(isinstance(point, dict), f"{prefix}: expected point object")
        identifier = integer(point.get("id"), prefix + ".id")
        require(identifier not in identifiers, f"{prefix}: duplicate point id {identifier}")
        identifiers.add(identifier)
        position = point.get("xyz")
        require(isinstance(position, list) and len(position) == 3, f"{prefix}: expected xyz triple")
        xyz.append([finite(component, prefix + ".xyz") for component in position])
    return xyz


def validate_points_contract(record, label, limit):
    """Keep the full map count distinct from the bounded exported array."""
    integer(limit, label + ".configured_limit")
    require(limit > 0, f"{label}: configured limit must be positive")
    total = integer(record.get("points_total"), label + ".points_total")
    exported = integer(record.get("points_exported"), label + ".points_exported")
    declared_limit = integer(record.get("points_limit"), label + ".points_limit")
    require(declared_limit == limit, f"{label}: points_limit differs from configured limit {limit}")
    truncated = record.get("points_truncated")
    require(type(truncated) is bool, f"{label}: points_truncated must be boolean")
    points = validate_points(record.get("points"), label + ".points")
    require(exported == len(points), f"{label}: points_exported differs from array length")
    require(exported == min(total, limit), f"{label}: exported count must equal min(total, limit)")
    require(truncated == (total > exported), f"{label}: points_truncated contradicts actual counts")
    return points


def validate_quaternion(value, matrix, label, report):
    require(isinstance(value, list) and len(value) == 4, f"{label}: expected quaternion_xyzw quadruple")
    quaternion = [finite(component, label) for component in value]
    norm = math.sqrt(sum(component * component for component in quaternion))
    require(abs(norm - 1.0) <= 1e-4, f"{label}: quaternion norm differs from one")
    x, y, z, w = [component / norm for component in quaternion]
    rotation = [[1 - 2 * (y*y + z*z), 2 * (x*y - z*w), 2 * (x*z + y*w)],
                [2 * (x*y + z*w), 1 - 2 * (x*x + z*z), 2 * (y*z - x*w)],
                [2 * (x*z - y*w), 2 * (y*z + x*w), 1 - 2 * (x*x + y*y)]]
    error = max(abs(rotation[i][j] - matrix[i][j]) for i in range(3) for j in range(3))
    require(error <= 1e-3, f"{label}: quaternion rotation differs from Twc")
    report["pose_checks"]["quaternions_checked"] += 1
    report["pose_checks"]["max_quaternion_norm_error"] = max(report["pose_checks"]["max_quaternion_norm_error"], abs(norm - 1.0))
    report["pose_checks"]["max_quaternion_rotation_error"] = max(report["pose_checks"]["max_quaternion_rotation_error"], error)


def validate_snapshots(path, input_frames, report):
    frames = []
    states = Counter()
    valid_count = 0
    maximum_live = 0
    maximum_total = 0
    map_changes = {}
    track_seconds = []
    previous_elapsed = -1.0
    with path.open(encoding="utf-8") as source:
        for ordinal, line in enumerate(source):
            label = f"snapshots.jsonl:{ordinal + 1}"
            require(bool(line.strip()), f"{label}: blank line instead of frame record")
            row = json.loads(line, parse_constant=reject_constant, parse_float=parse_float)
            require(isinstance(row, dict), f"{label}: expected object")
            require(row.get("type") == "frame", f"{label}: every JSONL record must have type=frame")
            frame_id = integer(row.get("frame_id"), label + ".frame_id")
            require(frame_id == ordinal, f"{label}: frame_id must be consecutive from zero")
            require(ordinal < len(input_frames), f"{label}: extra frame beyond input RGB index")
            processed = integer(row.get("processed_frames"), label + ".processed_frames")
            require(processed == ordinal + 1, f"{label}: processed_frames must equal frame_id + 1")
            timestamp = finite(row.get("timestamp"), label + ".timestamp")
            require(not frames or timestamp > frames[-1]["timestamp"], f"{label}: timestamps must strictly increase")
            require(abs(timestamp - input_frames[ordinal]["timestamp"]) <= 1e-6,
                    f"{label}: timestamp differs from the independently read input RGB index")
            state = row.get("tracking_state")
            require(type(state) is int and state in STATE_NAMES, f"{label}: unrecognized pinned ORB tracking state")
            if "tracking_state_name" in row:
                require(row["tracking_state_name"] == STATE_NAMES[state], f"{label}: tracking state name disagrees with code")
            states[STATE_NAMES[state]] += 1
            pose_valid = row.get("pose_valid")
            require(type(pose_valid) is bool, f"{label}: pose_valid must be boolean")
            require("Twc" in row, f"{label}: missing Twc")
            require("quaternion_xyzw" in row, f"{label}: missing quaternion_xyzw")
            require(pose_valid == (row["Twc"] is not None), f"{label}: pose_valid and Twc disagree")
            require(pose_valid == (row["quaternion_xyzw"] is not None), f"{label}: pose_valid and quaternion disagree")
            if state != 2:
                require(row["Twc"] is None and not pose_valid, f"{label}: non-OK tracking must have null Twc")
            if pose_valid:
                validate_pose(row["Twc"], label + ".Twc", report)
                validate_quaternion(row["quaternion_xyzw"], row["Twc"], label + ".quaternion_xyzw", report)
                valid_count += 1
            require("map_id" in row, f"{label}: missing map_id")
            map_id = row["map_id"]
            require(type(map_id) is int and map_id >= -1, f"{label}: map_id must be integer >= -1")
            segment_id = integer(row.get("segment_id"), label + ".segment_id")
            expected_segment = (frames[-1]["segment_id"] + (map_id != frames[-1]["map_id"])) if frames else 0
            require(segment_id == expected_segment, f"{label}: segment_id must increment exactly on map_id changes")
            change = integer(row.get("map_change_index"), label + ".map_change_index")
            if map_id >= 0:
                require(change >= map_changes.get(map_id, 0), f"{label}: map_change_index decreased within map")
                map_changes[map_id] = change
            points = validate_points_contract(row, label, 2000)
            total = row["points_total"]
            if map_id == -1:
                require(not pose_valid and total == 0, f"{label}: unknown map must have null pose and no points")
            if "track_seconds" in row:
                seconds = finite(row["track_seconds"], label + ".track_seconds")
                require(seconds >= 0, f"{label}: negative tracking duration")
                track_seconds.append(seconds)
            if "elapsed_processing_seconds" in row:
                elapsed = finite(row["elapsed_processing_seconds"], label + ".elapsed_processing_seconds")
                require(elapsed >= max(0, previous_elapsed), f"{label}: elapsed processing time decreased")
                previous_elapsed = elapsed
            maximum_live = max(maximum_live, len(points))
            maximum_total = max(maximum_total, total)
            frames.append({"timestamp": timestamp, "pose_valid": pose_valid, "map_id": map_id,
                           "segment_id": segment_id})
            report["counts"]["snapshots_records_validated"] = len(frames)
    require(bool(frames), "snapshots.jsonl: empty output")
    require(len(frames) == len(input_frames), "JSONL record count differs from input RGB frame count")
    require(valid_count > 0, "snapshots.jsonl: no valid camera poses")
    report["counts"].update(
        snapshots=len(frames), processed_frames=len(frames), valid_snapshot_poses=valid_count,
        invalid_snapshot_poses=len(frames) - valid_count, maximum_live_points=maximum_live,
        maximum_live_points_total=maximum_total, observed_maps=len(map_changes),
        segments_observed=frames[-1]["segment_id"] + 1,
    )
    report["tracking_states"] = dict(sorted(states.items()))
    report["timestamps"] = {"first": frames[0]["timestamp"], "last": frames[-1]["timestamp"]}
    if track_seconds:
        require(len(track_seconds) == len(frames), "Per-frame track_seconds must be present on every frame or none")
        report["track_seconds_from_frames"] = math.fsum(track_seconds)
    report["checks"]["snapshots_numbering_timestamps_tracking_points"] = True
    return frames


def validate_final(trajectory, point_map, frames, report):
    require(trajectory.get("coordinate_convention") == "Twc", "trajectory.json: coordinate_convention must be Twc")
    require(trajectory.get("scale") == "relative", "trajectory.json: scale must be relative")
    map_id = integer(trajectory.get("map_id"), "trajectory.json.map_id")
    require(integer(point_map.get("map_id"), "map_points.json.map_id") == map_id,
            "Final trajectory and points have different map_id")
    segment_id = integer(trajectory.get("segment_id"), "trajectory.json.segment_id")
    require(integer(point_map.get("segment_id"), "map_points.json.segment_id") == segment_id,
            "Final trajectory and points have different segment_id")
    matching_segments = [frame["segment_id"] for frame in frames if frame["map_id"] == map_id]
    require(matching_segments and segment_id == max(matching_segments),
            "Final output must use the most recent observed segment of its map")
    poses = trajectory.get("poses")
    require(isinstance(poses, list) and len(poses) > 0, "trajectory.json: empty/missing poses")
    coordinates = []
    previous_frame = -1
    for index, pose in enumerate(poses):
        label = f"trajectory.json.poses[{index}]"
        require(isinstance(pose, dict), f"{label}: expected object")
        require(pose.get("type") == "pose", f"{label}: expected type=pose in final trajectory")
        frame_id = integer(pose.get("frame_id"), label + ".frame_id")
        require(previous_frame < frame_id < len(frames), f"{label}: frame_id missing, duplicated or unordered")
        previous_frame = frame_id
        timestamp = finite(pose.get("timestamp"), label + ".timestamp")
        require(abs(timestamp - frames[frame_id]["timestamp"]) <= 1e-6, f"{label}: timestamp differs from frame record")
        require(frames[frame_id]["pose_valid"], f"{label}: final pose references invalid live tracking")
        require(integer(pose.get("map_id"), label + ".map_id") == map_id, f"{label}: map_id differs from final map")
        require(frames[frame_id]["map_id"] == map_id, f"{label}: source live frame belongs to a different map")
        pose_segment = integer(pose.get("segment_id"), label + ".segment_id")
        require(pose_segment == segment_id == frames[frame_id]["segment_id"], f"{label}: segment differs from final/source segment")
        position = validate_pose(pose.get("Twc"), label + ".Twc", report)
        validate_quaternion(pose.get("quaternion_xyzw"), pose["Twc"], label + ".quaternion_xyzw", report)
        coordinates.append({"position": position, "frame_id": frame_id, "segment_id": segment_id, "map_id": map_id})
    points = validate_points_contract(point_map, "map_points.json", 100000)
    require(bool(points), "map_points.json: empty point cloud")
    report["map_id"] = map_id
    report["segment_id"] = segment_id
    report["counts"].update(final_poses=len(poses), final_points=len(points))
    report["final_point_contract"] = {key: point_map[key] for key in
                                      ("points_total", "points_exported", "points_limit", "points_truncated")}
    report["checks"]["final_map_pose_geometry_and_points"] = True
    return coordinates, points


def validate_summary(summary, report):
    require(bool(summary), "native-summary.json: empty summary")
    counts = report["counts"]
    expected = {
        "frames_available": report["input"]["expected_input_frames"],
        "processed_frames": counts["processed_frames"], "snapshots": counts["snapshots"],
        "valid_poses": counts["valid_snapshot_poses"],
        "invalid_poses": counts["invalid_snapshot_poses"],
        "final_poses": counts["final_poses"], "final_points": counts["final_points"],
        "points_total": report["final_point_contract"]["points_total"],
        "points_exported": report["final_point_contract"]["points_exported"],
        "points_limit": report["final_point_contract"]["points_limit"],
    }
    verified = {}
    for key, value in expected.items():
        require(integer(summary.get(key), "native-summary." + key) == value,
                f"native-summary.{key}: differs from actual input/exported records")
        verified[key] = value
    require(type(summary.get("points_truncated")) is bool and
            summary["points_truncated"] == report["final_point_contract"]["points_truncated"],
            "native-summary.points_truncated differs from final point metadata")
    require(integer(summary.get("map_id"), "native-summary.map_id") == report["map_id"],
            "native-summary.map_id differs from final map")
    require(integer(summary.get("segment_id"), "native-summary.segment_id") == report["segment_id"],
            "native-summary.segment_id differs from final segment")
    state_counts = summary.get("tracking_state_counts")
    require(isinstance(state_counts, dict), "native-summary.tracking_state_counts must be an object")
    for key, value in state_counts.items():
        integer(value, "native-summary.tracking_state_counts." + key)
    require(state_counts == report["tracking_states"], "native-summary tracking_state_counts differ from JSONL")
    timings = {}
    for key in ("startup_seconds", "processing_seconds", "track_seconds", "shutdown_seconds", "total_seconds"):
        timings[key] = finite(summary.get(key), "native-summary." + key)
        require(timings[key] >= 0, f"native-summary.{key}: negative duration")
    require(timings["processing_seconds"] > 0, "native-summary.processing_seconds must be positive")
    require(timings["track_seconds"] <= timings["processing_seconds"] + 1e-6, "Tracking time exceeds processing time")
    duration_sum = timings["startup_seconds"] + timings["processing_seconds"] + timings["shutdown_seconds"]
    for key in ("shutdown_wait_seconds", "final_snapshot_seconds"):
        if key in summary:
            value = finite(summary[key], "native-summary." + key)
            require(value >= 0, f"native-summary.{key}: negative duration")
            timings[key] = value
            duration_sum += value
    require(duration_sum <= timings["total_seconds"] + 1e-6, "Native total time is smaller than measured phases")
    if "track_seconds_from_frames" in report:
        require(math.isclose(timings["track_seconds"], report["track_seconds_from_frames"], rel_tol=1e-6, abs_tol=1e-6),
                "Native tracking duration differs from sum of per-frame tracking durations")
    fps = finite(summary.get("processing_fps"), "native-summary.processing_fps")
    require(math.isclose(fps, counts["processed_frames"] / timings["processing_seconds"], rel_tol=1e-6, abs_tol=1e-6),
            "native-summary.processing_fps contradicts frames/processing_seconds")
    consistency = summary.get("final_snapshot_consistency")
    require(isinstance(consistency, dict), "Missing native final_snapshot_consistency instrumentation")
    for key in ("mapper_started", "loop_started", "mapper_finished", "loop_finished"):
        require(consistency.get(key) is True, f"final_snapshot_consistency.{key} was not confirmed true")
    require(consistency.get("workers_finished") is True, "Final snapshot workers were not confirmed finished")
    require(integer(consistency.get("active_gba"), "final_snapshot_consistency.active_gba") == 0,
            "Global bundle adjustment remains active at final snapshot")
    require(consistency.get("geometry_stable") is True, "Final snapshot geometry was not confirmed stable")
    first_digest = consistency.get("geometry_digest_first")
    second_digest = consistency.get("geometry_digest_second")
    for label, digest in (("first", first_digest), ("second", second_digest)):
        require(isinstance(digest, str) and re.fullmatch(r"[0-9a-fA-F]{16}", digest) is not None,
                f"Final geometry {label} digest must contain exactly 16 hexadecimal digits")
    require(first_digest.lower() == second_digest.lower(), "Final geometry digests differ")
    comparisons = integer(consistency.get("snapshot_comparison_count"), "final_snapshot_consistency.snapshot_comparison_count")
    require(comparisons >= 1, "At least one exact comparison of two full snapshots is required")
    report["final_snapshot_consistency"] = consistency
    omission_keys = ("retained_numeric_records", "resolved_numeric_records", "omitted_reference_poses",
                     "omitted_other_map_poses", "omitted_nonfinite_poses", "omitted_live_invalid_poses",
                     "omitted_map_or_segment_poses", "omitted_duplicate_poses", "published_keyframes",
                     "ok_tracking_frames", "unavailable_ok_snapshot_poses")
    omissions = {key: integer(summary[key], "native-summary." + key) for key in omission_keys if key in summary}
    resolve_keys = ("retained_numeric_records", "resolved_numeric_records", "omitted_reference_poses",
                    "omitted_other_map_poses", "omitted_nonfinite_poses")
    if all(key in omissions for key in resolve_keys):
        require(omissions["retained_numeric_records"] == sum(omissions[key] for key in resolve_keys[1:]),
                "Retained numeric records do not equal resolved records plus explicit omissions")
    filter_keys = ("resolved_numeric_records", "omitted_live_invalid_poses", "omitted_map_or_segment_poses", "omitted_duplicate_poses")
    if all(key in omissions for key in filter_keys):
        require(omissions["resolved_numeric_records"] == counts["final_poses"] + sum(omissions[key] for key in filter_keys[1:]),
                "Resolved numeric records do not equal final poses plus explicit export omissions")
    if "ok_tracking_frames" in omissions:
        require(omissions["ok_tracking_frames"] == report["tracking_states"].get("OK", 0), "OK tracking frame count differs from JSONL")
    if "unavailable_ok_snapshot_poses" in omissions:
        require(omissions["unavailable_ok_snapshot_poses"] == report["tracking_states"].get("OK", 0) - counts["valid_snapshot_poses"],
                "Unavailable OK poses count differs from tracking/pose validity records")
    if omissions:
        report["numeric_record_coverage"] = omissions
    if "exit_code" in summary:
        require(type(summary["exit_code"]) is int and summary["exit_code"] == 0, "native-summary.exit_code is not zero")
    report["native_summary"] = summary
    report["summary_fields_verified"] = verified
    report["checks"]["native_summary_counts_timing_and_final_consistency"] = True


def validate_streaming(summary, report):
    expected = report["input"]["expected_input_frames"]
    require(integer(summary.get("expected_input_frames"), "run-summary.expected_input_frames") == expected,
            "Wrapper input frame count differs from independently read RGB index")
    require(type(summary.get("exit_code")) is int and summary["exit_code"] == 0, "Wrapper reports unsuccessful process exit")
    require(summary.get("timed_out") is False, "Wrapper reports timeout or missing timeout result")
    require(finite(summary.get("wall_seconds"), "run-summary.wall_seconds") > 0, "Wrapper wall time must be positive")
    observations = summary.get("observations")
    require(isinstance(observations, list) and observations, "Missing independent streaming observations")
    live_records = []
    previous_count = 0
    previous_elapsed = 0.0
    for index, observation in enumerate(observations):
        label = f"run-summary.observations[{index}]"
        require(isinstance(observation, dict), f"{label}: expected object")
        count = integer(observation.get("frame_records"), label + ".frame_records")
        require(previous_count <= count <= expected, f"{label}: frame record count decreased or exceeds input")
        previous_count = count
        last = observation.get("last_frame_id")
        require(type(last) is int and last == count - 1, f"{label}: last_frame_id must equal frame_records-1")
        elapsed = finite(observation.get("elapsed_seconds"), label + ".elapsed_seconds")
        require(elapsed >= previous_elapsed, f"{label}: observation clock decreased")
        previous_elapsed = elapsed
        require(type(observation.get("process_running")) is bool, f"{label}: process_running must be boolean")
        require(type(observation.get("final_trajectory_exists")) is bool, f"{label}: final_trajectory_exists must be boolean")
        if observation["process_running"] and not observation["final_trajectory_exists"] and count > 0:
            if not live_records or count > live_records[-1]:
                live_records.append(count)
    require(len(live_records) >= 2, "No two independently observed growing frame counts while process was alive before final output")
    if "intermediate_received_while_running" in summary:
        require(summary["intermediate_received_while_running"] is True, "Wrapper incremental-output flag contradicts observations")
    report["streaming"] = {"verified": True, "observations_checked": len(observations),
                           "growing_live_frame_records": live_records, "expected_input_frames": expected}
    report["streaming_verified"] = True
    report["checks"]["independent_live_complete_record_counts"] = True


def plot_geometry(coordinates, points, destination, run_dir, report):
    os.environ["MPLCONFIGDIR"] = str(run_dir / "matplotlib-cache")
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    trajectory = np.asarray([record["position"] for record in coordinates], dtype=float)
    groups = []
    start = 0
    for index in range(1, len(coordinates)):
        previous, current = coordinates[index - 1], coordinates[index]
        if (current["segment_id"] != previous["segment_id"] or current["map_id"] != previous["map_id"]
                or current["frame_id"] != previous["frame_id"] + 1):
            groups.append(trajectory[start:index])
            start = index
    groups.append(trajectory[start:])
    cloud = np.asarray(points, dtype=float)
    displayed = min(len(cloud), 5000)
    indices = (np.arange(displayed, dtype=np.int64) * len(cloud)) // displayed
    scatter = cloud[indices]
    figure = plt.figure(figsize=(16, 5.5), layout="constrained")
    try:
        for number, horizontal, vertical, title in ((1, 0, 1, "Raw XY trajectory"), (2, 0, 2, "Raw XZ trajectory")):
            axis = figure.add_subplot(1, 3, number)
            for index, group in enumerate(groups):
                axis.plot(group[:, horizontal], group[:, vertical], color="#145b9a", linewidth=1,
                          marker=".", markersize=2, label="Trajectory (gaps preserved)" if index == 0 else None)
            axis.scatter(*trajectory[0, [horizontal, vertical]], marker="o", color="#128049", label="Start", zorder=3)
            axis.scatter(*trajectory[-1, [horizontal, vertical]], marker="x", color="#c34332", label="End", zorder=4)
            axis.set(xlabel=f"{'XYZ'[horizontal]} (relative units)", ylabel=f"{'XYZ'[vertical]} (relative units)", title=title)
            axis.set_aspect("equal", adjustable="box")
            axis.grid(alpha=0.25)
            axis.legend(fontsize="small")
        axis = figure.add_subplot(1, 3, 3, projection="3d")
        axis.scatter(scatter[:, 0], scatter[:, 1], scatter[:, 2], s=2, alpha=0.3, color="#596776", label="Real map points")
        for index, group in enumerate(groups):
            axis.plot(group[:, 0], group[:, 1], group[:, 2], color="#145b9a", linewidth=1,
                      marker=".", markersize=2, label="Trajectory (gaps preserved)" if index == 0 else None)
        axis.scatter(*trajectory[0], marker="o", color="#128049", label="Start", s=25)
        axis.scatter(*trajectory[-1], marker="x", color="#c34332", label="End", s=30)
        bounds_min = np.minimum(trajectory.min(axis=0), cloud.min(axis=0))
        bounds_max = np.maximum(trajectory.max(axis=0), cloud.max(axis=0))
        center = (bounds_min + bounds_max) / 2
        radius = float((bounds_max - bounds_min).max()) * 0.525
        if radius == 0:
            radius = 1.0
        axis.set_xlim(center[0] - radius, center[0] + radius)
        axis.set_ylim(center[1] - radius, center[1] + radius)
        axis.set_zlim(center[2] - radius, center[2] + radius)
        axis.set_box_aspect((1, 1, 1))
        axis.set(xlabel="X (relative units)", ylabel="Y (relative units)", zlabel="Z (relative units)", title="Raw 3D trajectory and map")
        axis.legend(fontsize="small")
        selection = (f"Point scatter: {displayed}/{len(cloud)} exported points; deterministic display subsample."
                     if displayed < len(cloud) else f"Point scatter: all {len(cloud)} exported points.")
        if report["final_point_contract"]["points_truncated"]:
            selection += f" Export limit: {len(cloud)}/{report['final_point_contract']['points_total']} map points."
        figure.suptitle(f"Monocular reconstruction, map {report['map_id']} — relative scale\n"
                       f"Raw coordinates; tracking/segment gaps preserved; no smoothing or reflection.\n{selection}", fontsize=11)
        destination.parent.mkdir(parents=True, exist_ok=True)
        figure.savefig(destination, dpi=180)
    finally:
        plt.close(figure)
    report["figure"] = {"path": str(destination), "trajectory_poses": len(trajectory),
                        "continuous_trajectory_groups": len(groups), "tracking_and_segment_gaps_preserved": True,
                        "points_available": len(cloud), "points_displayed": displayed,
                        "deterministic_point_subsample": displayed < len(cloud),
                        "bounds_include_all_exported_points": True, "coordinates_transformed": False}
    report["checks"]["raw_geometry_figure"] = True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("figure_output", nargs="?", type=Path)
    parser.add_argument("--sequence", required=True, type=Path,
                        help="Original RGB sequence directory containing the independent rgb.txt input index")
    args = parser.parse_args()
    run_dir = args.run_dir.expanduser().resolve()
    if not run_dir.is_dir():
        parser.error("RUN_DIR must be an existing real run directory")
    destination = (args.figure_output.expanduser().resolve() if args.figure_output
                   else run_dir / "trajectory.png")
    sequence = args.sequence.expanduser().resolve()
    protected = {run_dir / name for name in ("snapshots.jsonl", "trajectory.json", "map_points.json", "native-summary.json", "run-summary.json", "validation.json")}
    protected.add(sequence / "rgb.txt")
    if destination in protected:
        parser.error("FIGURE_OUTPUT must not overwrite input files or validation.json")
    report = {"schema_version": 2, "created_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
              "run_dir": str(run_dir), "valid": False, "data_valid": False,
              "streaming_verified": False,
              "counts": {}, "checks": {}, "errors": [],
              "pose_checks": {"matrices_checked": 0, "max_bottom_row_error": 0.0,
                              "max_orthogonality_error": 0.0, "max_determinant_error": 0.0,
                              "quaternions_checked": 0, "max_quaternion_norm_error": 0.0,
                              "max_quaternion_rotation_error": 0.0,
                              "bottom_row_tolerance": 1e-6, "rotation_tolerance": 1e-3}}
    try:
        input_frames = load_input_index(sequence, report)
        frames = validate_snapshots(run_dir / "snapshots.jsonl", input_frames, report)
        coordinates, points = validate_final(read_object(run_dir / "trajectory.json"),
                                             read_object(run_dir / "map_points.json"), frames, report)
        validate_summary(read_object(run_dir / "native-summary.json"), report)
        report["data_valid"] = True
        validate_streaming(read_object(run_dir / "run-summary.json"), report)
        plot_geometry(coordinates, points, destination, run_dir, report)
        report["valid"] = True
    except Exception as error:
        report["errors"].append(f"{type(error).__name__}: {error}")
    validation_path = run_dir / "validation.json"
    temporary = run_dir / "validation.json.tmp"
    temporary.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(validation_path)
    print(json.dumps({"valid": report["valid"], "data_valid": report["data_valid"],
                      "streaming_verified": report["streaming_verified"],
                      "validation": str(validation_path), "counts": report["counts"],
                      "errors": report["errors"]}, indent=2))
    return 0 if report["valid"] else 1


if __name__ == "__main__":
    sys.exit(main())
