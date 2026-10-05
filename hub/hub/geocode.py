"""Site addresses -> coordinates (and back), and the time zone at a point.

Providers:
  * OpenStreetMap Nominatim (HUB_GEOCODER_URL points at a self-hosted Nominatim or any endpoint that speaks its /search
    and /reverse jsonv2 API; HUB_GEOCODER_KEY, when set, is sent as `key=` for hosted ones that want it). Its usage
    policy, honoured here: an identifying User-Agent and at most one request a second for the whole hub (an asyncio
    lock plus a sleep between calls). OSM often misses US rural/highway addresses as people write them ("10187 SW US
    HWY 54"), so an empty answer is retried with rewritten forms (nominatim_variants: "US-54", "Highway", no
    direction before a highway, no ZIP).
  * The US Census Bureau geocoder (free, no key; HUB_CENSUS_URL, "off" disables it), which resolves US street
    addresses as typed. Asked first for anything that looks like a US street address (us_street_first), otherwise as
    a fallback for street addresses. No published rate limit; kept to a few requests a second.
The first provider with an answer wins; results are cached in kv by normalised query for 30 days, so the same address
is never asked twice. Every failure degrades to [] / None: a Site page never 500s because a geocoder is down.

Time zones come from the coordinates offline (timezonefinder), never from a provider.

Backfill: Sites with an address but no coordinates are geocoded once per hub start (and by `python -m hub geocode`);
each attempt is recorded in kv so an address that fails is retried no sooner than 24 h later. Unattended, a hit is
only taken when it is trustworthy (backfill_accepts: the address starts with a house number, and the hit has a house
number or the ZIP the address names), because a pin in the wrong place is worse than none for a SOC: "Building 2"
once landed somewhere arbitrary. Picks in the Settings page are the person's choice and are not second-guessed.
"""
from __future__ import annotations

import asyncio
import collections
import hashlib
import logging
import re
import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
import sqlalchemy as sa

from . import db
from .config import settings

log = logging.getLogger("hub.geocode")

USER_AGENT = "AxiomVisionHub/0.2 (+https://hub.axiomvision.ai)"
DEFAULT_URL = "https://nominatim.openstreetmap.org"
CENSUS_URL = "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"
MIN_INTERVAL_S = 1.0                  # Nominatim: an absolute maximum of 1 request per second
CENSUS_INTERVAL_S = 0.34              # Census: no published limit; a few a second at most
CACHE_TTL_S = 30 * 86400
RETRY_AFTER_S = 24 * 3600             # backfill: a failed address waits this long before it is tried again
TIMEOUT_S = 8.0
MAX_VARIANTS = 3                      # Nominatim tries per lookup: as typed + up to two rewrites
PART_KEYS = ("house_number", "street", "city", "county", "state", "state_code", "postcode", "country", "country_code", "display_name")
HEAL_KEY = "geo:heal_v1"              # set once the pre-guard backfill's untrustworthy pins have been cleared

# tests swap these (a fake httpx transport, a recording sleep, a fake clock)
transport: httpx.AsyncBaseTransport | None = None
_sleep = asyncio.sleep
_clock = time.monotonic

_last_call = -1e9          # Nominatim
_last_census = -1e9
_locks: dict[str, tuple[asyncio.AbstractEventLoop, asyncio.Lock]] = {}


def _hub_lock(name: str = "nominatim") -> asyncio.Lock:
    """One lock per provider per event loop (the app has one loop; tests may make several)."""
    loop = asyncio.get_running_loop()
    cur = _locks.get(name)
    if cur is None or cur[0] is not loop:
        cur = _locks[name] = (loop, asyncio.Lock())
    return cur[1]


MAP_TILES = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
MAP_ATTRIBUTION = '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'


def map_config() -> dict:
    """Tiles for the hub UI's maps: OSM's standard tiles with their attribution unless HUB_MAP_TILES says otherwise."""
    tiles = settings.map_tiles.strip() or MAP_TILES
    return {"tiles": tiles, "attribution": settings.map_attribution.strip() or (MAP_ATTRIBUTION if tiles == MAP_TILES else "")}


def base_url() -> str:
    return (settings.geocoder_url or DEFAULT_URL).rstrip("/")


