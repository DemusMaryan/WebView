"""WSL df alone cannot detect exhaustion of the Windows volume backing its VHDX."""
import shutil
from .config import CONFIG, DATA


def ensure_space():
    storage = CONFIG["storage"]
    for path, reserve in ((DATA, storage["native_reserve_bytes"]),
                          (storage["backing_mount"], storage["backing_reserve_bytes"])):
        free = shutil.disk_usage(path).free
        if free < reserve:
            raise ValueError(f"Insufficient free space on {path}: {free} bytes free; reserve {reserve}")
