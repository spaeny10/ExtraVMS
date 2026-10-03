"""SQLite storage (events, cameras, ONVIF rule events, FTS + vector search)."""
from __future__ import annotations

import json
import sqlite3
import struct
import threading
import time
from typing import Any, Iterable

import sqlite_vec

from .config import settings

EMBED_DIM = 768  # nomic-embed-text
REID_DIM = 512   # OSNet person re-ID
CLIP_DIM = 512   # OpenCLIP ViT-B-16 (footage index)
COLOR_DIM = 48   # HSV colour histogram (identities.color_hist)
VEHICLE_DIM = CLIP_DIM + COLOR_DIM   # vehicle fingerprint: weighted CLIP of the tight crop + colour

# Vector search relevance: nomic embeddings are unit length, so L2 distance ~0.7 is a strong match and
# ~1.0 is unrelated (measured: "Person working" -> person synopses 0.71-0.85, label-only vehicle docs 1.01).
VEC_MAX_DIST = 0.95
VEC_MAX_GAP = 0.2
STOPWORDS = {"a", "an", "the", "of", "in", "on", "at", "to", "and", "or", "is", "are", "was", "with", "near",
             "by", "for", "from", "any", "some", "show", "me", "find", "all", "who", "that", "this"}

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS cameras (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    host         TEXT NOT NULL,
    onvif_port   INTEGER NOT NULL DEFAULT 80,
    rtsp_port    INTEGER NOT NULL DEFAULT 554,
    username     TEXT NOT NULL DEFAULT 'admin',
    password     TEXT NOT NULL DEFAULT '',
    main_path    TEXT NOT NULL DEFAULT '/main',
    sub_path     TEXT NOT NULL DEFAULT '/sub',
    enabled      INTEGER NOT NULL DEFAULT 1,
    zones        TEXT NOT NULL DEFAULT '[]',   -- [{{name, points:[[x,y],...]}}] normalized 0..1
    retention_days INTEGER
);

CREATE TABLE IF NOT EXISTS events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    camera_id     TEXT NOT NULL REFERENCES cameras(id),
    track_id      TEXT NOT NULL,
    camera_class  TEXT NOT NULL,
    camera_conf   REAL,
    start_ts      REAL NOT NULL,
    end_ts        REAL,
    path          TEXT NOT NULL DEFAULT '[]', -- [[ts, l, t, r, b, conf], ...] normalized boxes
    rules         TEXT NOT NULL DEFAULT '[]', -- ONVIF rule events that fired during the track
    status        TEXT NOT NULL DEFAULT 'open', -- open|pending|verified|rejected|error
    yolo_class    TEXT,
    yolo_conf     REAL,
    yolo_hits     INTEGER,
    detections    TEXT,                       -- per sampled frame YOLO boxes
    snapshot      TEXT,
    clip          TEXT,
    synopsis      TEXT,
    synopsis_json TEXT,
    threat        TEXT,
    error         TEXT,
    created_at    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS events_cam_ts ON events(camera_id, start_ts);
CREATE INDEX IF NOT EXISTS events_status ON events(status);

CREATE TABLE IF NOT EXISTS rule_events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    camera_id TEXT NOT NULL,
    ts        REAL NOT NULL,
    topic     TEXT NOT NULL,
    rule      TEXT,
    state     TEXT,
    data      TEXT
);
CREATE INDEX IF NOT EXISTS rule_events_cam_ts ON rule_events(camera_id, ts);

CREATE TABLE IF NOT EXISTS chat_messages (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id  INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    role      TEXT NOT NULL,              -- user | assistant
    content   TEXT NOT NULL,
    frames    TEXT NOT NULL DEFAULT '[]', -- [{{file, t}}] frames the answer was based on
    at        REAL,                       -- clip time the question was about (seconds)
    saved     INTEGER NOT NULL DEFAULT 0, -- saved as an event note
    ts        REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS chat_event ON chat_messages(event_id, id);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL             -- JSON
);

