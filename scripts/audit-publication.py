"""Read-only audit of every file version in local, unpublished commits.

Reports paths/commits/rules only, never matching credential values. This is a
targeted publication check, not a proof that arbitrary secrets cannot exist.
"""
import argparse
import json
from pathlib import Path
import re
import subprocess

CERTIFICATE_PAYLOAD = rb'-----BEGIN CERTIFICATE-----\r?\n[A-Za-z0-9+/=\r\n]{64,}-----END CERTIFICATE-----'


def git(*args):
    return subprocess.check_output(['git',*args])


def audit(base):
    head=git('rev-parse','HEAD').decode().strip()
    commits=git('rev-list','--reverse',base+'..HEAD').decode().splitlines()
    blobs={};authors=[]
    for commit in commits:
        authors.append(dict(zip(('commit','author','committer'),git('show','-s','--format=%H%n%an <%ae>%n%cn <%ce>',commit).decode().splitlines())))
        for entry in git('ls-tree','-r','-l','-z',commit).split(b'\0'):
            if not entry:continue
            meta,name=entry.split(b'\t',1);_,kind,oid,size=meta.split()
            if kind!=b'blob':continue
            item=blobs.setdefault(oid.decode(),{'commit':commit,'paths':set(),'bytes':int(size)})
            item['paths'].add(name.decode('utf-8'))
    oids=list(blobs)
    batch=subprocess.run(['git','cat-file','--batch'],input=('\n'.join(oids)+'\n').encode(),stdout=subprocess.PIPE,check=True).stdout
    offset=0;findings=[];large=[]
    patterns={
        'private_key_header':rb'-----BEGIN (?:RSA |EC |OPENSSH |DSA |ENCRYPTED )?PRIVATE KEY-----',
        'certificate_payload':CERTIFICATE_PAYLOAD,
        'credential_in_database_url':rb'postgres(?:ql)?(?:\+psycopg)?://[^\s/:]+:[^\s/@]+@',
        'github_token':rb'\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{50,})\b',
        'provider_token':rb'\b(?:AKIA[0-9A-Z]{16}|sk-(?:proj-)?[A-Za-z0-9_-]{35,})\b',
    }
    sensitive={'service-key','proxy-key','database-url','admin-password','credentials.json','session.json','.env','fixture-seed'}
    forbidden={'.mp4','.avi','.mkv','.mov','.vhdx','.db','.sqlite','.sqlite3','.dump','.o','.so','.a','.exe','.dll'}
    for oid in oids:
        end=batch.index(b'\n',offset);header=batch[offset:end].split();size=int(header[2])
        data=batch[end+1:end+1+size];offset=end+size+2
        item=blobs[oid];paths=sorted(item['paths']);rules=[]
        if any(Path(p).name in sensitive for p in paths):rules.append('sensitive_filename')
        if any(Path(p).suffix.lower() in forbidden or any(x in Path(p).parts for x in ('pgdata','node_modules','build','dist')) for p in paths):rules.append('media_database_or_build_path')
        if data.startswith((b'PGDMP',b'SQLite format 3\0',b'\x7fELF',b'MZ')):rules.append('database_or_executable_signature')
        # Test/source strings may intentionally contain example URLs; review hits.
        for rule,pattern in patterns.items():
            if re.search(pattern,data):rules.append(rule)
        if rules:findings.append({'paths':paths,'first_unpublished_commit':item['commit'],'rules':rules,'blob':oid})
        if size>1024**2:large.append({'paths':paths,'first_unpublished_commit':item['commit'],'bytes':size})
    return {'base_ref':base,'base_commit':git('rev-parse',base).decode().strip(),'head':head,
        'commits':len(commits),'unique_blobs':len(blobs),'findings':findings,'files_over_1_MiB':large,
        'largest_blobs':sorted([{'paths':sorted(v['paths']),'bytes':v['bytes']} for v in blobs.values()],key=lambda v:v['bytes'],reverse=True)[:10],
        'authors_and_committers':authors,'working_tree':git('status','--porcelain').decode('utf-8'),
        'scope':'Every tree/blob version in base..HEAD; no network or mutations; no arbitrary-secret guarantee.'}


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--base',default='target/main');parser.add_argument('--report',type=Path,required=True)
    args=parser.parse_args();result=audit(args.base);args.report.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({k:result[k] for k in ('base_commit','head','commits','unique_blobs','findings','files_over_1_MiB')},ensure_ascii=False))
