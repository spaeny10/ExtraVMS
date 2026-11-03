"""Exports: per-camera MP4s (original / SD, with or without audio), the grid video, the report, checksums, the zip and
the locks; the API's limits. Recordings are stood in for by ffmpeg test clips (no MediaMTX).

Run: ..\\.venv\\Scripts\\python.exe tests\\test_exports.py   (from backend/)
"""
import asyncio
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

os.environ["NVR_DATA_DIR"] = tempfile.mkdtemp(prefix="nvr-exports-test-")
os.environ["NVR_RUNTIME_DIR"] = tempfile.mkdtemp(prefix="nvr-exports-runtime-")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

from nvr import api, exports, mediamtx  # noqa: E402
from nvr.db import db  # noqa: E402

FF = shutil.which("ffmpeg") or r"C:\ffmpeg\bin\ffmpeg.exe"
T0 = time.time() - 3600
SRC = Path(tempfile.mkdtemp(prefix="nvr-exports-src-"))


def _clip(name: str, seconds: int, audio: bool) -> Path:
    p = SRC / f"{name}.mp4"
    if not p.exists():
        args = [FF, "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc=size=640x360:rate=10:duration={seconds}"]
        if audio:
            args += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}", "-c:a", "aac"]
        args += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-g", "20", str(p)]
        subprocess.run(args, check=True)
    return p


async def fake_fetch(path, start, duration, dest):
    """MediaMTX's MP4 for a range: cam1 has audio, cam2 none, cam3 nothing recorded."""
    if path == "cam3":
        raise httpx.HTTPStatusError("no recording", request=httpx.Request("GET", "http://x"), response=httpx.Response(404))
    shutil.copyfile(_clip(path, int(duration), audio=path == "cam1"), dest)


mediamtx.fetch_clip = fake_fetch
for cid, name in (("cam1", "Front Gate"), ("cam2", "Yard: North"), ("cam3", "Dock")):
    db.execute("INSERT OR REPLACE INTO cameras (id, name, host, enabled) VALUES (?,?,?,1)", [cid, name, "10.0.0.1"])


def probe(p: Path) -> str:
    r = subprocess.run([str(Path(FF).with_name("ffprobe" + Path(FF).suffix)), "-v", "error", "-show_entries", "stream=codec_type,codec_name,height",
                        "-of", "csv=p=0", str(p)], capture_output=True, text=True)
    return r.stdout


def run(coro):
    return asyncio.run(coro)


async def _wait(job):
    for _ in range(600):
        if job.state not in ("queued", "running"):
            return job
        await asyncio.sleep(0.1)
    raise AssertionError("export did not finish")


def test_original_with_audio_grid_report_checksums_zip_and_locks():
    eid = db.create_event(camera_id="cam1", track_id="t1", camera_class="person", camera_conf=0.9, start_ts=T0 + 3, end_ts=T0 + 6,
                          path=[[T0 + 3, 0.1, 0.1, 0.2, 0.5, 0.9]], status="verified")
    db.update_event(eid, synopsis="A man in a black jacket walks to the gate.", priority="medium",
                    policy={"kind": "ppe", "text": "No hard hat in PPE zone 1"})

    async def go():
        job = exports.start([{"id": "cam1", "name": "Front Gate"}, {"id": "cam2", "name": "Yard: North"},
                             {"id": "cam3", "name": "Dock"}], T0, T0 + 8, encoder="libx264", quality="original", audio=True,
                            grid=True, lock=True, by="shawn@example.com", event_id=eid, site="World HQ")
        return await _wait(job)

    job = run(go())
    assert job.state == "done", job.error
    d = exports.job_dir(job.id)
    names = [f["name"] for f in job.files]
    assert names[0].startswith("Front-Gate_") and names[1].startswith("Yard-North_") and any(n.startswith("grid_") for n in names)
    assert job.missing == [{"id": "cam3", "name": "Dock", "why": "no recording in this range"}]
    v1 = probe(d / names[0])
    assert "h264" in v1 and "aac" in v1                      # the video as recorded (copied), audio as AAC
    assert "audio" not in probe(d / names[1]) and "aac" not in probe(d / names[1])
    grid = next(n for n in names if n.startswith("grid_"))
    assert "1080" in probe(d / grid)
    report = (d / "report.html").read_text(encoding="utf-8")
    assert "A man in a black jacket" in report and "No hard hat in PPE zone 1" in report and "Dock: no recording" in report
    assert "shawn@example.com" in report and "World HQ" in report
    sums = dict(line.split("  ")[::-1] for line in (d / "checksums.txt").read_text().splitlines())
    for n in names:
        assert sums[n] == hashlib.sha256((d / n).read_bytes()).hexdigest()
    with zipfile.ZipFile(d / job.zip_name) as z:
        assert set(z.namelist()) == set(names) | {"checksums.txt"}
    locks = db.all("SELECT camera_id, start_ts, end_ts, note FROM locks ORDER BY camera_id")
    assert [(l["camera_id"], l["start_ts"], l["end_ts"]) for l in locks] == [("cam1", T0, T0 + 8), ("cam2", T0, T0 + 8)]
    assert locks[0]["note"] == "Exported by shawn@example.com"


