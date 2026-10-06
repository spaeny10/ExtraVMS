"""python -m hub            serve the hub
   python -m hub createsuper EMAIL [--password PW]   create a hub administrator (or read HUB_ADMIN_PASSWORD)
   python -m hub setpassword EMAIL                   change a user's password (prompts twice; ends their sessions)
   python -m hub setsuper EMAIL [--off]              make an existing user a hub administrator (--off: revoke)
   python -m hub setsoc EMAIL operator|supervisor|off   give an existing user a SOC role (off: remove it)
   python -m hub createorg NAME SLUG                 create an organization
   python -m hub geocode [--force]                   locate Sites that have an address but no coordinates (1 request/s)
"""
from __future__ import annotations

import argparse
import getpass
import logging
import os
import sys

from .config import settings


def main() -> None:
    ap = argparse.ArgumentParser(prog="hub")
    sub = ap.add_subparsers(dest="cmd")
    cs = sub.add_parser("createsuper")
    cs.add_argument("email")
    cs.add_argument("--password")
    sp = sub.add_parser("setpassword", help="set a user's password (prompts; signs them out everywhere)")
    sp.add_argument("email")
    ss = sub.add_parser("setsuper", help="grant (or --off: revoke) hub administrator for an existing user")
    ss.add_argument("email")
    ss.add_argument("--off", action="store_true", help="revoke instead of grant")
    sc = sub.add_parser("setsoc", help="give an existing user a SOC role (operator, supervisor) or take it away (off)")
    sc.add_argument("email")
    sc.add_argument("role", choices=["operator", "supervisor", "off"])
    gc = sub.add_parser("geocode", help="locate Sites that have an address but no coordinates (as the hub does at start)")
    gc.add_argument("--force", action="store_true", help="also retry addresses that failed in the last 24 h")
    co = sub.add_parser("createorg")
    co.add_argument("name")
    co.add_argument("slug")
    co.add_argument("--owner", help="email of an existing user to make owner")
    args = ap.parse_args()
    logging.basicConfig(level=settings.log_level.upper(), format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    if args.cmd == "createsuper":
        from . import auth, db
        db.engine()
        pw = args.password or os.environ.get("HUB_ADMIN_PASSWORD") or getpass.getpass("Password: ")
        if len(pw) < 10:
            sys.exit("password must be at least 10 characters")
        u = auth.create_user(args.email, pw, is_super=True)
        print("created hub administrator", u["email"])
        return
    if args.cmd == "setpassword":
        from . import auth, db
        db.engine()
        pw = getpass.getpass("New password: ")
        if pw != getpass.getpass("Again: "):
            sys.exit("passwords differ")
        try:
            u = auth.set_password(args.email, pw)
        except ValueError as e:
            sys.exit(str(e))
        print("password set for", u["email"], "- existing sessions ended")
        return
    if args.cmd == "setsuper":
        from . import auth, db
        db.engine()
        try:
            u, changed = auth.set_super(args.email, not args.off)
        except (LookupError, ValueError) as e:
            sys.exit(str(e))
        if args.off:
            print(f"{u['email']} is {'no longer' if changed else 'already not'} a hub administrator")
        else:
            print(f"{u['email']} is {'now' if changed else 'already'} a hub administrator")
        return
    if args.cmd == "setsoc":
        from . import auth, db
        db.engine()
        role = None if args.role == "off" else args.role
        try:
            u, changed = auth.set_soc_role(args.email, role)
        except (LookupError, ValueError) as e:
            sys.exit(str(e))
        what = f"SOC {role}" if role else "not SOC staff"
        print(f"{u['email']} is {'now' if changed else 'already'} {what}")
        return
    if args.cmd == "geocode":
        import asyncio

        from . import db, geocode
        db.engine()
        st = asyncio.run(geocode.backfill(force=args.force))
        print(f"located {st['located']}, not found or not trustworthy {st['failed']}, skipped {st['skipped']} (failed in the last 24 h; --force retries)"
              + (f", cleared {st['cleared']} untrustworthy earlier pin(s)" if st.get("cleared") else ""))
        return
    if args.cmd == "createorg":
        import time

        import sqlalchemy as sa

        from . import auth, db
        db.engine()
        o = {"id": db.new_id("o_"), "name": args.name, "slug": args.slug, "created_at": time.time(), "branding": None, "ai_shared": False}
        db.insert(db.orgs, o)
        if args.owner:
            u = auth.user_by_email(args.owner)
            if not u:
                sys.exit("no such user")
            db.insert(db.memberships, {"user_id": u["id"], "org_id": o["id"], "role": "owner"})
        print("created organization", o["id"], o["slug"])
        return

    import uvicorn
    # ws_ping_timeout: a site busy pushing playback chunks answers pings late; uvicorn's 20 s default dropped a live tunnel
    # (Oct 5 2026) and every proxied request failed until it reconnected. 90 s rides out a saturated uplink.
    trusted = [h.strip() for h in settings.forwarded_allow_ips.split(",") if h.strip() and h.strip() != "*"] or ["127.0.0.1"]
    logging.getLogger("hub").info("trusting X-Forwarded-* from %s", ", ".join(trusted))
    uvicorn.run("hub.api:app", host=settings.host, port=settings.port, log_level=settings.log_level, proxy_headers=True,
                forwarded_allow_ips=",".join(trusted), ws_ping_interval=20, ws_ping_timeout=90)


if __name__ == "__main__":
    main()
