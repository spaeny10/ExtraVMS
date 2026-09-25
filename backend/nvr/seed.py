"""Import CAMERA_<n>_* entries from .env into the cameras table (first run convenience)."""
from __future__ import annotations

import logging

from dotenv import dotenv_values

from .config import ROOT
from .db import db

log = logging.getLogger("nvr.seed")


def seed_cameras_from_env() -> None:
    env = dotenv_values(ROOT / ".env")
    existing = {c["id"] for c in db.cameras()}
    n = 1
    while env.get(f"CAMERA_{n}_HOST"):
        cid = f"cam{n}"
        if cid not in existing:
            db.upsert_camera({
                "id": cid,
                "name": env.get(f"CAMERA_{n}_NAME") or cid,
                "host": env[f"CAMERA_{n}_HOST"],
                "onvif_port": int(env.get(f"CAMERA_{n}_ONVIF_PORT") or 80),
                "rtsp_port": int(env.get(f"CAMERA_{n}_RTSP_PORT") or 554),
                "username": env.get(f"CAMERA_{n}_USER") or "admin",
                "password": env.get(f"CAMERA_{n}_PASS") or "",
                "main_path": env.get(f"CAMERA_{n}_MAIN_PATH") or "/main",
                "sub_path": env.get(f"CAMERA_{n}_SUB_PATH") or "/sub",
            })
            log.info("added camera %s from .env", cid)
        n += 1