def test_sd_without_audio_or_lock():
    db.execute("DELETE FROM locks")

    async def go():
        return await _wait(exports.start([{"id": "cam1", "name": "Front Gate"}], T0, T0 + 5, encoder="libx264", quality="sd",
                                         audio=False, grid=False, tiles=True, lock=False, by="local"))

    job = run(go())
    assert job.state == "done", job.error
    p = exports.job_dir(job.id) / job.files[0]["name"]
    assert "h264" in probe(p) and "aac" not in probe(p)
    assert not db.all("SELECT * FROM locks")
    # the hub's grid tile: listed (fetched with /files/), 360 high, but not in the zip or the checksums
    tile = next(f for f in job.files if f["kind"] == "tile")
    assert tile["name"] == "tile_cam1.mp4" and "360" in probe(exports.job_dir(job.id) / tile["name"])
    with zipfile.ZipFile(exports.job_dir(job.id) / job.zip_name) as z:
        assert "tile_cam1.mp4" not in z.namelist()
    assert "tile_cam1" not in (exports.job_dir(job.id) / "checksums.txt").read_text()


def test_nothing_recorded_fails_with_a_reason():
    async def go():
        return await _wait(exports.start([{"id": "cam3", "name": "Dock"}], T0, T0 + 5, encoder="libx264", by="local"))
    job = run(go())
    assert job.state == "failed" and "none of the cameras" in job.error


def test_grid_layout_and_names():
    assert [exports.grid_layout(n) for n in (1, 2, 3, 4, 5, 7, 9, 10, 16)] == [(1, 1), (2, 1), (2, 2), (2, 2), (3, 2), (3, 3), (3, 3), (4, 3), (4, 4)]
    assert exports.safe_name("Yard: North / gate 2") == "Yard-North-gate-2"
    fc, last = exports.grid_filter(["A", "B: it's"], T0, None)
    assert "xstack=inputs=2:layout=0_0|960_0" in fc and last == "g"


def test_api_limits_and_download():
    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url="http://localhost:8080") as c:
            api._encoder[:] = ["libx264"]
            assert (await c.post("/api/exports", json={"cameras": ["cam1"], "start": T0 + 10, "end": T0})).status_code == 422
            assert (await c.post("/api/exports", json={"cameras": ["cam1"], "start": T0, "end": T0 + 3601})).status_code == 422
            r = await c.post("/api/exports", json={"cameras": ["cam1"], "start": T0, "end": T0 + 1900, "grid": True})
            assert r.status_code == 422 and "grid" in r.text
            assert (await c.post("/api/exports", json={"cameras": ["nope"], "start": T0, "end": T0 + 5})).status_code == 422
            r = await c.post("/api/exports", json={"cameras": ["cam2"], "start": T0, "end": T0 + 4, "lock": False})
            assert r.status_code == 200, r.text
            jid = r.json()["id"]
            for _ in range(300):
                st = (await c.get(f"/api/exports/{jid}")).json()
                if st["state"] not in ("queued", "running"):
                    break
                await asyncio.sleep(0.1)
            assert st["state"] == "done", st
            d = await c.get(f"/api/exports/{jid}/download")
            assert d.status_code == 200 and d.headers["content-type"] == "application/zip"
            assert "attachment" in d.headers["content-disposition"] and st["zip_name"] in d.headers["content-disposition"]
            f = await c.get(f"/api/exports/{jid}/files/{st['files'][0]['name']}")
            assert f.status_code == 200 and hashlib.sha256(f.content).hexdigest() == st["files"][0]["sha256"]
            assert (await c.get(f"/api/exports/{jid}/files/job.json")).status_code == 404     # only the export's files
            assert (await c.get("/api/exports/..%2F..%2Fx")).status_code == 404
            assert any(j["id"] == jid for j in (await c.get("/api/exports")).json())
    run(go())


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
    print("all passed")