def source() -> str:
    """What geocode_source records for a Nominatim(-compatible) hit."""
    return "nominatim" if base_url() == DEFAULT_URL else "geocoder"


def census_url() -> str | None:
    u = (settings.census_url or "").strip()
    return None if u.lower() == "off" else (u or CENSUS_URL)


def normalise(q: str) -> str:
    return " ".join((q or "").casefold().replace(",", ", ").split()).strip(" ,")


def _key(kind: str, q: str) -> str:
    return f"geo:{kind}:" + hashlib.sha1(q.encode()).hexdigest()   # kv.key is 64 chars at most


def _cache_get(key: str) -> list | None:
    try:
        row = db.one(sa.select(db.kv.c.value).where(db.kv.c.key == key))
    except Exception:
        log.exception("geocode cache read")
        return None
    v = row["value"] if row else None
    if isinstance(v, dict) and isinstance(v.get("results"), list) and time.time() - float(v.get("at") or 0) < CACHE_TTL_S:
        return v["results"]
    return None


def _cache_put(key: str, q: str, results: list) -> None:
    try:
        with db.engine().begin() as c:
            c.execute(sa.delete(db.kv).where(db.kv.c.key == key))
            c.execute(db.kv.insert().values(key=key, value={"at": time.time(), "q": q[:200], "results": results}))
    except Exception:
        log.exception("geocode cache write")


# ---------------------------------------------------------------- what a query looks like

US_STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California", "CO": "Colorado", "CT": "Connecticut",
    "DE": "Delaware", "DC": "District of Columbia", "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois",
    "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri", "MT": "Montana",
    "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania",
    "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah",
    "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
    "PR": "Puerto Rico", "GU": "Guam", "VI": "U.S. Virgin Islands", "AS": "American Samoa", "MP": "Northern Mariana Islands",
}
# "10187 SW…", "12-14 High St", "5B Elm St": digits, then a space, then more address
_HOUSE_NUMBER = re.compile(r"^\s*(\d+[A-Za-z]?(?:[-/]\d+[A-Za-z]?)?)\s+[A-Za-z0-9]")
_ZIP = re.compile(r"(?<!\d)(\d{5})(?:-\d{4})?(?!\d)")
_STATE_TAIL = re.compile(
    r"[\s,](" + "|".join(US_STATES) + "|" + "|".join(re.escape(n) for n in US_STATES.values()) + r")\.?"
    r"(?:[\s,]+\d{5}(?:-\d{4})?)?(?:[\s,]+(?:USA|US|U\.S\.A?\.?|United States))?[\s,]*$", re.I)


def house_number_of(q: str) -> str | None:
    """The leading house number of a street address ("10187 SW US HWY 54…" -> "10187"), else None ("Building 2")."""
    m = _HOUSE_NUMBER.match(q or "")
    return m.group(1) if m else None


def zip_of(q: str) -> str | None:
    """A 5-digit US ZIP after the house number ("… KS 67010" -> "67010")."""
    text = (q or "").strip()
    hn = house_number_of(text)
    rest = text[text.index(hn) + len(hn):] if hn else text
    m = None
    for m in _ZIP.finditer(rest):
        pass
    return m.group(1) if m else None   # the last one: a ZIP comes at the end


def has_us_state(q: str) -> bool:
    return bool(_STATE_TAIL.search(q or ""))


def us_street_first(q: str) -> bool:
    """Census first: a street address (leading house number) that names a US state or ZIP, or any street address on a
    hub whose Sites are in the US (HUB_GEOCODER_COUNTRY, default US)."""
    if not house_number_of(q):
        return False
    return bool(zip_of(q) or has_us_state(q)) or (settings.geocoder_country or "").strip().upper() == "US"


_DIRECTION = r"\b(?:N|S|E|W|NE|NW|SE|SW)\.?\s+"
_HIGHWAY_AHEAD = r"(?=(?:US|U\.S\.|State|SR|SH|Hwy|Highway|Route|Rte|Interstate|I)\b|[A-Z]{2}-\d)"


