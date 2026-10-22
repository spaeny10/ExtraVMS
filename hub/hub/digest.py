"""Customer digest: one morning note per customer, grouped by Site, written from what each server reports.

Each server is asked for its structured digest facts (NVR GET /api/digest/facts: weapons with the second look's
verdict, unreviewed high-priority events, unusual events, site rules, PPE by zone, counts). A server too old for that
endpoint falls back to its latest briefing. The text is assembled here, in this order:

    **Needs your attention**                      unreviewed high-priority events of every Site, worded by code
    - World Headquarters › TSS-DT · South Exterior Door, yesterday 4:17 PM: Possible holstered handgun seen on a
      second look (medium confidence), unconfirmed: needs review [#22909]
    **World Headquarters**                         one section per Site (servers without one: "(no Site)")
    - TSS-DT: <one line per server that had something to say>
    - PPE: 74 violations (missing hard hat / hi-vis vest), mostly East Door (40), Side Yard (34).
    - Quiet: DC1 Server, DC1-Kanop               servers with nothing to report, one line per Site

With the shared model configured it writes the per-server lines from those facts (never the attention list); without
it, or when it fails, the lines are plain. Weapons are worded by code from the verdict: "confirmed" only for a
verdict of confirmed with HIGH confidence (the NVR's weaponcheck rule), everything else "possible ..., unconfirmed:
needs review"; a model line that talks about a weapon any other way is reworded or dropped (enforce_weapon_wording).
Times are 12-hour in the Site's timezone. Event references [#id] are stored in order with their server
(data.refs: {server_id, event_id, camera_id, location_id}) so the UI links each to its event; data.sections has each
Site's own part (its attention items and section) for the Site page. PPE is one line per Site unless the Site turned
it off (locations.digest_ppe = false: PPE is left out of that Site's digest entirely).
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import re
import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import sqlalchemy as sa

from . import alerts, db, vlm_proxy
from .agents import registry
from .config import settings

log = logging.getLogger("hub.digest")

FORMAT = 2
PERIOD_S = 86400
ATTENTION_MAX = 8
NO_SITE = "(no Site)"
FACTS_PATH = "/api/digest/facts"
HEADERS = {"x-hub-user": "hub-digest", "x-hub-role": "viewer"}
REF_RE = re.compile(r"\[#(\d+)\]")

# ---------------------------------------------------------------- collecting


async def _get_json(conn, path: str, query: str, name: str) -> dict | None:
    try:
        status, body = await conn.call("GET", path, query, HEADERS, None, 15)
        if status == 200:
            out = json.loads(body.decode())
            return out if isinstance(out, dict) else None
    except Exception as e:
        log.warning("digest: %s %s failed: %s", name, path, e)
    return None


def _acked_events(org_id: str, since: float) -> set[tuple[str, int]]:
    """(server, event id) of event alerts someone acknowledged on the hub: handled, not "needs your attention"."""
    q = sa.select(db.alerts.c.site_id, db.alerts.c.key).where(
        db.alerts.c.org_id == org_id, db.alerts.c.kind.in_(alerts.EVENT_KINDS), db.alerts.c.acked_at.is_not(None),
        db.alerts.c.opened_at >= since - alerts.EVENT_TTL_S)
    return {(r["site_id"], int(r["key"])) for r in db.rows(q) if str(r["key"]).isdigit()}


async def collect(org_id: str, now: float | None = None) -> dict:
    now = now or time.time()
    since = now - PERIOD_S
    sites = db.rows(sa.select(db.sites).where(db.sites.c.org_id == org_id, db.sites.c.retired_at.is_(None)).order_by(db.sites.c.name))
    locs = {r["id"]: r for r in db.rows(sa.select(db.locations).where(db.locations.c.org_id == org_id))}
    acked = _acked_events(org_id, since)
    cams = {(r["server_id"], r["camera_id"]): r["name"] for r in db.rows(
        sa.select(db.cameras.c.server_id, db.cameras.c.camera_id, db.cameras.c.name).where(db.cameras.c.org_id == org_id))}
    parts = []
    for s in sites:
        conn = registry.get(s["id"])
        facts = briefing = None
        if conn is not None:
            facts = await _get_json(conn, FACTS_PATH, f"since={since:.0f}&until={now:.0f}", s["name"])
            if facts is None:   # a server from before /api/digest/facts: its latest briefing, as digests always were
                b = await _get_json(conn, "/api/briefings", "limit=1", s["name"])
                bs = (b or {}).get("briefings") or []
                briefing = bs[0] if bs else None
        summ = s.get("summary") or {}
        loc = locs.get(s.get("location_id")) or {}
        open_alerts = []
        for a in alerts.open_for_org(org_id, [s["id"]], 20):
            d = dict(a.get("detail") or {})
            if a["kind"] in alerts.EVENT_KINDS and d.get("camera_id"):
                d["camera"] = cams.get((s["id"], str(d["camera_id"])))
            open_alerts.append({"kind": a["kind"], "detail": d})
        parts.append({"site_id": s["id"], "site_name": s["name"], "location_id": s.get("location_id"),
                      "location_name": loc.get("name"), "timezone": loc.get("timezone"),
                      "digest_ppe": loc.get("digest_ppe") is not False, "online": s["id"] in registry.by_site,
                      "headline": (briefing or {}).get("headline"), "text": (briefing or {}).get("text"),
                      "today": summ.get("today") or {}, "cameras_down": [c["name"] for c in summ.get("cameras") or [] if not c.get("stream_ready")],
                      "open_alerts": open_alerts, "facts": facts,
                      "acked_events": sorted(i for sid, i in acked if sid == s["id"])})
    return {"format": FORMAT, "generated_at": now, "since": since, "sites": parts, "locations": group(parts)}


def group(parts: list[dict]) -> list[dict]:
    """[{id, name, timezone, digest_ppe, servers: [server ids]}] by Site name; servers without a Site are one group
    (id None, NO_SITE), listed last."""
    groups: dict[str, dict] = {}
    for p in parts:
        key = p.get("location_id") or ""
        g = groups.setdefault(key, {"id": p.get("location_id") or None,
                                    "name": (p.get("location_name") or p["site_name"]) if p.get("location_id") else NO_SITE,
                                    "timezone": p.get("timezone"), "digest_ppe": p.get("digest_ppe", True) is not False,
                                    "servers": []})
        g["servers"].append(p["site_id"])
    return sorted(groups.values(), key=lambda g: (g["id"] is None, g["name"].casefold()))


def _grouped(parts: list[dict]) -> list[tuple[dict, list[dict]]]:
    by_id = {p["site_id"]: p for p in parts}
    return [(g, [by_id[sid] for sid in g["servers"]]) for g in group(parts)]


# ---------------------------------------------------------------- wording

def _zone(name: str | None):
    if name:
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            pass
    return None


def clock(t: dt.datetime) -> str:
    return f"{t.hour % 12 or 12}:{t.minute:02d} {'AM' if t.hour < 12 else 'PM'}"


def when(ts: float, tz: str | None, now: float) -> str:
    """12-hour, in the Site's timezone (the hub's own without one): "today 6:02 AM", "yesterday 4:17 PM", "Oct 19, 4:17 PM"."""
    z = _zone(tz)
    t, today = dt.datetime.fromtimestamp(ts, z), dt.datetime.fromtimestamp(now, z).date()
    if t.date() == today:
        return f"today {clock(t)}"
    if t.date() == today - dt.timedelta(days=1):
        return f"yesterday {clock(t)}"
    return f"{t:%b} {t.day}, {clock(t)}"


_H24_RE = re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)(?::[0-5]\d)?\b(?!\s*(?:[AaPp]\.?\s?[Mm]\b))")


def twelve_hour(text: str) -> str:
    """"at 16:17" -> "at 4:17 PM" (a model line that wrote a 24-hour time); 12-hour times are left alone."""
    def f(m: re.Match) -> str:
        h, mi = int(m.group(1)), m.group(2)
        return f"{h % 12 or 12}:{mi} {'AM' if h < 12 else 'PM'}"
    return _H24_RE.sub(f, text)


_LONGEST_RE = re.compile(r"(?:a duration |a stay |that is |which is |, )?longer than 100\s?% of (?:all |the |previous |earlier |past )*"
                         r"(?:visits|stays|sightings)", re.I)


def fix_longest(text: str) -> str:
    """"longer than 100% of previous visits" -> "the longest visit on record" (the NVR says so itself since this change;
    older servers and a model's paraphrase still say it the old way)."""
    return _LONGEST_RE.sub(lambda m: (", " if m.group(0).startswith(",") else " " if m.group(0)[:1].isspace() else "")
                           + "the longest visit on record", text).replace("  ", " ")


_WEAPON_RE = re.compile(r"(?<!nail )(?<!glue )(?<!heat )(?<!spray )(?<!staple )(?<!caulk )(?<!putty )\b(?:hand\s?guns?|pistols?|"
                        r"revolvers?|shotguns?|rifles?|firearms?|guns?|gunman|knife|knives|machetes?|daggers?|weapons?)\b", re.I)


def weapon_confirmed(w: dict) -> bool:
    """Confirmed only when the server's verdict is confirmed AND the second look was HIGH confidence."""
    return w.get("verdict") == "confirmed" and w.get("confidence") == "high"


def weapon_phrase(w: dict) -> str:
    """How the digest words a weapon event, from the verdict alone (never the model's own description)."""
    kind = w.get("kind") or "weapon"
    what = w.get("object") or kind
    conf = w.get("confidence")
    if weapon_confirmed(w):
        return f"{what[:1].upper()}{what[1:]} confirmed by a second look (high confidence)"
    if conf:
        return f"Possible {what} seen on a second look ({conf} confidence), unconfirmed: needs review"
    return f"Possible {kind}, unconfirmed: needs review"


def _sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?])\s+", (text or "").strip()) if s]


