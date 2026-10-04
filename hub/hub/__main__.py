"""python -m hub            serve the hub
   python -m hub createsuper EMAIL [--password PW]   create a hub administrator (or read HUB_ADMIN_PASSWORD)
   python -m hub setpassword EMAIL                   change a user's password (prompts twice; ends their sessions)
   python -m hub createorg NAME SLUG                 create an organisation
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
        print("created organisation", o["id"], o["slug"])
        return

    import uvicorn
    uvicorn.run("hub.api:app", host=settings.host, port=settings.port, log_level=settings.log_level, proxy_headers=True, forwarded_allow_ips="*")


if __name__ == "__main__":
    main()
