"""Read-only API validation of completed results from a partial R5 run; no jobs."""
import argparse
import json
import math
from pathlib import Path
import httpx
from mtw.config import PORTS
from mtw.files import atomic_json, read_json
from mtw.worker import check_pose


def validate(client, job_id, expected_frames=798):
    def get(path):
        r = client.get(path)
        r.raise_for_status()
        return r.json()
    state = get(f"/api/jobs/{job_id}")
    assert state["status"] == "succeeded"
    r = get(f"/api/jobs/{job_id}/result")
    assert r["job_id"] == job_id and r["server_id"] == state["server_id"]
    assert r["scale"] == "relative" and r["coordinate_convention"] == "Twc"
    trajectory = get(r["artifacts"]["trajectory.json"])
    points = get(r["artifacts"]["map_points.json"])
    assert len(trajectory["poses"]) == r["final_poses"] > 0
    assert len(points["points"]) == r["points_exported"] > 0
    assert points["points_limit"] == 100000
    assert len(points["points"]) == min(points["points_total"], points["points_limit"])
    assert points["points_truncated"] == (points["points_total"] > len(points["points"]))
    assert (points["map_id"], points["segment_id"]) == (trajectory["map_id"], trajectory["segment_id"])
    for p in points["points"]:
        assert len(p["xyz"]) == 3 and all(math.isfinite(v) for v in p["xyz"])
    final_ids = []
    for p in trajectory["poses"]:
        check_pose(p)
        assert 0 <= p["frame_id"] < expected_frames
        assert abs(p["timestamp"] - p["frame_id"]/30) < 1e-6
        assert (p["map_id"], p["segment_id"]) == (trajectory["map_id"], trajectory["segment_id"])
        final_ids.append(p["frame_id"])
    assert final_ids == sorted(set(final_ids))
    count, live_ids = 0, set()
    with client.stream("GET", r["artifacts"]["snapshots.jsonl"]) as raw:
        raw.raise_for_status()
        for line in raw.iter_lines():
            row = json.loads(line)
            assert row["type"] == "frame" and isinstance(row["tracking_state"], int)
            assert row["frame_id"] == count and abs(row["timestamp"] - count/30) < 1e-6
            check_pose(row)
            assert row["pose_valid"] == (row["Twc"] is not None)
            if row["pose_valid"]:
                live_ids.add(count)
            count += 1
    assert count == r["processed_frames"] == expected_frames
    assert len(live_ids) == r["valid_live_poses"]
    assert set(final_ids) <= live_ids
    c = r["native_summary"]["final_snapshot_consistency"]
    assert c["workers_finished"] and c["active_gba"] == 0 and c["geometry_stable"]
    assert c["geometry_digest_first"] == c["geometry_digest_second"]
    return {"state": state, "result": {k:r[k] for k in ["processed_frames", "valid_live_poses",
            "final_poses", "points_exported", "points_limit", "points_truncated", "worker_wall_seconds"]},
            "validation": {"passed": True, "frame_records": count, "null_poses": count-len(live_ids),
                           "final_snapshot_consistency": c}}


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--evidence", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    if args.output.exists():
        raise ValueError("Validation evidence exists; choose a fresh path")
    source = read_json(args.evidence)
    with httpx.Client(base_url=f"http://127.0.0.1:{PORTS['entry']}", timeout=30, trust_env=False) as client:
        result = {k:validate(client, source["jobs"][k]["id"], source["jobs"][k].get("expected_frames", 798))
                  for k in ("A", "B")}
    assert result["A"]["state"]["server_id"] != result["B"]["state"]["server_id"]
    atomic_json(args.output, {"passed": True, "creates_jobs": False, "jobs": result,
                             "source_evidence": str(args.evidence)})
    print(json.dumps({"passed": True, "results": {k:r["result"] for k,r in result.items()}}))