def enforce_weapon_wording(text: str, weapons: dict[int, dict]) -> str:
    """A model's (or an older server's) text, with every sentence about a weapon checked against the verdicts: kept
    when every event it cites is a confirmed weapon (weapon_confirmed); reworded from the verdict when it cites weapon
    events that aren't; dropped when it cites none (a weapon nobody's second look confirmed is not a fact to repeat)."""
    out = []
    for s in re.split(r"(?<=[.!?;])\s+", (text or "").strip()):   # sentences and clauses
        if not s or not _WEAPON_RE.search(s):
            out.append(s)
            continue
        cited = [(int(n), weapons[int(n)]) for n in REF_RE.findall(s) if int(n) in weapons]
        if cited and all(weapon_confirmed(w) for _, w in cited):
            out.append(s)
        elif cited:
            out.append("; ".join(f"{weapon_phrase(w)} [#{i}]" for i, w in cited) + (s[-1] if s[-1] in ".!?;" else "."))
    return " ".join(x for x in out if x).rstrip(";").strip()


_PPE_RE = re.compile(r"\b(?:PPE|hard ?hats?|hi-?vis|high-visibility|safety vests?|vests?)\b", re.I)


def _drop_ppe(text: str) -> str:
    return " ".join(s for s in _sentences(text) if not _PPE_RE.search(s))


