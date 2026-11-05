"""Local operator only: grant/revoke the admin role for an explicit existing login."""
import argparse
import json
from sqlalchemy import select
from .db import Session,User


def set_role(login,admin,sessions=Session):
    with sessions.begin() as s:
        user=s.scalar(select(User).where(User.login==login).with_for_update())
        if user is None: raise ValueError('Specified existing login was not found; no role changed')
        previous=user.is_admin
        user.is_admin=admin
        return {'login':user.login,'was_admin':previous,'is_admin':admin}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--login',required=True)
    group=p.add_mutually_exclusive_group(required=True)
    group.add_argument('--grant',action='store_true');group.add_argument('--revoke',action='store_true')
    a=p.parse_args()
    try: print(json.dumps(set_role(a.login,a.grant)))
    except ValueError as e: p.exit(2,str(e)+'\n')


if __name__=='__main__': main()
