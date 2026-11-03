import os
import sys
import uvicorn
from .config import PORTS

name = sys.argv[1]
if name == "api":
    target, port = "mtw.api:app", PORTS["api"]
elif name in ("compute-1", "compute-2"):
    os.environ["MTW_SERVER_ID"] = name
    target, port = "mtw.compute:app", PORTS[name.replace("-", "_")]
else:
    raise SystemExit("Unknown service")
uvicorn.run(target, host="127.0.0.1", port=port, workers=1, access_log=False,
            proxy_headers=False, timeout_graceful_shutdown=5)