def nominatim_variants(q: str) -> list[str]:
    """What to ask Nominatim, in order: the text as typed, then (only used when that found nothing) the route as OSM
    names it ("SW US HWY 54" -> "US-54") and HWY spelled out, both without a direction before the highway and without
    the ZIP. Duplicates removed."""
    q = (q or "").strip()
    hn = house_number_of(q) or ""
    base = q[len(hn):] if hn and q.startswith(hn) else q
    base = re.sub(_DIRECTION + _HIGHWAY_AHEAD, "", base, flags=re.I)
    base = _ZIP.sub("", base)
    base = hn + re.sub(r"\s*,\s*(?=,|$)", "", re.sub(r"\s{2,}", " ", base)).rstrip(" ,")
    route = re.sub(r"\b(?:US|U\.S\.)\s+(?:HWY|Highway|Hiway|Route|Rte)\.?\s+(\d+)\b", r"US-\1", base, flags=re.I)
    spelled = re.sub(r"\bHWY\b\.?", "Highway", base, flags=re.I)
    out: list[str] = []
    for v in (q, route, spelled, base):
        v = re.sub(r"\s{2,}", " ", v).strip(" ,")
        if v and v.casefold() not in {o.casefold() for o in out}:
            out.append(v)
    return out


# ---------------------------------------------------------------- provider calls

async def _get(url: str, params: dict, what: str) -> object | None:
    try:
        async with httpx.AsyncClient(transport=transport, timeout=TIMEOUT_S, headers={"User-Agent": USER_AGENT, "Accept-Language": "en"}) as c:
            r = await c.get(url, params=params)
        if r.status_code != 200:
            log.warning("%s: HTTP %s", what, r.status_code)
            return None
        return r.json()
    except Exception as e:   # network, timeout, bad JSON: never the caller's problem
        log.warning("%s failed: %s", what, e)
        return None


async def _fetch(path: str, params: dict) -> object | None:
    """One Nominatim call, spaced at least MIN_INTERVAL_S after the previous one hub-wide. None on any failure."""
    global _last_call
    params = {"format": "jsonv2", "addressdetails": 1, **params}
    if settings.geocoder_key:
        params["key"] = settings.geocoder_key
    async with _hub_lock("nominatim"):
        wait = _last_call + MIN_INTERVAL_S - _clock()
        if wait > 0:
            await _sleep(wait)
        try:
            return await _get(f"{base_url()}/{path}", params, f"geocoder {path}")
        finally:
            _last_call = _clock()


async def _census(address: str) -> object | None:
    """One Census one-line-address lookup, a few a second at most. None on any failure (or when switched off)."""
    global _last_census
    url = census_url()
    if not url:
        return None
    async with _hub_lock("census"):
        wait = _last_census + CENSUS_INTERVAL_S - _clock()
        if wait > 0:
            await _sleep(wait)
        try:
            return await _get(url, {"address": address[:200], "benchmark": "Public_AR_Current", "format": "json"}, "census geocoder")
        finally:
            _last_census = _clock()


# ---------------------------------------------------------------- results

def tz_for(lat: float | None, lon: float | None) -> str | None:
    """The IANA time zone at a point (offline), or None (no point, open sea with only an Etc/ zone, unknown name)."""
    if lat is None or lon is None:
        return None
    try:
        name = _finder().timezone_at(lat=float(lat), lng=float(lon))
    except Exception:
        log.exception("timezonefinder")
        return None
    if not name or name.startswith("Etc/"):
        return None
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return None
    return name


_tf = None


def _finder():
    global _tf
    if _tf is None:
        from timezonefinder import TimezoneFinder
        _tf = TimezoneFinder()
    return _tf


_NUMBER_FIRST = {"us", "ca", "gb", "ie", "au", "nz", "fr", "za", "in", "ph", "sg", "my", "lu", "be"}


def _s(v, n: int = 120) -> str | None:
    return str(v).strip()[:n] or None if v is not None else None


def parts_from(item: dict) -> dict:
    """Nominatim's addressdetails -> our address_parts."""
    a = item.get("address") if isinstance(item.get("address"), dict) else {}
    iso = a.get("ISO3166-2-lvl4") or a.get("ISO3166-2-lvl6") or ""
    return {
        "house_number": _s(a.get("house_number"), 40),
        "street": _s(a.get("road") or a.get("pedestrian") or a.get("footway") or a.get("street") or a.get("square")),
        "city": _s(a.get("city") or a.get("town") or a.get("village") or a.get("hamlet") or a.get("municipality") or a.get("suburb")),
        "county": _s(a.get("county")),
        "state": _s(a.get("state") or a.get("region") or a.get("province")),
        "state_code": _s(iso.split("-", 1)[1], 10) if "-" in iso else None,
        "postcode": _s(a.get("postcode"), 20),
        "country": _s(a.get("country")),
        "country_code": _s((a.get("country_code") or "").upper(), 4),
        "display_name": _s(item.get("display_name"), 300),
    }


