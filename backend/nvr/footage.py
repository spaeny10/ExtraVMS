"""Search all recorded footage: an image-text index of the 24/7 recording (not just events).

Indexer: walks each camera's fMP4 segments ~1 minute behind live, decodes keyframes only (one frame per
SAMPLE_S), skips frames where nothing changed (but keeps one per KEEP_S), and stores CLIP embeddings of the
whole frame plus four overlapping tiles, so small or distant things can still be found.
The index lives next to the recordings (separate SQLite file) and is pruned as retention deletes footage;
AI-kept footage stays searchable.

Search: text -> CLIP text embedding -> nearest frames (camera/time filtered inside the vector search) ->
hits grouped into "moments" (same camera, within MOMENT_GAP_S).
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import struct
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import av
import cv2
import numpy as np
import sqlite_vec

from . import frames as framecache
from .clip import DIM
from .config import settings
from .db import db

log = logging.getLogger("nvr.footage")

SAMPLE_S = 5.0          # at most one indexed frame per 5 s
KEEP_S = 60.0           # ...and at least one per minute even if nothing moves
MOTION_MIN = 12.0       # change in the most-changed 8x8 block of a 128x72 thumbnail (0-255) that counts as motion
LIVE_LAG_S = 60.0       # stay this far behind live (parts are still being written)
CHUNK_S = 120.0         # seconds of video per indexing step, per camera
DECODE_WIDTH = 1280
MOMENT_GAP_S = 20.0
# tile 0 = whole frame; 1-4 = overlapping quadrants (x0, y0, x1, y1), normalised
TILES = [(0.0, 0.0, 1.0, 1.0), (0.0, 0.0, 0.6, 0.6), (0.4, 0.0, 1.0, 0.6), (0.0, 0.4, 0.6, 1.0), (0.4, 0.4, 1.0, 1.0)]

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS frames (id INTEGER PRIMARY KEY, camera_id TEXT NOT NULL, ts REAL NOT NULL, tile INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS frames_cam_ts ON frames(camera_id, ts);
CREATE TABLE IF NOT EXISTS cursors (camera_id TEXT PRIMARY KEY, ts REAL NOT NULL);
CREATE VIRTUAL TABLE IF NOT EXISTS frame_vec USING vec0(
    camera_id text partition key, ts float, tile integer, embedding float[{DIM}] distance_metric=cosine);
"""