def _strip_markdown(text: str) -> str:
    lines = [re.sub(r"^\s*(?:[-*•]|\d+[.)])\s+", "", ln) for ln in str(text or "").splitlines()]
    return re.sub(r"\s+", " ", " ".join(lines).replace("**", "").replace("__", "")).strip()


def _known_ids(p: dict) -> set[int]:
    f = p.get("facts") or {}
    ids = {x.get("id") for k in ("weapons", "attention", "unusual") for x in f.get(k) or []}
    ids |= {i for r in f.get("rules") or [] for i in r.get("examples") or []}
    ids |= {z.get("example") for z in (f.get("ppe") or {}).get("zones") or []}
    ids |= {(a.get("detail") or {}).get("id") for a in p.get("open_alerts") or []}
    return {int(i) for i in ids if isinstance(i, int) or (isinstance(i, str) and i.isdigit())}


def clean_line(text: str, p: dict) -> str:
    """A model's line (or an older server's headline) made safe to show: one plain line, weapons only as the verdicts
    allow, references only to this server's events, 12-hour times, no "longer than 100%", PPE left out when the Site
    turned it off."""
    weapons = {int(w["id"]): w for w in (p.get("facts") or {}).get("weapons") or [] if isinstance(w.get("id"), int)}
    known = _known_ids(p)
    s = enforce_weapon_wording(_strip_markdown(text), weapons)
    s = REF_RE.sub(lambda m: m.group(0) if int(m.group(1)) in known else "", s)
    s = fix_longest(twelve_hour(s))
    if p.get("digest_ppe") is False:
        s = _drop_ppe(s)
    return re.sub(r"\s+([.,;:])", r"\1", re.sub(r"\s{2,}", " ", s)).strip()