def format_address(p: dict) -> str:
    """One line a person would write on an envelope: "1200 Main Street, Augusta, KS 67010, United States"."""
    cc = (p.get("country_code") or "").lower()
    num, street = p.get("house_number"), p.get("street")
    line1 = " ".join(x for x in ((num, street) if cc in _NUMBER_FIRST else (street, num)) if x)
    region = p.get("state_code") if cc in {"us", "ca", "au"} and p.get("state_code") else p.get("state")
    region_line = " ".join(x for x in (region, p.get("postcode")) if x)
    out = ", ".join(x for x in (line1, p.get("city"), region_line, p.get("country")) if x)
    return (out or p.get("display_name") or "")[:200]


def result_from(item: dict) -> dict | None:
    """One Nominatim item -> {display, address_parts, lat, lon, timezone, source}."""
    try:
        lat, lon = float(item["lat"]), float(item["lon"])
    except (KeyError, TypeError, ValueError):
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    parts = parts_from(item)
    return {"display": format_address(parts), "address_parts": parts, "lat": round(lat, 7), "lon": round(lon, 7), "timezone": tz_for(lat, lon),
            "source": source()}


_KEEP_UPPER = {"US", "N", "S", "E", "W", "NE", "NW", "SE", "SW", "PO", "FM", "RR", "CR", "SR", "I"}


def title_case(text: str) -> str:
    """Census writes in capitals: "10187 US HWY 54" -> "10187 US Hwy 54", "1ST ST NE" -> "1st St NE", "AUGUSTA" -> "Augusta"."""
    out = []
    for w in (text or "").split():
        if w.upper() in _KEEP_UPPER or w.isdigit():
            out.append(w.upper())
        elif re.fullmatch(r"\d+(ST|ND|RD|TH)", w, re.I):
            out.append(w.lower())
        else:
            out.append("-".join(p[:1].upper() + p[1:].lower() for p in w.split("-")))
    return " ".join(out)


def census_result_from(m: dict) -> dict | None:
    """One Census addressMatch ("10187 US HWY 54, AUGUSTA, KS, 67010", coordinates x=lon y=lat) -> our result shape."""
    try:
        lon, lat = float(m["coordinates"]["x"]), float(m["coordinates"]["y"])
    except (KeyError, TypeError, ValueError):
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    matched = str(m.get("matchedAddress") or "")
    c = m.get("addressComponents") if isinstance(m.get("addressComponents"), dict) else {}
    bits = [b.strip() for b in matched.split(",")]
    line = bits[0] if bits else ""
    num = house_number_of(line)
    street = line[len(num):].strip() if num else line
    state_code = ((c.get("state") or (bits[2] if len(bits) > 2 else "")) or "").strip().upper()[:10] or None
    parts = {
        "house_number": _s(num, 40), "street": _s(title_case(street)),
        "city": _s(title_case(c.get("city") or (bits[1] if len(bits) > 1 else ""))), "county": None,
        "state": US_STATES.get(state_code or ""), "state_code": state_code,
        "postcode": _s(c.get("zip") or (bits[3] if len(bits) > 3 else ""), 20), "country": "United States", "country_code": "US",
        "display_name": _s(matched, 300),
    }
    return {"display": format_address(parts), "address_parts": parts, "lat": round(lat, 7), "lon": round(lon, 7), "timezone": tz_for(lat, lon),
            "source": "census"}


async def _nominatim_search(q: str, limit: int) -> list[dict] | None:
    """The text as typed, then (when that found nothing) its rewrites. None = the provider failed every time."""
    answered = False
    for v in nominatim_variants(q)[:MAX_VARIANTS]:
        data = await _fetch("search", {"q": v[:200], "limit": limit})
        if not isinstance(data, list):
            continue
        answered = True
        out = [r for r in (result_from(i) for i in data if isinstance(i, dict)) if r]
        if out:
            return out
    return [] if answered else None


