"""Named layouts of a Site, kept on the hub because one Site's layout spans its servers: a server's own /api/layouts
only knows its own cameras.

Private to the user who saved them, per Site: one list shared by the Site's Live grid and its Timeline, so a layout
saved on either page is picked on both. A layout is {id, name, config, created_at, updated_at}, the shape of a
server's /api/layouts, so the UI's layout pickers read both alike. `config` is the union of what the two pages keep:
the Timeline's LayoutConfig ({visible, solo, order}, camera keys "<server>/<camera>" on both pages) plus Live's
per-camera stream quality, grid columns (null = Auto) and whether the activity column shows. Each page sends only the
fields it owns; an update merges them into the stored config, so a save on Live keeps the Timeline's solo and a save
on the Timeline keeps Live's quality, columns and activity column.
Rows are stored with view "live" (the list began as Live's); the `view` a client names is accepted and ignored.
Not audited: like a private dashboard, a personal arrangement of cameras changes nothing anyone else sees.
"""
from __future__ import annotations

import time
from typing import Literal

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from . import db

View = Literal["live", "timeline"]
STORED_VIEW = "live"      # every row's view: one list per user and Site (an older hub lists it on Live)
MAX_LAYOUTS = 50          # per user and Site
MAX_KEYS = 500            # cameras named in one list (a Site's cameras across all its servers)
KEY_LEN = 120             # "<server id>/<camera id>"


class LayoutConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")
    visible: list[str] | None = Field(default=None, max_length=MAX_KEYS)
    solo: str | None = Field(default=None, max_length=KEY_LEN)
    order: list[str] | None = Field(default=None, max_length=MAX_KEYS)
    quality: dict[str, Literal["sd", "hd"]] | None = None
    cols: int | None = Field(default=None, ge=1, le=4)
    activity: bool | None = None


def clean_config(cfg: dict) -> dict:
    """The config as stored: known fields only (those sent), bounded lists of non-empty keys. ValueError otherwise."""
    try:
        c = LayoutConfig.model_validate(cfg)
    except ValidationError as e:
        err = e.errors()[0]
        where = ".".join(str(x) for x in err.get("loc", ()))
        raise ValueError(f"{where}: {err.get('msg', 'invalid')}" if where else err.get("msg", "invalid layout")) from e
    out = c.model_dump(exclude_unset=True)
    for k in ("visible", "order"):
        keys = out.get(k)
        if keys is not None:
            if any(not x or len(x) > KEY_LEN for x in keys):
                raise ValueError(f"{k}: camera keys are 1-{KEY_LEN} characters")
            out[k] = list(dict.fromkeys(keys))   # duplicates would only confuse the order
    q = out.get("quality")
    if q is not None:
        if len(q) > MAX_KEYS:
            raise ValueError(f"quality: at most {MAX_KEYS} cameras")
        if any(not k or len(k) > KEY_LEN for k in q):
            raise ValueError(f"quality: camera keys are 1-{KEY_LEN} characters")
    return out


def _public(r: dict) -> dict:
    return {k: r[k] for k in ("id", "name", "config", "created_at", "updated_at")}


def _mine(u: dict, location_id: str):
    """This user's layouts of this Site, whatever view they were saved from (the list is shared by Live and Timeline)."""
    t = db.site_layouts.c
    return sa.and_(t.user_id == u["id"], t.location_id == location_id)


def list_for(u: dict, location_id: str) -> list[dict]:
    rows = db.rows(sa.select(db.site_layouts).where(_mine(u, location_id)))
    rows.sort(key=lambda r: (r["name"].lower(), r["id"]))
    return [_public(r) for r in rows]


def get(u: dict, location_id: str, layout_id: int) -> dict | None:
    """One of this user's layouts at this Site; anyone else's (or another Site's) is None."""
    t = db.site_layouts.c
    return db.one(sa.select(db.site_layouts).where(t.id == layout_id, t.user_id == u["id"], t.location_id == location_id))


class NameTaken(ValueError):
    pass


class TooMany(ValueError):
    pass


def _name_taken(u: dict, location_id: str, name: str, exclude: int | None = None) -> bool:
    t = db.site_layouts.c
    q = sa.select(t.id).where(_mine(u, location_id), sa.func.lower(t.name) == name.lower())
    if exclude is not None:
        q = q.where(t.id != exclude)
    return db.one(q) is not None


def create(u: dict, loc: dict, name: str, config: dict) -> dict:
    name = name.strip()
    cfg = clean_config(config)
    if _name_taken(u, loc["id"], name):
        raise NameTaken("a layout with that name already exists")
    n = db.one(sa.select(sa.func.count().label("n")).select_from(db.site_layouts).where(_mine(u, loc["id"])))
    if n and n["n"] >= MAX_LAYOUTS:
        raise TooMany(f"at most {MAX_LAYOUTS} layouts per site: delete one first")
    now = time.time()
    row = {"location_id": loc["id"], "org_id": loc["org_id"], "user_id": u["id"], "view": STORED_VIEW, "name": name, "config": cfg,
           "created_at": now, "updated_at": now}
    with db.engine().begin() as c:
        lid = int(c.execute(db.site_layouts.insert().values(**row)).inserted_primary_key[0])
    return _public({**row, "id": lid})


def merged_config(stored: dict | None, config: dict) -> dict:
    """An update's config: the fields sent replace the stored ones, the rest (another page's) stay. ValueError if invalid."""
    return {**(stored if isinstance(stored, dict) else {}), **clean_config(config)}


def update(u: dict, row: dict, name: str, config: dict) -> dict:
    name = name.strip()
    cfg = merged_config(row.get("config"), config)
    if _name_taken(u, row["location_id"], name, exclude=row["id"]):
        raise NameTaken("a layout with that name already exists")
    db.run(sa.update(db.site_layouts).where(db.site_layouts.c.id == row["id"]).values(name=name, config=cfg, updated_at=time.time()))
    return _public(db.one(sa.select(db.site_layouts).where(db.site_layouts.c.id == row["id"])))


def delete(row: dict) -> None:
    db.run(sa.delete(db.site_layouts).where(db.site_layouts.c.id == row["id"]))


def drop_location(conn, location_id: str) -> None:
    """The Site is deleted: everyone's layouts of it go with it (their camera keys name its servers)."""
    conn.execute(sa.delete(db.site_layouts).where(db.site_layouts.c.location_id == location_id))
