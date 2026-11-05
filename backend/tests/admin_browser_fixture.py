"""Configure ONLY the explicit disposable auth fixture for admin browser QA."""
import argparse
import os
from pathlib import Path
import subprocess
import sys
from mtw.config import DATA,REPO
from mtw.files import read_json


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--root',type=Path,required=True);a=p.parse_args()
    root=a.root.resolve();control=read_json(root/'control.json')
    assert root.parent==(DATA/'reports').resolve() and root.name.startswith('auth-browser-') and len(root.name)==45
    assert control['schema']=='mtw_browser_'+root.name.removeprefix('auth-browser-')
    assert control['base']=='https://127.0.0.1:8444'
    env=os.environ|{'MTW_CONFIG':str(root/'config.toml')}
    subprocess.run(['bash',str(REPO/'scripts/admin-role'),'--login','browser_a','--grant'],env=env,check=True)
    # The disposable admin reads live project telemetry, never production sessions.
    (root/'monitoring').symlink_to(DATA/'monitoring',target_is_directory=True)
    print('Disposable admin role and read-only monitoring view configured; public roles unchanged')