# ---------------------------------------------------------------- the parts of the text

def _attention(p: dict, now: float) -> list[dict]:
    """This server's "needs your attention" items: [{rank, ts, event_id, camera_id, text}], unconfirmed weapons
    first. From the facts (unreviewed high-priority events) when the server sent them, else its open event alerts."""
    f, tz = p.get("facts"), p.get("timezone")
    acked = set(p.get("acked_events") or [])
    items = []
    if f is not None:
        for a in f.get("attention") or []:
            if a.get("id") in acked:
                continue
            w = a.get("weapon")
            if w:
                what, rank = weapon_phrase(w), 1 if weapon_confirmed(w) else 0
            else:
                rank = 2
                what = (f"Site rule: {a['policy']}" if a.get("policy") else f"Watch list: {a['watched']}" if a.get("watched")
                        else clean_line(a.get("synopsis") or "", p)[:160] or f"High-priority {a.get('label') or 'event'}")
            cam = a.get("camera") or a.get("camera_id") or "camera"
            items.append({"rank": rank, "ts": a.get("start_ts") or 0, "event_id": a["id"], "camera_id": a.get("camera_id"),
                          "text": f"{cam}, {when(a.get('start_ts') or now, tz, now)}: {what}"})
    else:
        for al in p.get("open_alerts") or []:
            d = al.get("detail") or {}
            if al.get("kind") not in alerts.EVENT_KINDS or not str(d.get("id", "")).isdigit():
                continue
            what = (f"Site rule: {d['text']}" if al["kind"] == "event_policy" and d.get("text")
                    else f"Watch list: {d['name']}" if al["kind"] == "event_watched" and d.get("name")
                    else clean_line(d.get("synopsis") or "", p)[:160] or f"High-priority {d.get('label') or 'event'}")
            if _WEAPON_RE.search(d.get("synopsis") or "") and not _WEAPON_RE.search(what):
                what = f"Possible weapon described, unconfirmed: needs review ({what})" if what else "Possible weapon, unconfirmed: needs review"
            cam = d.get("camera") or d.get("camera_id") or "camera"
            items.append({"rank": 2, "ts": d.get("start_ts") or 0, "event_id": int(d["id"]), "camera_id": d.get("camera_id"),
                          "text": f"{cam}, {when(d.get('start_ts') or now, tz, now)}: {what}"})
    return sorted(items, key=lambda x: (x["rank"], -x["ts"]))


def _counts(today: dict) -> str:
    return ", ".join(f"{v} {k}" for k, v in sorted(today.items(), key=lambda kv: -kv[1]) if v)


def plain_line(p: dict, n_attention: int, now: float) -> str:
    """One server's line without the model: alerts above, unusual events, broken rules, cameras down, activity."""
    tz, f = p.get("timezone"), p.get("facts")
    bits = []
    if not p.get("online"):
        bits.append("offline when the digest was made")
    if n_attention:
        bits.append(f"{n_attention} alert{'s' if n_attention != 1 else ''} listed above")
    if f is not None:
        for u in (f.get("unusual") or [])[:2]:
            why = fix_longest((u.get("reasons") or ["unusual for the camera"])[0])
            bits.append(f"unusual at {u.get('camera') or u.get('camera_id')}, {when(u['start_ts'], tz, now)}: {why} [#{u['id']}]")
        for r in (f.get("rules") or [])[:2]:
            ex = (r.get("examples") or [])[:1]
            bits.append(f"site rule broken {r['count']}×: {r.get('text') or r.get('kind')}" + "".join(f" [#{i}]" for i in ex))
    if p.get("cameras_down"):
        bits.append("cameras down: " + ", ".join(p["cameras_down"]))
    if f is None and (p.get("headline") or p.get("text")):
        bits.append(clean_line(p.get("headline") or p.get("text") or "", p))
    counts = _counts((f or {}).get("counts") or p.get("today") or {})
    if counts and p.get("online"):
        bits.append(counts)
    s = "; ".join(b for b in bits if b)
    return (s[:1].upper() + s[1:] + ("" if s.endswith((".", "!", "?")) else ".")) if s else "Nothing to report."


