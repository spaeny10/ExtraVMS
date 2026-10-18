"""Regenerate golden.json: the exact bytes Python's tunnelproto writes, which the Go bridge must match.

Run from the repo root:  .venv\\Scripts\\python.exe bridge\\internal\\proto\\testdata\\gen_golden.py
Bridge -> hub frames are dicts in the Go structs' field order, so Go's encoding must equal these bytes; hub -> bridge
frames carry extra fields the bridge must ignore.
"""
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "tunnelproto"))

from tunnelproto import CHUNK, chunk, encode, split  # noqa: E402

to_hub = {
    "hello": {"t": "hello", "proto": 1, "version": "0.1.0", "os": "windows", "arch": "amd64", "hostname": "Büro-PC",
              "lan": [{"if": "Ethernet", "addr": "192.168.1.20/24"}], "caps": ["tcp", "probe"]},
    "heartbeat": {"t": "heartbeat", "streams": 2, "bytes_up": 123456789, "bytes_down": 4321, "uptime_s": 3600,
                  "target_errors": {"bt_x:554": "connection refused"}},
    "heartbeat_empty": {"t": "heartbeat", "streams": 0, "bytes_up": 0, "bytes_down": 0, "uptime_s": 5, "target_errors": {}},
    "opened": {"t": "opened", "id": 1},
    "end": {"t": "end", "id": 3},
    "abort": {"t": "abort", "id": 5, "reason": "policy: <loopback> & co"},
    "credit": {"t": "credit", "id": 4294967295, "bytes": 65536},
    "probe_ok": {"t": "probe_result", "id": 7, "ok": True, "ms": 12, "error": None},
    "probe_fail": {"t": "probe_result", "id": "p1", "ok": False, "ms": 5000, "error": "timeout"},
    "rotated": {"t": "rotated"},
    "pong": {"t": "pong", "ts": 1760000000.25},
    "pong_bare": {"t": "pong"},
}

from_hub = {
    "welcome": {"t": "welcome", "bridge_id": "br_abc", "name": "Gate PC", "site": {"id": "loc_1", "name": "Ranch"},
                "heartbeat_s": 30, "max_streams": 16, "window": 524288, "future": "ignored"},
    "welcome_site_str": {"t": "welcome", "bridge_id": "br_abc", "name": "Gate PC", "site": "Ranch Büro"},
    "targets": {"t": "targets", "rev": 3, "items": [{"id": "bt_1", "host": "192.168.1.50", "ports": [554, 80, 555],
                                                    "label": "Gate"}]},
    "open": {"t": "open", "id": 1, "target": "bt_1", "host": "192.168.1.50", "port": 554, "extra": [1, 2]},
    "probe": {"t": "probe", "id": 9, "target": "bt_1", "host": "192.168.1.50", "port": 80},
    "rotate": {"t": "rotate", "token": "tok_new"},
    "revoked": {"t": "revoked", "reason": "retired by admin"},
    "enrolled": {"t": "enrolled", "bridge_id": "br_abc", "token": "tok_1"},
    "end": {"t": "end", "id": 3},
    "abort": {"t": "abort", "id": 5, "reason": "host closed"},
    "credit": {"t": "credit", "id": 7, "bytes": 65536},
    "ping": {"t": "ping", "ts": 1760000000.5},
    "unknown": {"t": "turn", "urls": ["turn:x"]},
}

data = bytes(i % 251 for i in range(150000))
out = {
    "chunk_size": CHUNK,
    "to_hub": {k: encode(v) for k, v in to_hub.items()},
    "from_hub": {k: encode(v) for k, v in from_hub.items()},
    "chunks": [
        {"id": 1, "data": b"hello".hex(), "frame": chunk(1, b"hello").hex()},
        {"id": 0, "data": "", "frame": chunk(0, b"").hex()},
        {"id": 4294967295, "data": bytes(range(256)).hex(), "frame": chunk(4294967295, bytes(range(256))).hex()},
    ],
    "split": {"id": 7, "len": len(data), "mod": 251,
              "frames": [{"len": len(f), "sha256": hashlib.sha256(f).hexdigest()} for f in (chunk(7, p) for p in split(data))]},
}
Path(__file__).with_name("golden.json").write_text(json.dumps(out, indent=1) + "\n", encoding="utf-8")
print("wrote golden.json")
