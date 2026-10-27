"""Run the NVR: python -m nvr (from the backend directory) or run.ps1."""
import logging
import os
import sys

os.environ.setdefault("CUDA_DEVICE_ORDER", "PCI_BUS_ID")
# Windows consoles/pipes default to cp1252; synopses contain characters like "→".
for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")

import uvicorn

from .config import settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)

if __name__ == "__main__":
    # Before anything opens the database (nvr.api imports nvr.db, which opens nvr.db at import): make room on the
    # data dir's own filesystem from the filesystem alone. At 0 bytes free SQLite can't open its WAL, and the service
    # crash-looped without ever reaching the retention pass that would have freed space (diskguard.py).
    from . import diskguard
    diskguard.preopen(settings.data_dir, settings.disk_emergency_free_gb, settings.backup_dir)
    # a central instance's share of the GPU it shares with other Sites, before any model loads (gpumem.py); none on
    # one using its host's inference service: no model loads here, and the cap would import torch for nothing
    if settings.gpu_mem_mb > 0 and not settings.infer_socket:
        from . import gpumem
        gpumem.apply()
    # proxy_headers=False: no reverse proxy sits in front of the NVR, and uvicorn's default (trust
    # X-Forwarded-For from 127.0.0.1) let any local process choose the client address the app sees
    uvicorn.run("nvr.api:app", host=settings.host, port=settings.port, log_level="info", proxy_headers=False)