def _quiet(p: dict, n_attention: int) -> bool:
    f = p.get("facts")
    if not p.get("online") or n_attention or p.get("cameras_down"):
        return False
    if f is None:
        return not (p.get("headline") or p.get("text"))
    return not (f.get("unusual") or f.get("rules"))


def ppe_line(servers: list[dict]) -> str | None:
    """One short PPE line for a Site: the count and the top zones (None when there were none, or the Site turned PPE
    off)."""
    if not servers or servers[0].get("digest_ppe") is False:
        return None
    total, zones, missing = 0, {}, {}
    for p in servers:
        ppe = (p.get("facts") or {}).get("ppe") or {}
        total += int(ppe.get("count") or 0)
        for z in ppe.get("zones") or []:
            zones[z["zone"]] = zones.get(z["zone"], 0) + int(z.get("count") or 0)
        for k, v in (ppe.get("missing") or {}).items():
            missing[k] = missing.get(k, 0) + int(v or 0)
    if not total:
        return None
    items = " / ".join(k for k, _ in sorted(missing.items(), key=lambda kv: -kv[1])[:2])
    top = ", ".join(f"{z} ({n})" for z, n in sorted(zones.items(), key=lambda kv: -kv[1])[:3])
    return (f"PPE: {total} violation{'s' if total != 1 else ''}" + (f" (missing {items})" if items else "")
            + (f", mostly {top}" if top else "") + ".")


# ---------------------------------------------------------------- the whole text

def compose(data: dict, lines: dict[str, str] | None = None, now: float | None = None) -> tuple[str, list[dict], list[dict]]:
    """(markdown text, refs, sections) from collect()'s data and the model's per-server lines (None: plain). refs lists
    every [#id] of the text in order with its server; sections are each Site's own part with its own refs."""
    now = now or data.get("generated_at") or time.time()
    parts = data.get("sites") or []
    lines = lines or {}
    groups = _grouped(parts)
    attention: list[tuple[dict, dict, dict]] = []   # (item, server, group)
    for g, servers in groups:
        for p in servers:
            attention += [(it, p, g) for it in _attention(p, now)]
    attention.sort(key=lambda x: (x[0]["rank"], -x[0]["ts"]))
    n_att: dict[str, int] = {}
    for _, p, _g in attention:
        n_att[p["site_id"]] = n_att.get(p["site_id"], 0) + 1

    def att_line(it: dict, p: dict, g: dict) -> tuple[str, dict, str | None]:
        return f"- {g['name']} › {p['site_name']} · {it['text']} [#{it['event_id']}]", p, it.get("camera_id")

    def body(g: dict, servers: list[dict]) -> list[tuple[str, dict | None, str | None]]:
        out: list[tuple[str, dict | None, str | None]] = [(f"**{g['name']}**", None, None)]
        quiet = []
        for p in sorted(servers, key=lambda p: p["site_name"].casefold()):
            n = n_att.get(p["site_id"], 0)
            if _quiet(p, n):
                quiet.append(p["site_name"])
                continue
            line = clean_line(lines.get(p["site_id"]) or "", p) if lines.get(p["site_id"]) else ""
            if line and n and "above" not in line:
                line = f"{line.rstrip('.')}; {n} alert{'s' if n != 1 else ''} listed above."
            out.append((f"- {p['site_name']}: {line or plain_line(p, n, now)}", p, None))
        ppe = ppe_line(servers)
        if ppe:
            out.append((f"- {ppe}", None, None))
        if quiet:
            out.append(("- Quiet: " + ", ".join(quiet), None, None))
        return out

    def render(rows: list[tuple[str, dict | None, str | None]]) -> tuple[str, list[dict]]:
        refs = []
        for text, p, cam in rows:
            for m in REF_RE.finditer(text):
                refs.append({"server_id": p["site_id"] if p else None, "event_id": int(m.group(1)),
                             "camera_id": cam or (_ref_camera(p, int(m.group(1))) if p else None),
                             "location_id": p.get("location_id") if p else None})
        return "\n".join(t for t, _, _ in rows), refs

    head: list[tuple[str, dict | None, str | None]] = []
    if attention:
        head.append(("**Needs your attention**", None, None))
        head += [att_line(*x) for x in attention[:ATTENTION_MAX]]
        if len(attention) > ATTENTION_MAX:
            more = len(attention) - ATTENTION_MAX
            head.append((f"- …and {more} more open alert{'s' if more != 1 else ''} (see Alerts)", None, None))
    else:
        head.append(("**Needs your attention:** nothing open.", None, None))
    rows = list(head)
    sections = []
    for g, servers in groups:
        b = body(g, servers)
        rows += [("", None, None)] + b
        mine = [att_line(*x) for x in attention if x[2] is g]
        sec_rows = ([("**Needs your attention**", None, None)] + mine + [("", None, None)] if mine else []) + b
        s_text, s_refs = render(sec_rows)
        sections.append({"location_id": g["id"], "name": g["name"], "servers": g["servers"], "text": s_text, "refs": s_refs})
    text, refs = render(rows if parts else [("No servers.", None, None)])
    return text, refs, sections


