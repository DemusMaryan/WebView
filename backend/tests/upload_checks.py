"""Opt-in TCP checks for global body limits, including chunked uploads."""
import json
from pathlib import Path
import subprocess
import tempfile
import time
from mtw.config import CV_ROOT, DATA, LIMITS, PORTS
from mtw.files import atomic_json

results = []
with tempfile.TemporaryDirectory(prefix="upload-check-", dir=DATA) as name:
    directory = Path(name)
    video = directory / "oversized.video"
    # Sparse local fixture; no real video is needed to test HTTP byte limits.
    with video.open("wb") as output:
        output.truncate(LIMITS["file_bytes"] + 65537)
    parameters = CV_ROOT / "datasets/freiburg1_xyz-rgb-30fps.parameters.json"
    for label, port, chunked in (("nginx", PORTS["entry"], False), ("api", PORTS["api"], False),
                                  ("api_chunked", PORTS["api"], True)):
        response = directory / f"{label}.response"
        command = ["curl", "--silent", "--show-error", "--max-time", "30", "--output", str(response),
                   "--write-out", "%{http_code}", "-F", f"video=@{video}", "-F", f"parameters=<{parameters}"]
        if chunked:
            command += ["-H", "Transfer-Encoding: chunked"]
        command += [f"http://127.0.0.1:{port}/api/jobs"]
        started = time.monotonic()
        completed = subprocess.run(command, capture_output=True, text=True)
        body = response.read_text() if response.exists() else ""
        result = {"label": label, "method": "POST", "port": port, "path": "/api/jobs", "chunked": chunked,
                  "http_status": completed.stdout, "curl_exit": completed.returncode, "body": body,
                  "seconds": time.monotonic() - started, "stderr": completed.stderr}
        results.append(result)
        atomic_json(DATA / "reports/upload-checks.json", results)
        assert completed.returncode == 0 and completed.stdout == "413", result
print(json.dumps(results, indent=2))
