import json
import os
from pathlib import Path


def atomic_json(path: Path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as output:
        json.dump(value, output, allow_nan=False, separators=(",", ":"))
        output.flush()
        os.fsync(output.fileno())
    temporary.replace(path)


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


class JsonlTail:
    """Read each byte once; retain an incomplete last line across polls."""
    def __init__(self, path):
        self.path = path
        self.offset = 0
        self.pending = b""

    def rows(self):
        if not self.path.exists():
            return
        with self.path.open("rb") as source:
            source.seek(self.offset)
            while chunk := source.read(1024 * 1024):
                self.offset += len(chunk)
                lines = (self.pending + chunk).split(b"\n")
                self.pending = lines.pop()
                if len(self.pending) > 2 * 1024 * 1024:
                    raise ValueError("Native JSONL record exceeds limit")
                for line in lines:
                    yield json.loads(line)