async def _census_search(q: str, limit: int) -> list[dict] | None:
    data = await _census(q)
    if not isinstance(data, dict) or not census_url():
        return None
    res = data.get("result")
    matches = res.get("addressMatches") if isinstance(res, dict) else None
    if not isinstance(matches, list):
        return None
    return [r for r in (census_result_from(m) for m in matches if isinstance(m, dict)) if r][:limit]


def provider_order(q: str) -> list[str]:
    """US street address: Census, then Nominatim. Anything else: Nominatim, then Census for a street address (Census
    only knows street addresses)."""
    if us_street_first(q):
        return ["census", "nominatim"]
    return ["nominatim"] + (["census"] if house_number_of(q) else [])


async def search(q: str, limit: int = 5) -> list[dict]:
    """Up to `limit` candidates for a free-text address (cached by normalised query). The first provider (see
    provider_order) with an answer wins."""
    nq = normalise(q)
    if len(nq) < 3:
        return []
    key = _key("s2", f"{base_url()}|{census_url()}|{limit}|{nq}")
    cached = _cache_get(key)
    if cached is not None:
        return cached
    q = q.strip()[:200]
    failed = False
    for name in provider_order(q):
        out = await (_census_search if name == "census" else _nominatim_search)(q, limit)
        if out:
            _cache_put(key, nq, out)
            return out
        failed = failed or out is None
    if not failed:
        _cache_put(key, nq, [])   # every provider answered "nothing": remember that; a failure is not cached
    return []


async def reverse(lat: float, lon: float) -> dict | None:
    """The address at a point (Nominatim; cached by the point rounded to ~1 m)."""
    q = f"{lat:.5f},{lon:.5f}"
    key = _key("r", f"{base_url()}|{q}")
    cached = _cache_get(key)
    if cached is not None:
        return cached[0] if cached else None
    data = await _fetch("reverse", {"lat": f"{lat:.6f}", "lon": f"{lon:.6f}"})
    if not isinstance(data, dict):
        return None
    r = result_from(data) if "error" not in data else None
    _cache_put(key, q, [r] if r else [])
    return r


def place_of(loc: dict) -> dict:
    """Where a Site is, as every Site payload carries it (absent columns read as unknown)."""
    return {"lat": loc.get("lat"), "lon": loc.get("lon"), "address_parts": loc.get("address_parts"),
            "geocoded_at": loc.get("geocoded_at"), "geocode_source": loc.get("geocode_source")}


# ---------------------------------------------------------------- per-user limit on the lookup routes

USER_LIMIT, USER_WINDOW_S = 30, 60
_asks: dict[str, collections.deque] = collections.defaultdict(collections.deque)


def rate_limited(uid: str, now: float | None = None) -> bool:
    """True when this user has asked too often lately; otherwise records this ask."""
    now = now if now is not None else time.time()
    q = _asks[uid]
    while q and q[0] < now - USER_WINDOW_S:
        q.popleft()
    if len(q) >= USER_LIMIT:
        return True
    q.append(now)
    return False


# ---------------------------------------------------------------- backfill

def _try_key(location_id: str) -> str:
    return f"geo:try:{location_id}"


def clean_parts(p) -> dict | None:
    """address_parts as a client may send them: known keys, short strings, nothing else."""
    if not isinstance(p, dict):
        return None
    out = {k: _s(p.get(k), 300 if k == "display_name" else 120) for k in PART_KEYS}
    return out if any(out.values()) else None


def backfill_accepts(address: str, parts: dict | None) -> bool:
    """May an unattended lookup pin this Site? Only for a real street address (it starts with a house number) whose hit
    is just as specific: the hit has a house number, or its postcode is the ZIP the address names."""
    if not house_number_of(address or ""):
        return False
    p = parts if isinstance(parts, dict) else {}
    if (p.get("house_number") or "").strip():
        return True
    z = zip_of(address)
    return bool(z) and (p.get("postcode") or "").strip()[:5] == z