def _ref_camera(p: dict, event_id: int) -> str | None:
    f = p.get("facts") or {}
    for k in ("attention", "unusual", "weapons"):
        for x in f.get(k) or []:
            if x.get("id") == event_id:
                return x.get("camera_id")
    for a in p.get("open_alerts") or []:
        d = a.get("detail") or {}
        if str(d.get("id")) == str(event_id):
            return d.get("camera_id")
    return None


# ---------------------------------------------------------------- the model's per-server lines

SYSTEM = ("You write one short line per server for a security customer's morning digest. For each server key given, write "
          "one or two short, factual sentences about what stands out there: unusual events, broken site rules, cameras "
          "down, notable activity. Do not mention weapons, firearms, knives, alerts listed separately, or PPE: those are "
          "reported separately by the system. Cite events as [#id] exactly as given, only ids given for that server. "
          "Copy times exactly as given (12-hour with AM/PM). Do not start with the server or Site name. No markdown, no "
          "bullets. Use American English spelling.")
SCHEMA = {"type": "object", "additionalProperties": False, "required": ["servers"],
          "properties": {"servers": {"type": "array", "items": {
              "type": "object", "additionalProperties": False, "required": ["key", "text"],
              "properties": {"key": {"type": "string"}, "text": {"type": "string"}}}}}}


def _facts_text(key: str, g: dict, p: dict, n_attention: int, now: float) -> str:
    tz, f = p.get("timezone"), p.get("facts")
    out = [f"{key}: server {p['site_name']} at Site {g['name']} ({'online' if p['online'] else 'OFFLINE'})"]
    if f is not None:
        out.append(f"Activity (last 24 h): {_counts(f.get('counts') or {}) or 'none'}")
        for u in f.get("unusual") or []:
            out.append(f"Unusual [#{u['id']}] {u.get('camera')}, {when(u['start_ts'], tz, now)}: "
                       f"{'; '.join(fix_longest(r) for r in u.get('reasons') or []) or 'unusual for the camera'}"
                       + (f" ({enforce_weapon_wording(u['synopsis'], {})[:140]})" if u.get("synopsis") else ""))
        for r in f.get("rules") or []:
            out.append(f"Site rule broken {r['count']} times ({r.get('priority') or 'unknown'} priority): {r.get('text') or r.get('kind')} "
                       + " ".join(f"[#{i}]" for i in r.get("examples") or []))
    else:
        brief = enforce_weapon_wording(_strip_markdown(p.get("text") or p.get("headline") or ""), {})
        if p.get("digest_ppe") is False:
            brief = _drop_ppe(brief)
        out.append(f"Server briefing: {fix_longest(twelve_hour(brief))[:600] or 'none'}")
    out.append(f"Cameras down: {', '.join(p.get('cameras_down') or []) or 'none'}")
    if n_attention:
        out.append(f"({n_attention} high-priority alert{'s' if n_attention != 1 else ''} listed separately: do not repeat)")
    return "\n".join(out)


