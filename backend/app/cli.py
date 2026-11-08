"""Server-side admin commands (run on the server, never exposed over HTTP).

    python -m app.cli sync-permissions
    python -m app.cli bootstrap --company-code ACME --company-name "Acme Sdn Bhd" \
        --hq-code HQ --hq-name "Head Office" --admin boss
    python -m app.cli set-password boss
    python -m app.cli devices                 # list devices
    python -m app.cli authorize-device <id>   # the owner's first computer

Passwords are read from ERP_ADMIN_PASSWORD or prompted (never a CLI argument,
so they do not end up in shell history).
"""

import argparse
import getpass
import os
import sys
import uuid
from datetime import UTC, datetime

from sqlalchemy import select

from app.core.security import hash_password
from app.db.session import SessionLocal
from app.models import AppUser, Company, Device
from app.services.auth import revoke_sessions
from app.services.seed import bootstrap, sync_permissions, sync_system_tags

MIN_PASSWORD = 10


def _password() -> str:
    pw = os.environ.get("ERP_ADMIN_PASSWORD")
    if not pw:
        pw = getpass.getpass("Password: ")
        if pw != getpass.getpass("Repeat: "):
            sys.exit("Passwords do not match")
    if len(pw) < MIN_PASSWORD:
        sys.exit(f"Password must be at least {MIN_PASSWORD} characters")
    return pw


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="app.cli")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("sync-permissions", help="Sync permission codes and built-in tags")

    b = sub.add_parser("bootstrap", help="Create company, HQ location and the owner")
    b.add_argument("--company-code", required=True)
    b.add_argument("--company-name", required=True)
    b.add_argument("--hq-code", default="HQ")
    b.add_argument("--hq-name", default="Head Office")
    b.add_argument("--admin", default="admin")

    sp = sub.add_parser("set-password", help="Set a user's password (signs them out)")
    sp.add_argument("username")

    sub.add_parser("devices", help="List devices")
    ad = sub.add_parser("authorize-device", help="Allow logins from a device")
    ad.add_argument("device_id", type=uuid.UUID)

    args = parser.parse_args(argv)
    with SessionLocal() as db:
        if args.cmd == "sync-permissions":
            sync_permissions(db)
            for company in db.scalars(select(Company)):
                sync_system_tags(db, company)
            db.commit()
            print("Permissions and built-in tags synced.")

        elif args.cmd == "bootstrap":
            user = bootstrap(
                db,
                company_code=args.company_code,
                company_name=args.company_name,
                hq_code=args.hq_code,
                hq_name=args.hq_name,
                admin_username=args.admin,
                admin_password=_password(),
            )
            print(f"Ready. Owner user: {user.username}")

        elif args.cmd == "set-password":
            user = db.scalar(select(AppUser).where(AppUser.username == args.username))
            if user is None:
                sys.exit(f"No user {args.username!r}")
            user.password_hash = hash_password(_password())
            revoke_sessions(db, user_id=user.id)
            db.commit()
            print(f"Password updated for {args.username}")

        elif args.cmd == "devices":
            for d in db.scalars(select(Device).order_by(Device.last_seen_at.desc())):
                flag = "OK" if d.is_authorized else "PENDING"
                print(f"{d.id}  {flag:7}  {d.last_seen_at:%Y-%m-%d %H:%M}  {d.name}")

        elif args.cmd == "authorize-device":
            d = db.get(Device, args.device_id)
            if d is None:
                sys.exit("No such device")
            d.is_authorized = True
            d.authorized_at = datetime.now(UTC)
            db.commit()
            print(f"Device {d.name} is authorized; users can now log in from it.")


if __name__ == "__main__":
    main()
