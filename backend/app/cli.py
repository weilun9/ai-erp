"""Admin commands.

    python -m app.cli sync-permissions
    python -m app.cli bootstrap --company-code ACME --company-name "Acme Sdn Bhd" \
        --hq-code HQ --hq-name "Head Office" --admin admin
    python -m app.cli set-password admin

The admin password is read from ERP_ADMIN_PASSWORD or prompted (never a CLI argument,
so it does not end up in shell history).
"""

import argparse
import getpass
import os
import sys

from sqlalchemy import select

from app.core.security import hash_password
from app.db.session import SessionLocal
from app.models import AppUser, Company
from app.services.seed import bootstrap, sync_permissions, sync_system_roles


def _password() -> str:
    pw = os.environ.get("ERP_ADMIN_PASSWORD")
    if pw:
        return pw
    pw = getpass.getpass("Admin password: ")
    if pw != getpass.getpass("Repeat: "):
        sys.exit("Passwords do not match")
    return pw


def _set_password(username: str) -> None:
    with SessionLocal() as db:
        user = db.scalar(select(AppUser).where(AppUser.username == username))
        if user is None:
            sys.exit(f"No user {username!r}")
        pw = _password()
        if len(pw) < 10:
            sys.exit("Password must be at least 10 characters")
        user.password_hash = hash_password(pw)
        db.commit()
    print(f"Password updated for {username}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="app.cli")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("sync-permissions", help="Sync permission codes and system roles")

    b = sub.add_parser("bootstrap", help="Create company, HQ location and first admin")
    b.add_argument("--company-code", required=True)
    b.add_argument("--company-name", required=True)
    b.add_argument("--hq-code", default="HQ")
    b.add_argument("--hq-name", default="Head Office")
    b.add_argument("--admin", default="admin")

    sp = sub.add_parser("set-password", help="Set a user's password")
    sp.add_argument("username")

    args = parser.parse_args(argv)
    if args.cmd == "set-password":
        _set_password(args.username)
        return
    with SessionLocal() as db:
        if args.cmd == "sync-permissions":
            sync_permissions(db)
            for company in db.scalars(select(Company)):
                sync_system_roles(db, company)
            db.commit()
            print("Permissions and system roles synced.")
        elif args.cmd == "bootstrap":
            pw = _password()
            if len(pw) < 10:
                sys.exit("Password must be at least 10 characters")
            user = bootstrap(
                db,
                company_code=args.company_code,
                company_name=args.company_name,
                hq_code=args.hq_code,
                hq_name=args.hq_name,
                admin_username=args.admin,
                admin_password=pw,
            )
            print(f"Ready. Admin user: {user.username}")


if __name__ == "__main__":
    main()