class Index:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or settings.recordings_dir.parent / "index" / "footage.db"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self.conn.enable_load_extension(True)
        sqlite_vec.load(self.conn)
        self.conn.enable_load_extension(False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self.lock = threading.RLock()

    def q(self, sql: str, params=()) -> list[tuple]:
        with self.lock:
            return self.conn.execute(sql, params).fetchall()

    def cursor(self, camera_id: str) -> float | None:
        r = self.q("SELECT ts FROM cursors WHERE camera_id=?", [camera_id])
        return r[0][0] if r else None

    def set_cursor(self, camera_id: str, ts: float) -> None:
        self.q("INSERT INTO cursors (camera_id, ts) VALUES (?, ?) ON CONFLICT(camera_id) DO UPDATE SET ts=excluded.ts",
               [camera_id, ts])

    def add(self, camera_id: str, items: list[tuple[float, int, np.ndarray]], cursor: float) -> None:
        """items: (ts, tile, embedding). Written in one transaction together with the new cursor."""
        with self.lock:
            c = self.conn
            c.execute("BEGIN")
            try:
                for ts, tile, vec in items:
                    fid = c.execute("INSERT INTO frames (camera_id, ts, tile) VALUES (?,?,?)", [camera_id, ts, tile]).lastrowid
                    c.execute("INSERT INTO frame_vec (rowid, camera_id, ts, tile, embedding) VALUES (?,?,?,?,?)",
                              [fid, camera_id, ts, tile, struct.pack(f"{DIM}f", *vec.tolist())])
                c.execute("INSERT INTO cursors (camera_id, ts) VALUES (?, ?) ON CONFLICT(camera_id) DO UPDATE SET ts=excluded.ts",
                          [camera_id, cursor])
                c.execute("COMMIT")
            except Exception:
                c.execute("ROLLBACK")
                raise

    def delete_ids(self, ids: list[int]) -> None:
        with self.lock:
            c = self.conn
            c.execute("BEGIN")
            for i in range(0, len(ids), 500):
                chunk = ids[i:i + 500]
                marks = ",".join("?" * len(chunk))
                c.execute(f"DELETE FROM frame_vec WHERE rowid IN ({marks})", chunk)
                c.execute(f"DELETE FROM frames WHERE id IN ({marks})", chunk)
            c.execute("COMMIT")

    def search(self, vec: np.ndarray, camera_ids: list[str], since: float | None, until: float | None,
               k: int = 400) -> list[tuple[str, float, int, float]]:
        """-> [(camera_id, ts, tile, similarity)] best first."""
        blob = struct.pack(f"{DIM}f", *vec.tolist())
        out = []
        for cam in camera_ids:
            sql = "SELECT ts, tile, distance FROM frame_vec WHERE embedding MATCH ? AND k = ? AND camera_id = ?"
            params: list = [blob, k, cam]
            if since is not None:
                sql += " AND ts >= ?"; params.append(since)
            if until is not None:
                sql += " AND ts <= ?"; params.append(until)
            out += [(cam, ts, tile, 1 - dist) for ts, tile, dist in self.q(sql + " ORDER BY distance", params)]
        return sorted(out, key=lambda r: -r[3])

    def stats(self) -> dict:
        rows = self.q("SELECT camera_id, COUNT(*), MIN(ts), MAX(ts) FROM frames WHERE tile=0 GROUP BY camera_id")
        size = sum(p.stat().st_size for p in self.path.parent.glob(self.path.name + "*") if p.exists())
        return {"db_mb": round(size / 1e6, 1),
                "cameras": {cam: {"frames": n, "oldest": lo, "newest": hi, "cursor": self.cursor(cam)} for cam, n, lo, hi in rows}}


# ---------------------------------------------------------------- decoding

def _thumb(img: np.ndarray) -> np.ndarray:
    g = cv2.cvtColor(cv2.resize(img, (128, 72), interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2GRAY)
    return cv2.GaussianBlur(g, (3, 3), 0).astype(np.float32)


def _change(a: np.ndarray, b: np.ndarray) -> float:
    """Largest mean difference over 8x8 blocks: a small, distant person moves one block a lot, while a
    whole-frame average barely changes (measured on this site: people 17-150, empty scene median ~7)."""
    return float(np.abs(a - b).reshape(9, 8, 16, 8).mean(axis=(1, 3)).max())


def tile_crops(img: np.ndarray) -> list[np.ndarray]:
    h, w = img.shape[:2]
    return [img[int(y0 * h):int(y1 * h), int(x0 * w):int(x1 * w)] for (x0, y0, x1, y1) in TILES]


def tile_jpeg(camera_id: str, ts: float, tile: int) -> bytes | None:
    """JPEG of one tile of the recorded frame at ts (what a footage match matched), for Qwen to check."""
    r = framecache.preview_jpeg(camera_id, ts, 1280, False)
    if not r:
        return None
    img = cv2.imdecode(np.frombuffer(r[0], np.uint8), cv2.IMREAD_COLOR)
    return cv2.imencode(".jpg", tile_crops(img)[tile], [cv2.IMWRITE_JPEG_QUALITY, 85])[1].tobytes()


def decode_keyframes(path: Path, seg_start: float, t0: float, t1: float) -> tuple[list[tuple[float, np.ndarray]], float | None, bool]:
    """Keyframes in [t0, t1), at most one per SAMPLE_S.
    Returns (frames, last packet time seen, ended) where ended = the file ran out before t1."""
    out: list[tuple[float, np.ndarray]] = []
    last_seen, ended = None, True
    with av.open(str(path)) as c:
        s = c.streams.video[0]
        tb = float(s.time_base)
        base = s.start_time or 0
        c.seek(max(base, base + int((t0 - seg_start) / tb)), stream=s, backward=True, any_frame=False)
        next_t = t0
        for pkt in c.demux(s):
            if pkt.pts is None:
                continue
            ts = seg_start + (pkt.pts - base) * tb
            last_seen = ts
            if ts >= t1:
                ended = False
                break
            if not pkt.is_keyframe or ts < next_t:
                continue
            for fr in pkt.decode():
                h = int(round(fr.height * DECODE_WIDTH / fr.width / 2)) * 2
                out.append((ts, fr.reformat(width=DECODE_WIDTH, height=h, format="bgr24").to_ndarray()))
                next_t = ts + SAMPLE_S
                break
    return out, last_seen, ended


# ---------------------------------------------------------------- indexer

class Indexer:
    def __init__(self, pipeline, index: Index | None = None) -> None:
        self.p = pipeline
        self.index = index or Index()
        self.clip = None
        self.decode = ThreadPoolExecutor(max_workers=1, thread_name_prefix="footage")
        self.last_thumb: dict[str, tuple[float, np.ndarray]] = {}
        self.last_prune = 0.0
        self.rate = 0.0  # frames indexed per second of wall time, for the status card

    async def ensure_clip(self):
        if self.clip is None:
            self.clip = await self.p.get_clip() if hasattr(self.p, "get_clip") else await self._own_clip()
        return self.clip

    async def _own_clip(self):  # tests with a bare executor
        from .clip import Clip
        return await asyncio.get_running_loop().run_in_executor(self.p.gpu, Clip)

    async def embed_text(self, text: str) -> np.ndarray:
        clip = await self.ensure_clip()
        return await asyncio.get_running_loop().run_in_executor(self.p.gpu, clip.embed_text, text)

    async def step(self, camera_id: str) -> int:
        """Index up to CHUNK_S of one camera's footage. Returns frames indexed (-1 if caught up)."""
        segs = framecache._segments(camera_id)
        if not segs:
            return -1
        limit_live = time.time() - LIVE_LAG_S
        cur = self.index.cursor(camera_id)
        if cur is None or cur < segs[0][0]:
            cur = segs[0][0]
        # the segment containing the cursor (or the next one after a gap)
        i = max((j for j, (st, _) in enumerate(segs) if st <= cur), default=0)
        seg_start, path = segs[i]
        next_start = segs[i + 1][0] if i + 1 < len(segs) else None
        t1 = min(cur + CHUNK_S, next_start or limit_live, limit_live)
        if t1 <= cur + 1:
            if next_start is not None and next_start <= limit_live:  # <1 s left in this segment: move to the next
                await asyncio.to_thread(self.index.set_cursor, camera_id, next_start)
                return 0
            return -1
        loop = asyncio.get_running_loop()
        try:
            samples, last_seen, ended = await loop.run_in_executor(self.decode, decode_keyframes, path, seg_start, cur, t1)
        except (av.error.FFmpegError, OSError) as e:  # damaged or just-deleted segment: skip it
            log.warning("footage index: skipping %s: %s", path.name, e)
            samples, last_seen, ended = [], None, True
        if ended:
            if next_start:      # finished segment ran out (or a gap follows): continue at the next one
                t1 = next_start
            elif last_seen is not None:  # live segment: don't skip data that's still being flushed
                t1 = min(t1, last_seen + 0.001)
        keep: list[tuple[float, np.ndarray]] = []
        spans = [(r["start_ts"] - 2, (r["end_ts"] or r["start_ts"]) + 2) for r in db.all(
            "SELECT start_ts, end_ts FROM events WHERE camera_id=? AND start_ts <= ? AND COALESCE(end_ts, start_ts) >= ?",
            [camera_id, t1, cur])]
        for ts, img in samples:
            th = _thumb(img)
            prev = self.last_thumb.get(camera_id)
            in_event = any(a <= ts <= b for a, b in spans)  # always index what the camera flagged
            if prev is None or in_event or ts - prev[0] >= KEEP_S or ts < prev[0] or _change(th, prev[1]) >= MOTION_MIN:
                keep.append((ts, img))
                self.last_thumb[camera_id] = (ts, th)
        items: list[tuple[float, int, np.ndarray]] = []
        if keep:
            clip = await self.ensure_clip()
            for b in range(0, len(keep), 8):  # small GPU batches so YOLO verification isn't held up
                batch = keep[b:b + 8]
                crops = [c for _, img in batch for c in tile_crops(img)]
                vecs = await loop.run_in_executor(self.p.gpu, clip.embed_images, crops)
                for n, (ts, _) in enumerate(batch):
                    items += [(ts, t, vecs[n * len(TILES) + t]) for t in range(len(TILES))]
        await asyncio.to_thread(self.index.add, camera_id, items, t1)
        return len(keep)

    def prune(self) -> int:
        """Drop index rows for footage that no longer exists (retention deleted or trimmed it)."""
        removed = 0
        for (camera_id,) in self.index.q("SELECT DISTINCT camera_id FROM frames"):
            d = settings.recordings_dir / camera_id
            segs = sorted((st, f) for f in d.glob("*.mp4") if (st := framecache.segment_start(f))) if d.exists() else []
            if not segs:
                ids = [r[0] for r in self.index.q("SELECT id FROM frames WHERE camera_id=?", [camera_id])]
            else:
                spans = []
                for j, (st, f) in enumerate(segs):
                    nxt = segs[j + 1][0] if j + 1 < len(segs) else None
                    try:
                        end = f.stat().st_mtime
                    except OSError:
                        continue
                    spans.append((st, min(end, nxt) if nxt else max(end, time.time())))
                horizon = time.time() - 2 * 86400  # footage newer than this is never deleted by retention
                rows = self.index.q("SELECT id, ts FROM frames WHERE camera_id=? AND ts < ? ORDER BY ts", [camera_id, horizon])
                ids, k = [], 0
                for fid, ts in rows:
                    while k < len(spans) and spans[k][1] + 1 < ts:
                        k += 1
                    if k >= len(spans) or not (spans[k][0] - 1 <= ts <= spans[k][1] + 1):
                        ids.append(fid)
            if ids:
                self.index.delete_ids(ids)
                removed += len(ids)
        if removed:
            log.info("footage index: pruned %d vectors for deleted footage", removed)
        return removed

    async def run(self) -> None:
        while self.p.verifier is None:  # let YOLO load first
            await asyncio.sleep(2)
        await asyncio.sleep(5)
        while True:
            t0, did = time.time(), 0
            try:
                for cam in [c["id"] for c in db.cameras(enabled_only=True)]:
                    n = await self.step(cam)
                    did += max(0, n) + (1 if n >= 0 else 0)
                if time.time() - self.last_prune > 600:
                    self.last_prune = time.time()
                    await asyncio.to_thread(self.prune)
            except Exception:
                log.exception("footage indexing failed")
                await asyncio.sleep(30)
            if not did:
                await asyncio.sleep(15)  # caught up: check again shortly
            else:
                self.rate = did / max(0.01, time.time() - t0)
                await asyncio.sleep(0.05)

    def status(self) -> dict:
        st = self.index.stats()
        now = time.time()
        for cam in [c["id"] for c in db.cameras(enabled_only=True)]:
            c = st["cameras"].setdefault(cam, {"frames": 0, "oldest": None, "newest": None, "cursor": self.index.cursor(cam)})
            c["backlog_s"] = max(0.0, now - LIVE_LAG_S - (c["cursor"] or now))
        st["model_loaded"] = self.clip is not None
        return st


def moments(hits: list[tuple[str, float, int, float]], limit: int = 30) -> list[dict]:
    """Group hits (same camera within MOMENT_GAP_S) into moments, scored by their best hit."""
    by_cam: dict[str, list] = {}
    for h in hits:
        by_cam.setdefault(h[0], []).append(h)
    out = []
    for cam, hs in by_cam.items():
        hs.sort(key=lambda h: h[1])
        cur = None
        for _, ts, tile, sim in hs:
            if cur and ts - cur["end"] <= MOMENT_GAP_S:
                cur["end"] = ts
                cur["hits"] += 1
                if sim > cur["score"]:
                    cur.update(ts=ts, tile=tile, score=sim)
            else:
                cur = {"camera_id": cam, "start": ts, "end": ts, "ts": ts, "tile": tile, "score": sim, "hits": 1}
                out.append(cur)
    out.sort(key=lambda m: -m["score"])
    for m in out:
        m["score"] = round(m["score"], 4)
        m["box"] = TILES[m["tile"]]
    return out[:limit]
