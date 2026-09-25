"""Compare the local Qwen with the remote model on this site's real data. Read-only: nothing is saved.

Replays recent person-event synopses, a journey narrative and a few Ask-the-NVR questions against each
configured backend and writes a side-by-side Markdown report with the outputs and timings (the first remote
call shows the cold start).

    cd backend
    ..\\.venv\\Scripts\\python.exe tools\\vlm_bench.py [--events 10] [--out vlm_bench_report.md]

Needs the NVR's Ollama running (start the NVR first). The remote runs only if NVR_REMOTE_VLM_URL / _KEY /
_MODEL are set in .env.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nvr import assistant, journeys, vlmroute  # noqa: E402
from nvr import synopsis as vlm  # noqa: E402
from nvr.db import db  # noqa: E402
from nvr.verifier import event_dir  # noqa: E402

QUESTIONS = ["Was anyone in the side yard today?", "When did someone go outside?",
             "How many people came through the East Door today?"]


async def timed(coro):
    t0 = time.time()
    try:
        return await coro, time.time() - t0, None
    except Exception as e:  # noqa: BLE001
        return None, time.time() - t0, f"{type(e).__name__}: {e}"


async def ask_once(backend, question: str) -> str:
    """Plan -> lookups -> answer with one backend, without storing a conversation."""
    now = time.time()
    system, text = assistant._plan_prompt(question, [], now)
    raw = await backend.chat_json([{"role": "system", "content": system}, {"role": "user", "content": text}],
                                  assistant.PLAN_SCHEMA, 300, 0.1, 240)
    calls = assistant.check_plan(raw, question, now)
    refs = assistant.Refs()
    results, _ = await assistant.run_calls(calls, refs)
    messages = [{"role": "system", "content": assistant.ANSWER_SYSTEM + " " + assistant._handles_note(refs)},
                {"role": "user", "content": f"Lookup results:\n{results}\n\nQuestion: {question}"}]
    out = "".join([c async for c in backend.stream(messages, 500, 0.2, 240)])
    return f"*looked up:* {', '.join(assistant.describe_call(c) for c in calls)}\n\n{out.strip()}"


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--events", type=int, default=10)
    ap.add_argument("--out", default="vlm_bench_report.md")
    args = ap.parse_args()
    r = vlmroute.router
    backends = [("local", r.local)] + ([("remote", r.remote)] if r.configured else [])
    if not r.configured:
        print("No remote model configured: timing the local model only.")
    cams = {c["id"]: c for c in db.cameras()}
    events = db.all("SELECT id FROM events WHERE status='verified' AND camera_class='person' AND synopsis IS NOT NULL "
                    "ORDER BY start_ts DESC LIMIT ?", [args.events])
    rows: list[str] = [f"# Qwen benchmark ({time.strftime('%Y-%m-%d %H:%M')})", "",
                       "Models: " + ", ".join(f"{n} = `{b.model}`" for n, b in backends), ""]
    timing: dict[str, list[float]] = {n: [] for n, _ in backends}

    rows += ["## Event synopses", ""]
    for e in events:
        ev = db.event(e["id"])
        d = event_dir(ev["id"])
        images = [(d / k["file"]).read_bytes() for k in (ev["detections"] or {}).get("keyframes", []) if (d / k["file"]).exists()][:4]
        if not images:
            continue
        prompt = vlm.build_prompt(ev, cams.get(ev["camera_id"], {"name": ev["camera_id"]}), [])
        rows.append(f"### Event #{ev['id']} ({cams.get(ev['camera_id'], {}).get('name', ev['camera_id'])})")
        rows.append(f"- **stored**: {ev['synopsis']}")
        for name, b in backends:
            out, secs, err = await timed(b.chat_json([{"role": "system", "content": vlm.SYSTEM},
                                                      {"role": "user", "content": prompt, "images": images}], vlm.SCHEMA, 600, 0.2, 300))
            timing[name].append(secs)
            rows.append(f"- **{name}** ({secs:.1f}s): " + (err or f"{out.get('summary', '')} *[threat: {out.get('threat_level')}]*"))
        rows.append("")
        print(f"event {ev['id']} done")

    j = db.one("SELECT id FROM journeys WHERE synopsis IS NOT NULL ORDER BY last_ts DESC LIMIT 1")
    if j:
        rows += ["## Journey narrative", ""]
        members = db.all("SELECT id, camera_id, start_ts, end_ts, synopsis FROM events WHERE journey_id=? ORDER BY start_ts", [j["id"]])
        items = [{"camera": cams.get(m["camera_id"], {}).get("name", m["camera_id"]), "time": journeys._hhmmss(m["start_ts"]),
                  "duration_s": (m["end_ts"] or m["start_ts"]) - m["start_ts"], "gap_s": None, "descriptions": [m["synopsis"]]}
                 for m in members][:6]
        images = [img for m in members[:4] if (img := journeys._crop_bytes(m["id"]))]
        for name, b in backends:
            orig = vlmroute.router.use_remote
            vlmroute.router.use_remote = (lambda task, _n=name: _n == "remote")  # route this call to the backend under test
            try:
                out, secs, err = await timed(vlm.journey_narrative(items, images))
            finally:
                vlmroute.router.use_remote = orig
            rows.append(f"- **{name}** ({secs:.1f}s): " + (err or f"{out.get('overall', '')} / actions: {out.get('actions')}"))
        rows.append("")

    rows += ["## Ask the NVR", ""]
    for q in QUESTIONS:
        rows.append(f"### {q}")
        for name, b in backends:
            out, secs, err = await timed(ask_once(b, q))
            timing[name].append(secs)
            rows.append(f"- **{name}** ({secs:.1f}s): " + (err or out.replace("\n", "\n  ")))
        rows.append("")
        print(f"question done: {q}")

    rows += ["## Timing", ""]
    for name, ts in timing.items():
        if ts:
            rows.append(f"- {name}: first call {ts[0]:.1f}s (includes any cold start), median {sorted(ts)[len(ts) // 2]:.1f}s over {len(ts)} calls")
    Path(args.out).write_text("\n".join(rows) + "\n", encoding="utf-8")
    print(f"report written to {Path(args.out).resolve()}")


if __name__ == "__main__":
    asyncio.run(main())
