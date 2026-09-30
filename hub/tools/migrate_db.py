"""Copy one hub database into another, table by table (e.g. the dev SQLite hub.db into the production Postgres).

    python -m tools.migrate_db sqlite:////data/hub.db "postgresql+psycopg://hub:PW@postgres:5432/hub"
    (inside the hub container: docker compose -f hub/docker-compose.yml exec hub python /app/hub/tools/migrate_db.py ...)

The schema is plain SQLAlchemy Core (hub/hub/db.py), created on the target with create_all. Rows are copied in
foreign-key order and existing rows are left alone (ON CONFLICT DO NOTHING on Postgres), so re-running is safe.
Integer primary-key sequences are bumped past the copied ids. Site ids and device-token hashes come across
unchanged, so enrolled sites reconnect to the new hub without a new claim.
"""
from __future__ import annotations

import sys

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql, sqlite

from hub.db import metadata


def copy(src_url: str, dst_url: str, batch: int = 500) -> None:
    src, dst = sa.create_engine(src_url), sa.create_engine(dst_url)
    metadata.create_all(dst)
    with src.connect() as s, dst.begin() as d:
        for table in metadata.sorted_tables:
            rows = [dict(r._mapping) for r in s.execute(sa.select(table))]
            n = 0
            for i in range(0, len(rows), batch):
                chunk = rows[i:i + batch]
                if d.dialect.name == "postgresql":
                    stmt = postgresql.insert(table).values(chunk).on_conflict_do_nothing()
                elif d.dialect.name == "sqlite":
                    stmt = sqlite.insert(table).values(chunk).on_conflict_do_nothing()
                else:
                    stmt = table.insert().values(chunk)
                n += d.execute(stmt).rowcount or 0
            print(f"{table.name:20s} {len(rows):6d} rows read, {n:6d} inserted")
            if d.dialect.name == "postgresql":
                for col in table.primary_key.columns:
                    if isinstance(col.type, sa.Integer) and col.autoincrement in (True, "auto"):
                        d.execute(sa.text(f"SELECT setval(pg_get_serial_sequence('{table.name}', '{col.name}'), "
                                          f"COALESCE((SELECT MAX({col.name}) FROM {table.name}), 0) + 1, false)"))


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(2)
    copy(sys.argv[1], sys.argv[2])
    print("done")
