"""Explicit, local-only ownership assignment; never called by registration."""
import argparse
import json
from sqlalchemy import select, update
from .db import Job, Session, User


def assign_unowned(login, sessions=Session, dry_run=False):
    with sessions.begin() as session:
        user = session.scalar(select(User).where(User.login == login.strip().lower()))
        if user is None:
            raise ValueError("The specified account does not exist; no jobs assigned")
        if dry_run:
            ids = list(session.scalars(select(Job.id).where(Job.owner_id.is_(None))))
        else:
            ids = list(session.scalars(update(Job).where(Job.owner_id.is_(None))
                                      .values(owner_id=user.id).returning(Job.id)))
        return {"login": user.login, "user_id": str(user.id), "dry_run": dry_run,
                "unowned_jobs": len(ids), "assigned": 0 if dry_run else len(ids)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--login", required=True, help="Existing owner account; never inferred")
    parser.add_argument("--all-unowned", required=True, action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        print(json.dumps(assign_unowned(args.login, dry_run=args.dry_run)))
    except ValueError as error:
        parser.exit(2, str(error) + "\n")


if __name__ == "__main__":
    main()
