"""Site exports across servers (hub/hub/exports.py): each server's part asked for on its own clock, followed, its files
fetched through the tunnel, the grid built from the servers' tiles, one report, checksums and one zip; an offline
server's cameras listed as not included. The servers are stand-ins answering like the site's /api/exports."""
import asyncio
import hashlib
import json
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

import pytest

from hub import exports
from hub.config import settings

FF = shutil.which("ffmpeg") or r"C:\ffmpeg\bin\ffmpeg.exe"
SRC = Path(tempfile.mkdtemp(prefix="hub-exports-src-"))


def _clip(name: str, size: str) -> bytes:
    p = SRC / f"{name}.mp4"
    if not p.exists():
        subprocess.run([FF, "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc=size={size}:rate=10:duration=4",
                        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(p)], check=True)
    return p.read_bytes()


class _Stream:
    def __init__(self, sid: int, status: int, body: bytes):
        self.id, self.status, self.aborted = sid, status, None
        self.head = asyncio.Event()
        self.head.set()
        self._chunks = [body[i:i + 65536] for i in range(0, len(body), 65536)] + [None]

    async def read(self):
        return self._chunks.pop(0)


class FakeServer:
    """What the hub sees of a site through its tunnel: call() and request() against the site's export API."""

    def __init__(self, name: str, cams: list[str], skew: float):
        self.name, self.cams, self.skew = name, cams, skew
        self.asked: list[dict] = []
        self.polls = 0
        self.streams: dict = {}
        self.files = {f"{c}.mp4": _clip(f"{name}-{c}", "640x360") for c in cams}
        self.files.update({f"tile_{c}.mp4": _clip(f"{name}-{c}-tile", "320x180") for c in cams})
        self.files["report.html"] = f"<html>{name}</html>".encode()

    def job(self) -> dict:
        done = self.polls >= 2
        return {"id": "abcdef123456", "state": "done" if done else "running", "progress": 1.0 if done else 0.5, "utc_offset_s": -21600,
                "files": [{"name": f"{c}.mp4", "kind": "video", "camera": c} for c in self.cams]
                + [{"name": f"tile_{c}.mp4", "kind": "tile", "camera": c} for c in self.cams]
                + [{"name": "report.html", "kind": "report"}],
                "missing": []}

    async def call(self, method, path, query="", headers=None, body=None, timeout=15.0):
        assert headers["x-hub-user"] == "shawn@example.com" and headers["x-hub-role"] == "operator"
        if method == "POST" and path == "/api/exports":
            self.asked.append(json.loads(body))
            return 200, json.dumps(self.job()).encode()
        if method == "GET" and path == "/api/exports/abcdef123456":
            self.polls += 1
            return 200, json.dumps(self.job()).encode()
        return 404, b"no"

    async def request(self, method, path, query, headers, body):
        name = path.rsplit("/", 1)[-1]
        return _Stream(len(self.streams) + 1, 200 if name in self.files else 404, self.files.get(name, b""))

    async def send(self, frame):
        pass

    async def abort(self, s, reason):
        pass

    def finish(self, s):
        pass


@pytest.mark.skipif(not Path(FF).exists() and not shutil.which("ffmpeg"), reason="needs ffmpeg")
def test_site_export_across_servers(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "exports_dir", tmp_path)
    monkeypatch.setattr(exports, "POLL_S", 0.01)
    a, b = FakeServer("TSS-DT", ["cam2", "cam4"], 0.0), FakeServer("T-304", ["cam1"], 2.5)
    conns = {"s_a": a, "s_b": b, "s_off": None}
    monkeypatch.setattr(exports.registry, "get", lambda sid: conns.get(sid))
    part = lambda sid, name, cams, off: {"server": sid, "server_name": name, "cameras": cams, "role": "operator", "offset": off,   # noqa: E731
                                         "state": "queued", "names": {c: f"{c} name" for c in cams}}
    job = exports.HubExport(id="0123456789ab", user_id="u1", by="shawn@example.com", location_id="l1", site="World HQ",
                            parts=[part("s_a", "TSS-DT", ["cam2", "cam4"], 0.0), part("s_b", "T-304", ["cam1"], 2.5),
                                   part("s_off", "Yard", ["cam9"], 0.0)],
                            start=1000.0, end=1004.0, grid=True)
    asyncio.run(exports.run(job))
    assert job.state == "done", job.error
    # each server asked on its own clock, with tiles for the grid; never its own grid
    assert a.asked[0]["start"] == 1000.0 and b.asked[0]["start"] == 1002.5 and b.asked[0]["end"] == 1006.5
    assert a.asked[0]["tiles"] and not a.asked[0]["grid"] and a.asked[0]["cameras"] == ["cam2", "cam4"]
    names = [f["name"] for f in job.files]
    assert {"TSS-DT_cam2.mp4", "TSS-DT_cam4.mp4", "T-304_cam1.mp4", "report_TSS-DT.html", "report_T-304.html", "report.html"} <= set(names)
    assert not any("tile" in n for n in names)                    # tiles feed the grid, they aren't handed over
    grid = next(n for n in names if n.startswith("grid_"))
    d = exports.job_dir(job.id)
    out = subprocess.run([str(Path(FF).with_name("ffprobe" + Path(FF).suffix)), "-v", "error", "-show_entries", "stream=width,height",
                          "-of", "csv=p=0", str(d / grid)], capture_output=True, text=True).stdout
    assert "1920,1080" in out
    assert job.missing == [{"id": "cam9", "name": "cam9", "why": "Yard is offline"}]
    sums = dict(line.split("  ")[::-1] for line in (d / "checksums.txt").read_text().splitlines())
    assert all(sums[n] == hashlib.sha256((d / n).read_bytes()).hexdigest() for n in names)
    with zipfile.ZipFile(d / job.zip_name) as z:
        assert set(z.namelist()) == set(names) | {"checksums.txt"}
    report = (d / "report.html").read_text()
    assert "World HQ" in report and "report_T-304.html" in report and "Yard is offline" in report
    assert exports._get(job.id).state == "done"                     # read back from disk too


def test_all_servers_offline_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "exports_dir", tmp_path)
    monkeypatch.setattr(exports.registry, "get", lambda sid: None)
    job = exports.HubExport(id="aaaaaaaaaaaa", user_id="u1", by="x", location_id="l1", site="S",
                            parts=[{"server": "s1", "server_name": "One", "cameras": ["cam1"], "role": "operator", "offset": 0, "state": "queued", "names": {}}],
                            start=0, end=10)
    asyncio.run(exports.run(job))
    assert job.state == "failed" and "none of the servers" in job.error


def test_grid_args_layout():
    args = exports.grid_args("ffmpeg", [Path("a.mp4"), Path("b.mp4"), Path("c.mp4")], ["A", "B", "C"], 0.0, Path("g.mp4"))
    fc = args[args.index("-filter_complex") + 1]
    assert "xstack=inputs=3:layout=0_0|960_0|0_540" in fc
