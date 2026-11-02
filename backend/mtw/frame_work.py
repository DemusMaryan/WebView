"""Retain published previews, then remove only new jobs' disposable RGB work files."""
import os
from pathlib import Path
from uuid import UUID
from .config import DATA, LIMITS
from .files import atomic_json, read_json
from .processes import members

POLICY = {"version": 1, "cleanup_decoded_frames": True}


def enabled(directory):
    return read_json(directory / "frame-work-policy.json") == POLICY


def checked_directory(directory):
    directory = Path(directory)
    UUID(directory.name)
    if directory.is_symlink() or directory.resolve().parent != (DATA / "jobs").resolve():
        raise ValueError("Frame work directory is outside this project's job storage")
    return directory.resolve()


def retain_preview(directory, frame_id):
    if not enabled(directory):
        return False  # Existing jobs and standalone CV runs keep their original layout.
    directory = checked_directory(directory)
    if not isinstance(frame_id, int) or isinstance(frame_id, bool) or not 0 <= frame_id < LIMITS["frames"]:
        raise ValueError("Invalid preview frame_id")
    source = directory / "sequence/rgb" / f"{frame_id:06d}.png"
    previews = directory / "previews"
    if previews.is_symlink():
        raise ValueError("Preview directory must not be a symlink")
    previews.mkdir(exist_ok=True)
    destination = previews / source.name
    if source.is_symlink() or destination.is_symlink():
        raise ValueError("Preview files must not be symlinks")
    if destination.exists():
        if source.exists() and not os.path.samefile(source, destination):
            raise ValueError("An immutable preview already has different content")
        return True
    if not source.is_file() or source.resolve() != directory / "sequence/rgb" / source.name:
        raise ValueError("Published frame is missing or outside the job directory")
    # Same native filesystem: atomic link of an already completely decoded PNG.
    # Every frame exposed by latest.json remains valid for a slow/in-flight reader.
    os.link(source, destination, follow_symlinks=False)
    return True


def cleanup_decoded_frames(directory, worker_pid=None):
    if not enabled(directory):
        return {"applied": False, "reason": "legacy_job_without_cleanup_policy"}
    directory = checked_directory(directory)
    if worker_pid is not None and members(worker_pid):
        raise RuntimeError("Cannot remove work frames while the process group is active")
    latest = read_json(directory / "latest.json", {})
    frame_id = latest.get("frame_id")
    if frame_id is not None:
        retain_preview(directory, frame_id)
    frames = directory / "sequence/rgb"
    files = []
    if frames.exists():
        if frames.is_symlink() or frames.resolve() != directory / "sequence/rgb":
            raise ValueError("Work frame directory escapes the job directory")
        files = list(frames.iterdir())
        # Validate the entire set before deleting anything, including interrupted PNG writes.
        for path in files:
            if (path.is_symlink() or not path.is_file() or path.suffix != ".png"
                    or not path.stem.isdigit() or path.name != f"{int(path.stem):06d}.png"
                    or not 0 <= int(path.stem) < LIMITS["frames"]):
                raise ValueError("Unexpected work file; refusing cleanup")
    removed_bytes = sum(path.stat().st_size for path in files)
    # Linked preview blocks remain allocated: do not count those as reclaimed space.
    reclaimable_blocks = sum(path.stat().st_blocks * 512 for path in files if path.stat().st_nlink == 1)
    for path in files:
        path.unlink()
    if frames.exists():
        frames.rmdir()
    previews = sorted(int(path.stem) for path in (directory / "previews").glob("*.png"))
    result = {"applied": True, "process_group_empty": True, "removed_work_files": len(files),
              "removed_logical_bytes": removed_bytes, "reclaimed_file_blocks_bytes": reclaimable_blocks,
              "preview_frame_id": frame_id, "retained_preview_frame_ids": previews,
              "source_video_and_results_preserved": True}
    atomic_json(directory / "frame-work-cleanup.json", result)
    return result