async def _summarize(org_name: str, data: dict) -> dict[str, str] | None:
    """{server id: line} from the shared model for the servers that have something to say, or None."""
    if not vlm_proxy.configured():
        return None
    now = data.get("generated_at") or time.time()
    keys: dict[str, str] = {}
    blocks = []
    for g, servers in _grouped(data.get("sites") or []):
        for p in servers:
            n = len(_attention(p, now))
            if _quiet(p, n) or not p.get("online"):
                continue
            key = f"S{len(keys) + 1}"
            keys[key] = p["site_id"]
            blocks.append(_facts_text(key, g, p, n, now))
    if not blocks:
        return {}
    messages = [{"role": "system", "content": SYSTEM},
                {"role": "user", "content": f"Customer: {org_name}\n\n" + "\n\n".join(blocks)}]
    try:
        raw = await vlm_proxy.complete(messages, max_tokens=700, temperature=0.2, schema=SCHEMA)
    except Exception as e:
        log.warning("digest: shared AI failed: %s", e)
        return None
    try:
        m = re.search(r"\{.*\}", raw or "", re.S)
        rows = json.loads(m.group(0) if m else raw or "")["servers"]
        return {keys[r["key"]]: str(r["text"]) for r in rows if isinstance(r, dict) and r.get("key") in keys and r.get("text")}
    except (ValueError, KeyError, TypeError):
        log.warning("digest: shared AI answered something else: %s", (raw or "")[:200])
        return None


async def generate(org_id: str) -> dict:
    org = db.one(sa.select(db.orgs).where(db.orgs.c.id == org_id))
    data = await collect(org_id)
    lines = await _summarize(org["name"] if org else org_id, data)
    data["lines"] = lines or {}
    text, refs, sections = compose(data, lines)
    data["refs"], data["sections"] = refs, sections
    row = {"org_id": org_id, "day": dt.date.today().isoformat(), "created_at": time.time(), "text": text, "data": data,
           "model": settings.vllm_model if lines else None}
    db.insert(db.digests, row)
    return db.one(sa.select(db.digests).where(db.digests.c.org_id == org_id).order_by(db.digests.c.id.desc()))


def latest(org_id: str, limit: int = 7) -> list[dict]:
    return db.rows(sa.select(db.digests).where(db.digests.c.org_id == org_id).order_by(db.digests.c.id.desc()).limit(limit))


async def daily_loop() -> None:
    """Every org gets a digest at settings.digest_hour (hub local time), once per day."""
    while True:
        now = dt.datetime.now()
        target = now.replace(hour=settings.digest_hour, minute=30, second=0, microsecond=0)
        if target <= now:
            target += dt.timedelta(days=1)
        await _sleep((target - now).total_seconds())
        for org in db.rows(sa.select(db.orgs)):
            if not db.one(sa.select(db.digests).where(db.digests.c.org_id == org["id"], db.digests.c.day == dt.date.today().isoformat())):
                try:
                    await generate(org["id"])
                except Exception:
                    log.exception("digest for %s failed", org["name"])


async def _sleep(s: float) -> None:
    import asyncio
    await asyncio.sleep(max(1.0, s))


def scoped(row: dict, see) -> dict:
    """A digest cut down for a Site-restricted member (`see`: auth.scope_filter's predicate): only the servers they
    can see, and in place of the whole customer's text (or its AI lines) the plain text for those servers."""
    data = row.get("data") if isinstance(row.get("data"), dict) else {}
    if data.get("sites") is None:
        return {**row, "text": "This digest covers Sites you don't have access to.", "model": None, "scoped": True, "data": None}
    parts = [p for p in data.get("sites") or [] if see({"site_id": p.get("site_id")})]
    cut = {**data, "sites": parts, "locations": group(parts), "lines": {}}
    text, refs, sections = compose(cut, None, data.get("generated_at") or row.get("created_at"))
    return {**row, "text": text, "model": None, "scoped": True, "data": {**cut, "refs": refs, "sections": sections}}
