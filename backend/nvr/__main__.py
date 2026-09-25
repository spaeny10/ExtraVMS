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
    uvicorn.run("nvr.api:app", host=settings.host, port=settings.port, log_level="info")