CREATE TABLE IF NOT EXISTS locks (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    camera_id  TEXT NOT NULL,
    start_ts   REAL NOT NULL,
    end_ts     REAL NOT NULL,
    event_id   INTEGER,             -- set when the lock came from an event
    note       TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS locks_cam_ts ON locks(camera_id, start_ts);

-- Footage past the continuous window that retention decided to keep (trimmed or whole segments).
CREATE TABLE IF NOT EXISTS kept_footage (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    camera_id  TEXT NOT NULL,
    start_ts   REAL NOT NULL,
    end_ts     REAL NOT NULL,
    file       TEXT NOT NULL UNIQUE,
    reasons    TEXT NOT NULL DEFAULT '[]',
    score      REAL NOT NULL DEFAULT 0,
    bytes      INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS kept_cam_ts ON kept_footage(camera_id, start_ts);

-- Named Timeline layouts: which cameras are shown / soloed, shared across browsers.
CREATE TABLE IF NOT EXISTS layouts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL UNIQUE,
    config     TEXT NOT NULL,       -- JSON {{visible: [camera ids], solo: camera id | null, order: [camera ids]}}
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
-- Home dashboards (frontend/src/dashboard): a widget grid, shared by everyone on this site.
CREATE TABLE IF NOT EXISTS dashboards (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL UNIQUE,
    config     TEXT NOT NULL,       -- JSON {{version, cols, rowH, widgets: [...]}}
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

-- Cross-camera journeys: which cameras neighbour each other, and how long the walk takes.
CREATE TABLE IF NOT EXISTS camera_links (
    cam_a   TEXT NOT NULL,
    cam_b   TEXT NOT NULL,
    min_s   REAL NOT NULL,          -- may be negative: overlapping fields of view
    max_s   REAL NOT NULL,
    one_way INTEGER NOT NULL DEFAULT 0,   -- 1: only a -> b
    PRIMARY KEY (cam_a, cam_b)
);

-- Candidate / confirmed "same person" links between two events on different cameras.
CREATE TABLE IF NOT EXISTS event_links (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    a          INTEGER NOT NULL,    -- earlier event
    b          INTEGER NOT NULL,    -- later event
    gap_s      REAL NOT NULL,
    sim        REAL NOT NULL,       -- re-ID cosine similarity
    status     TEXT NOT NULL,       -- confirmed | rejected | user_rejected
    confidence TEXT,
    reason     TEXT,
    created_at REAL NOT NULL,
    UNIQUE (a, b)
);
CREATE INDEX IF NOT EXISTS event_links_b ON event_links(b);

CREATE TABLE IF NOT EXISTS journeys (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    first_ts   REAL NOT NULL,
    last_ts    REAL NOT NULL,
    cameras    TEXT NOT NULL,       -- JSON [camera ids in order of appearance]
    synopsis   TEXT,
    dirty      INTEGER NOT NULL DEFAULT 1,
    updated_at REAL NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS reid_vec USING vec0(embedding float[{REID_DIM}]);
CREATE VIRTUAL TABLE IF NOT EXISTS vehicle_vec USING vec0(embedding float[{VEHICLE_DIM}]);  -- fingerprint of a verified vehicle (identities.py)

-- Named people and vehicles (identities.py): a centroid fingerprint the operator has put a name to.
CREATE TABLE IF NOT EXISTS identities (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    kind       TEXT NOT NULL,              -- person | vehicle
    embedding  BLOB NOT NULL,
    notes      TEXT NOT NULL DEFAULT '',
    sightings  INTEGER NOT NULL DEFAULT 0, -- how many sightings the centroid averages
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

-- One row per look (outfit) of a named identity; identities.embedding stays the look with most sightings.
CREATE TABLE IF NOT EXISTS identity_looks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    identity_id INTEGER NOT NULL REFERENCES identities(id) ON DELETE CASCADE,
    embedding   BLOB NOT NULL,
    sightings   INTEGER NOT NULL DEFAULT 0,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS identity_looks_ident ON identity_looks(identity_id);

-- Where a PTZ camera pointed over time: one row per change (ptz.py), for "was the camera away?" answers.
CREATE TABLE IF NOT EXISTS ptz_moves (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    camera_id TEXT NOT NULL,
    ts        REAL NOT NULL,
    at_home   INTEGER NOT NULL,
    preset    TEXT
);
CREATE INDEX IF NOT EXISTS ptz_moves_cam_ts ON ptz_moves(camera_id, ts);

-- Ask the NVR: site-wide conversations (assistant.py)
CREATE TABLE IF NOT EXISTS assistant_threads (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT NOT NULL,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS assistant_messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id   INTEGER NOT NULL REFERENCES assistant_threads(id) ON DELETE CASCADE,
    role        TEXT NOT NULL,              -- user | assistant
    content     TEXT NOT NULL,
    calls       TEXT,                       -- JSON: what was looked up, plus citation refs
    model       TEXT,
    ts          REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS assistant_messages_thread ON assistant_messages(thread_id, id);
-- Morning briefings (assistant.py)
CREATE TABLE IF NOT EXISTS briefings (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    period_start REAL NOT NULL,
    period_end   REAL NOT NULL,
    headline     TEXT NOT NULL,
    text         TEXT NOT NULL,             -- bullet lines, with [#id] citations
    stats        TEXT,                      -- JSON facts the briefing was written from, plus citation refs
    model        TEXT,
    created_at   REAL NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS events_fts USING fts5(synopsis, labels, content='', contentless_delete=1);
CREATE VIRTUAL TABLE IF NOT EXISTS events_vec USING vec0(embedding float[{EMBED_DIM}]);
"""

# Columns added after the first release: (table, column, definition)
MIGRATIONS = [
    ("cameras", "scene_notes", "TEXT NOT NULL DEFAULT ''"),
    ("cameras", "retention_policy", "TEXT"),       # JSON partial override of the site policy; NULL = inherit
    ("cameras", "synopsis_labels", "TEXT"),        # JSON ["person","vehicle"]: what Qwen describes here; NULL = site default
    ("cameras", "policies", "TEXT"),               # JSON list of site rules (policy.py)
    ("events", "policy", "TEXT"),                  # JSON {kind, text, priority}: the site rule this event breaks
    ("events", "cells", "TEXT"),                   # base64url 32x18 bitmap of grid cells the object's feet crossed (cells.py)
    ("cameras", "ptz_config", "TEXT"),             # JSON: home preset, return-home minutes, relay/input labels, preset positions (ptz.py)
    ("events", "ptz_preset", "TEXT"),              # PTZ camera turned away from home: preset name or "away"; NULL = at home / fixed camera
    ("events", "clip_start", "REAL"),
    ("events", "synopsis_original", "TEXT"),   # Qwen's JSON before the user corrected it
    ("events", "corrected_at", "REAL"),
    ("events", "feedback", "TEXT"),            # {rating, reasons, verdict, correct_class, note, at}
    ("events", "status_before_mask", "TEXT"),  # set while status='masked' (zone mask), restored on unmask
    ("events", "journey_id", "INTEGER"),        # cross-camera journey this event belongs to
    ("events", "anomaly", "REAL"),              # 0..1 how unusual for this camera (baseline.py)
    ("events", "anomaly_json", "TEXT"),         # {score, parts, reasons, learning}
    ("events", "priority", "TEXT"),
    ("journeys", "model", "TEXT"),              # which Qwen wrote the narrative (local 7B or remote)
    ("identities", "watch", "INTEGER NOT NULL DEFAULT 0"),  # on the watch list: matching sightings get priority
    ("identities", "watch_note", "TEXT NOT NULL DEFAULT ''"),
    ("events", "watched", "TEXT"),              # name of the watched identity this sighting matched
    ("events", "areas", "TEXT"),                # JSON [{name, from, to}]: named areas the object walked into             # none|low|medium|high: max(threat, unusualness), operator wins
    ("events", "migrated_from", "TEXT"),        # JSON {site, site_id, event_id, camera_id}: copied here by a fleet move (siteconfig.import_history)
]
JSON_FIELDS = ("path", "rules", "detections", "synopsis_json", "synopsis_original", "feedback", "anomaly_json", "areas", "policy", "migrated_from")

# ---- event filters shared by browse (/api/events), search and the Find summary strip
UNUSUAL_MIN = 0.75   # anomaly at/above this is "unusual" (frontend UNUSUAL_MIN)
PRIORITY_RANK = {"none": 0, "low": 1, "medium": 2, "high": 3}
PRIORITY_RANK_SQL = "(CASE priority WHEN 'high' THEN 3 WHEN 'medium' THEN 2 WHEN 'low' THEN 1 ELSE 0 END)"
LOCKED_SQL = "EXISTS(SELECT 1 FROM locks WHERE locks.event_id = events.id)"
JOURNEY_CAMS_SQL = ("(SELECT COUNT(DISTINCT je.value) FROM journeys, json_each(journeys.cameras) je "
                    "WHERE journeys.id = events.journey_id)")
# detections can be large and older rows may not be an object: only parse it when it is valid JSON
PPE_ZONE_SQL = "json_extract(CASE WHEN json_valid(detections) THEN detections END, '$.ppe.zone')"
FLAG_SQL = {
    "rule": "policy IS NOT NULL",
    "ppe": "json_extract(policy, '$.kind') = 'ppe'",
    "unusual": f"anomaly >= {UNUSUAL_MIN}",
    "watched": "watched IS NOT NULL",
    "multicam": f"(journey_id IS NOT NULL AND {JOURNEY_CAMS_SQL} > 1)",
    "locked": LOCKED_SQL,
    "corrected": "corrected_at IS NOT NULL",
    "false_alarm": "json_extract(feedback, '$.verdict') = 'false_alarm'",
}
# Find's "Attention" view: anything that needs a look (priority medium+, a broken rule, unusual, watched)
ATTENTION_SQL = f"({PRIORITY_RANK_SQL} >= 2 OR policy IS NOT NULL OR anomaly >= {UNUSUAL_MIN} OR watched IS NOT NULL)"


def event_filters(camera: str | None = None, label: str | None = None, threat: str | None = None,
                  status: str | None = None, since: float | None = None, until: float | None = None,
                  min_yolo: float = 0, keep_unverified: bool = True, priority: str | None = None,
                  flags: str | list[str] | None = None, place: str | None = None, ppe_zone: str | None = None,
                  attention: bool = False) -> tuple[list[str], list]:
    """WHERE clauses + params for the event filters; all optional and AND-combined.

    keep_unverified: with min_yolo, events still being tracked/verified (no YOLO score yet) stay in (browse).
    flags: names from FLAG_SQL (list or comma string); unknown names or priorities raise ValueError.
    """
    where, params = [], []
    if min_yolo and min_yolo > 0:
        where.append("(yolo_conf >= ? OR status IN ('open','pending'))" if keep_unverified else "yolo_conf >= ?")
        params.append(min_yolo)
    for col, val in (("camera_id", camera), ("camera_class", label), ("threat", threat)):
        if val:
            where.append(f"{col}=?"); params.append(val)
    if status:
        statuses = [x for x in status.split(",") if x]
        where.append(f"status IN ({','.join('?' * len(statuses))})"); params += statuses
    if since:
        where.append("start_ts>=?"); params.append(since)
    if until:
        where.append("start_ts<=?"); params.append(until)
    if priority and priority != "none":
        if priority not in PRIORITY_RANK:
            raise ValueError(f"unknown priority {priority!r}")
        where.append(f"{PRIORITY_RANK_SQL} >= ?"); params.append(PRIORITY_RANK[priority])
    names = flags.split(",") if isinstance(flags, str) else (flags or [])
    for f in dict.fromkeys(x.strip() for x in names if x and x.strip()):
        if f not in FLAG_SQL:
            raise ValueError(f"unknown flag {f!r} (known: {', '.join(FLAG_SQL)})")
        where.append(FLAG_SQL[f])
    if place:
        where.append("EXISTS (SELECT 1 FROM json_each(CASE WHEN json_valid(events.areas) THEN events.areas ELSE '[]' END) a "
                     "WHERE json_extract(a.value, '$.name') = ?)")
        params.append(place)
    if ppe_zone:
        where.append(f"(json_extract(policy, '$.kind') = 'ppe' AND {PPE_ZONE_SQL} = ?)"); params.append(ppe_zone)
    if attention:
        where.append(ATTENTION_SQL)
    return where, params


class Database:
    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.enable_load_extension(True)
        sqlite_vec.load(self.conn)
        self.conn.enable_load_extension(False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        # vehicle fingerprints changed width (CLIP only -> CLIP + colour): drop the old table, the backfill refills it
        old = self.conn.execute("SELECT sql FROM sqlite_master WHERE name='vehicle_vec'").fetchone()
        if old and f"float[{VEHICLE_DIM}]" not in old[0]:
            self.conn.execute("DROP TABLE vehicle_vec")
            self.conn.execute("DELETE FROM identities WHERE kind='vehicle' AND length(embedding) != ?", [VEHICLE_DIM * 4])
        self.conn.executescript(SCHEMA)
        # identities named before looks existed: their centroid becomes the first look
        self.conn.execute("INSERT INTO identity_looks (identity_id, embedding, sightings, created_at, updated_at) "
                          "SELECT id, embedding, sightings, created_at, updated_at FROM identities "
                          "WHERE id NOT IN (SELECT identity_id FROM identity_looks)")
        for table, col, definition in MIGRATIONS:
            cols = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            if col not in cols:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {definition}")
        self.lock = threading.RLock()

    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self.lock:
            return self.conn.execute(sql, tuple(params))

    def execute_insert(self, sql: str, params: Iterable[Any] = ()) -> int:
        """INSERT and return the new row id (atomically, under the connection lock)."""
        with self.lock:
            return self.conn.execute(sql, tuple(params)).lastrowid

    def all(self, sql: str, params: Iterable[Any] = ()) -> list[dict]:
        return [dict(r) for r in self.execute(sql, params).fetchall()]

    def one(self, sql: str, params: Iterable[Any] = ()) -> dict | None:
        row = self.execute(sql, params).fetchone()
        return dict(row) if row else None

    # ---- cameras
    def cameras(self, enabled_only: bool = False) -> list[dict]:
        sql = "SELECT * FROM cameras" + (" WHERE enabled=1" if enabled_only else "") + " ORDER BY id"
        cams = self.all(sql)
        for c in cams:
            c["zones"] = json.loads(c["zones"])
            c["retention_policy"] = json.loads(c["retention_policy"]) if c.get("retention_policy") else None
            c["synopsis_labels"] = json.loads(c["synopsis_labels"]) if c.get("synopsis_labels") else None
            c["policies"] = json.loads(c["policies"]) if c.get("policies") else []
            c["ptz_config"] = json.loads(c["ptz_config"]) if c.get("ptz_config") else None
        return cams

    def set_ptz_config(self, camera_id: str, cfg: dict) -> None:
        """Only ptz.py writes this; the camera form never touches it."""
        self.execute("UPDATE cameras SET ptz_config=? WHERE id=?", [json.dumps(cfg), camera_id])

    def upsert_camera(self, cam: dict) -> None:
        cols = ["id", "name", "host", "onvif_port", "rtsp_port", "username", "password",
                "main_path", "sub_path", "enabled", "zones", "retention_days", "scene_notes", "retention_policy",
                "synopsis_labels", "policies"]
        data = {**cam, "zones": json.dumps(cam.get("zones", []))}
        if "policies" in data:
            data["policies"] = json.dumps(data["policies"] or [])
        if "retention_policy" in data:
            data["retention_policy"] = json.dumps(data["retention_policy"]) if data["retention_policy"] else None
        if "synopsis_labels" in data:
            data["synopsis_labels"] = json.dumps(data["synopsis_labels"]) if data["synopsis_labels"] is not None else None
        present = [c for c in cols if c in data]
        self.execute(
            f"INSERT INTO cameras ({','.join(present)}) VALUES ({','.join('?' * len(present))}) "
            f"ON CONFLICT(id) DO UPDATE SET {','.join(f'{c}=excluded.{c}' for c in present if c != 'id')}",
            [data[c] for c in present],
        )

    # ---- events
    def create_event(self, **fields) -> int:
        fields.setdefault("created_at", time.time())
        for k in JSON_FIELDS:
            if k in fields and fields[k] is not None and not isinstance(fields[k], str):
                fields[k] = json.dumps(fields[k])
        cols = list(fields)
        cur = self.execute(f"INSERT INTO events ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                           [fields[c] for c in cols])
        return cur.lastrowid

    def update_event(self, event_id: int, **fields) -> None:
        for k in JSON_FIELDS:
            if k in fields and fields[k] is not None and not isinstance(fields[k], str):
                fields[k] = json.dumps(fields[k])
        self.execute(f"UPDATE events SET {','.join(f'{k}=?' for k in fields)} WHERE id=?",
                     [*fields.values(), event_id])

    def event(self, event_id: int) -> dict | None:
        e = self.one("SELECT *, EXISTS(SELECT 1 FROM locks WHERE locks.event_id = events.id) AS locked "
                     "FROM events WHERE id=?", [event_id])
        return decode_event(e) if e else None

    def index_event_text(self, event_id: int, synopsis: str, labels: str, embedding: list[float] | None) -> None:
        """(Re)index an event for search; replaces any previous entry."""
        with self.lock:
            self.conn.execute("DELETE FROM events_fts WHERE rowid=?", (event_id,))
            self.conn.execute("DELETE FROM events_vec WHERE rowid=?", (event_id,))
            self.conn.execute("INSERT INTO events_fts(rowid, synopsis, labels) VALUES (?,?,?)",
                              (event_id, synopsis, labels))
            if embedding:
                self.conn.execute("INSERT INTO events_vec(rowid, embedding) VALUES (?,?)",
                                  (event_id, serialize(embedding)))

    def unindex_event(self, event_id: int) -> None:
        with self.lock:
            self.conn.execute("DELETE FROM events_fts WHERE rowid=?", (event_id,))
            self.conn.execute("DELETE FROM events_vec WHERE rowid=?", (event_id,))

    def delete_event(self, event_id: int) -> None:
        """Remove an event row and everything keyed on it (search index, embeddings); files are the caller's."""
        self.unindex_event(event_id)
        with self.lock:
            self.conn.execute("DELETE FROM reid_vec WHERE rowid=?", (event_id,))
            self.conn.execute("DELETE FROM vehicle_vec WHERE rowid=?", (event_id,))
            self.conn.execute("DELETE FROM events WHERE id=?", (event_id,))

    # ---- person re-ID embeddings
    def set_vec(self, table: str, row_id: int, vec) -> None:
        assert table in ("reid_vec", "vehicle_vec")
        with self.lock:
            self.conn.execute(f"DELETE FROM {table} WHERE rowid=?", (row_id,))
            self.conn.execute(f"INSERT INTO {table}(rowid, embedding) VALUES (?,?)", (row_id, serialize(list(map(float, vec)))))

    def get_vec(self, table: str, row_id: int):
        import numpy as np
        assert table in ("reid_vec", "vehicle_vec")
        row = self.one(f"SELECT embedding FROM {table} WHERE rowid=?", [row_id])
        return np.frombuffer(row["embedding"], dtype=np.float32) if row else None

    def set_reid(self, event_id: int, vec) -> None:
        with self.lock:
            self.conn.execute("DELETE FROM reid_vec WHERE rowid=?", (event_id,))
            self.conn.execute("INSERT INTO reid_vec(rowid, embedding) VALUES (?,?)", (event_id, serialize(list(map(float, vec)))))

    def get_reid(self, event_id: int):
        import numpy as np
        row = self.one("SELECT embedding FROM reid_vec WHERE rowid=?", [event_id])
        return np.frombuffer(row["embedding"], dtype=np.float32) if row else None

    # ---- settings
    def get_setting(self, key: str, default=None):
        row = self.one("SELECT value FROM settings WHERE key=?", [key])
        return json.loads(row["value"]) if row else default

    def set_setting(self, key: str, value) -> None:
        self.execute("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     [key, json.dumps(value)])

    # ---- chat
    def chat(self, event_id: int) -> list[dict]:
        rows = self.all("SELECT * FROM chat_messages WHERE event_id=? ORDER BY id", [event_id])
        for r in rows:
            r["frames"] = json.loads(r["frames"])
        return rows

    def add_chat(self, event_id: int, role: str, content: str, frames: list | None = None, at: float | None = None) -> int:
        return self.execute("INSERT INTO chat_messages (event_id, role, content, frames, at, ts) VALUES (?,?,?,?,?,?)",
                            [event_id, role, content, json.dumps(frames or []), at, time.time()]).lastrowid

    def enrich(self, rows: list[dict]) -> list[dict]:
        """Add what browse cards show but SELECT * lacks: locked (footage lock) and journey_cameras."""
        ids = [r["id"] for r in rows if "locked" not in r or "journey_cameras" not in r]
        if ids:
            extra = {r["id"]: r for r in self.all(
                f"SELECT id, {LOCKED_SQL} AS locked, {JOURNEY_CAMS_SQL} AS journey_cameras FROM events "
                f"WHERE id IN ({','.join('?' * len(ids))})", ids)}
            for r in rows:
                x = extra.get(r["id"])
                if x:
                    r.setdefault("locked", x["locked"])
                    r.setdefault("journey_cameras", x["journey_cameras"])
        return rows

    def summary(self, where: list[str], params: list) -> dict:
        """Counts for Find's compliance strip: by broken-rule kind, PPE zone, camera and local day."""
        def w(*extra: str) -> str:
            parts = [*where, *extra]
            return f" WHERE {' AND '.join(parts)}" if parts else ""
        total = self.one(f"SELECT COUNT(*) AS n FROM events{w()}", params)["n"]
        by_kind = self.all(f"SELECT json_extract(policy, '$.kind') AS kind, COUNT(*) AS n FROM events"
                           f"{w('policy IS NOT NULL')} GROUP BY kind ORDER BY n DESC", params)
        by_zone = self.all(f"SELECT {PPE_ZONE_SQL} AS zone, COUNT(*) AS n FROM events"
                           f"{w(FLAG_SQL['ppe'])} GROUP BY zone ORDER BY n DESC", params)
        by_camera = self.all(f"SELECT camera_id, COUNT(*) AS n FROM events{w()} GROUP BY camera_id ORDER BY n DESC", params)
        by_day = self.all(f"SELECT date(start_ts, 'unixepoch', 'localtime') AS day, COUNT(*) AS n FROM events"
                          f"{w()} GROUP BY day ORDER BY day", params)
        return {"total": total, "by_kind": by_kind, "by_zone": [z for z in by_zone if z["zone"]],
                "by_camera": by_camera, "by_day": by_day}

    def search(self, query: str, embedding: list[float] | None, limit: int = 50,
               camera_id: str | None = None, since: float | None = None, until: float | None = None,
               label: str | None = None, min_yolo: float = 0, offset: int = 0, **filters) -> list[dict]:
        """Hybrid search: reciprocal-rank fusion of FTS5 keyword and vector results.

        Vector kNN always returns its k nearest neighbours, however unrelated, so vector hits are kept only
        when they are close in absolute terms (unit vectors: L2 < VEC_MAX_DIST) and relative to the best hit.
        """
        scores: dict[int, float] = {}
        # Filters first, ranking second: otherwise the 200 best matches from all history can all fall outside
        # "today" (or this camera) and the filter leaves nothing.
        eligible: set[int] | None = None
        # filters: status, priority, flags, place, ppe_zone, attention (event_filters)
        fw, fp = event_filters(camera=camera_id, label=label, since=since, until=until, min_yolo=min_yolo,
                               keep_unverified=False, **filters)
        fw.insert(0, "status != 'masked'")
        if len(fw) > 1:
            eligible = {r["id"] for r in self.all(f"SELECT id FROM events WHERE {' AND '.join(fw)}", fp)}
            if not eligible:
                return []
        ok = (lambda i: True) if eligible is None else eligible.__contains__
        words = [t.strip("?.,!:;'") for t in query.replace('"', " ").lower().split()]
        words = [t for t in words if t and t not in STOPWORDS]
        if words:
            terms = " OR ".join(f'"{t}"' for t in words)
            rows = self.all("SELECT rowid FROM events_fts WHERE events_fts MATCH ? ORDER BY rank LIMIT 4000", [terms])
            for rank, r in enumerate([r for r in rows if ok(r["rowid"])][:200]):
                scores[r["rowid"]] = scores.get(r["rowid"], 0) + 1 / (60 + rank)
        if embedding:
            k = 200 if eligible is None else 4096  # sqlite-vec's maximum; the filter is applied to these
            hits = [h for h in self.all("SELECT rowid, distance FROM events_vec WHERE embedding MATCH ? AND k = ? ORDER BY distance",
                                        [serialize(embedding), k]) if ok(h["rowid"])]
            if hits:
                cutoff = min(VEC_MAX_DIST, hits[0]["distance"] + VEC_MAX_GAP)  # relative to the best *eligible* hit
                for rank, r in enumerate(h for h in hits[:200] if h["distance"] <= cutoff):
                    scores[r["rowid"]] = scores.get(r["rowid"], 0) + 1 / (60 + rank)
        if not scores:
            return []
        ids = list(scores)
        rows = [decode_event(r) for r in self.all(
            f"SELECT * FROM events WHERE id IN ({','.join('?' * len(ids))}) AND {' AND '.join(fw)}", [*ids, *fp])]
        rows.sort(key=lambda r: (-scores[r["id"]], -r["start_ts"]))  # equal relevance: newest first
        page = rows[offset:offset + limit]
        return [{**r, "score": scores[r["id"]]} for r in self.enrich(page)]


def decode_event(e: dict) -> dict:
    for k in JSON_FIELDS:
        if e.get(k):
            e[k] = json.loads(e[k])
    return e


def serialize(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


db = Database(settings.data_dir / "nvr.db")