def _heal_once() -> int:
    """Clear the pins the backfill wrote before backfill_accepts existed when they fail it (source nominatim/geocoder;
    marker, manual and census points are kept). Runs once per database (HEAL_KEY). Returns how many were cleared."""
    if db.one(sa.select(db.kv.c.key).where(db.kv.c.key == HEAL_KEY)):
        return 0
    L = db.locations
    rows = db.rows(sa.select(L.c.id, L.c.address, L.c.address_parts).where(L.c.lat.is_not(None), L.c.geocode_source.in_(["nominatim", "geocoder"])))
    bad = [r["id"] for r in rows if not backfill_accepts(r["address"] or "", r["address_parts"])]
    with db.engine().begin() as c:
        if bad:
            c.execute(sa.update(L).where(L.c.id.in_(bad)).values(lat=None, lon=None, address_parts=None, geocoded_at=None, geocode_source=None))
            # drop their attempt notes: this very pass tries them again, under the guard
            for lid in bad:
                c.execute(sa.delete(db.kv).where(db.kv.c.key == _try_key(lid)))
        c.execute(db.kv.insert().values(key=HEAL_KEY, value={"at": time.time(), "cleared": bad}))
    if bad:
        log.warning("geocode: cleared %d untrustworthy pin(s) from an earlier backfill: %s", len(bad), bad)
    return len(bad)


async def backfill(force: bool = False, now: float | None = None, ids: list[str] | None = None) -> dict:
    """Geocode Sites that have an address but no coordinates. Sets lat/lon/address_parts (never the typed address,
    never an existing time zone; an empty one is filled from the point), and only for a hit backfill_accepts. Each
    attempt is noted in kv; a failed address is skipped for RETRY_AFTER_S unless `force` or the address changed since."""
    now = now if now is not None else time.time()
    healed = _heal_once()
    L = db.locations
    q = sa.select(L.c.id, L.c.address, L.c.timezone).where(L.c.lat.is_(None), L.c.address != "")
    todo = db.rows(q.where(L.c.id.in_(ids)) if ids is not None else q)
    tries = {r["key"]: r["value"] for r in db.rows(sa.select(db.kv).where(db.kv.c.key.in_([_try_key(t["id"]) for t in todo])))} if todo else {}
    stats = {"located": 0, "failed": 0, "skipped": 0, "cleared": healed}
    for loc in todo:
        addr = (loc["address"] or "").strip()
        prev = tries.get(_try_key(loc["id"])) or {}
        if not addr or (not force and prev.get("address") == addr and now - float(prev.get("at") or 0) < RETRY_AFTER_S):
            stats["skipped"] += 1
            continue
        hit = None
        if house_number_of(addr):   # no house number ("Building 2"): nothing could be trusted, so don't even ask
            hits = await search(addr)   # same query (and cache entry) the Settings lookup uses
            hit = next((h for h in hits if backfill_accepts(addr, h.get("address_parts"))), None)
        with db.engine().begin() as c:
            c.execute(sa.delete(db.kv).where(db.kv.c.key == _try_key(loc["id"])))
            c.execute(db.kv.insert().values(key=_try_key(loc["id"]), value={"at": now, "address": addr, "ok": bool(hit)}))
            if hit:
                vals = {"lat": hit["lat"], "lon": hit["lon"], "address_parts": hit["address_parts"], "geocoded_at": now,
                        "geocode_source": hit.get("source") or source()}
                if not (loc["timezone"] or "").strip() and hit.get("timezone"):
                    vals["timezone"] = hit["timezone"]
                # the row may have changed while we waited on the provider: only fill it if it is still unlocated at that address
                c.execute(sa.update(L).where(L.c.id == loc["id"], L.c.lat.is_(None), L.c.address == loc["address"]).values(**vals))
        stats["located" if hit else "failed"] += 1
    if todo or healed:
        log.info("geocode backfill: %s", stats)
    return stats


def locate_soon(location_id: str) -> None:
    """A Site was saved with an address and no point: try to locate it in the background (same rules as backfill)."""
    if not settings.geocode_backfill:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return

    async def run():
        try:
            await backfill(ids=[location_id])
        except Exception:
            log.exception("geocode %s", location_id)
    loop.create_task(run())


async def backfill_once_at_start(delay_s: float = 10.0) -> None:
    await asyncio.sleep(delay_s)   # let the hub settle (tunnels reconnecting) before talking to the outside world
    try:
        await backfill()
    except Exception:
        log.exception("geocode backfill")
