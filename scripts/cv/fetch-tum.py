#!/usr/bin/env python3
"""Fetch one official TUM sequence; retain only RGB images and rgb.txt."""
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import tarfile
import urllib.request
import sys

root = Path(sys.argv[1]).resolve()
root.mkdir(parents=True, exist_ok=True)
name = 'rgbd_dataset_freiburg1_xyz'
url = 'https://webshare.cvg.cit.tum.de/g/rgbd/dataset/freiburg1/' + name + '.tgz'
archive = root / (name + '.tgz')
if not archive.is_file():
    temporary = archive.with_suffix('.part')
    with urllib.request.urlopen(url, timeout=120) as source, temporary.open('wb') as target:
        shutil.copyfileobj(source, target)
    temporary.rename(archive)
with archive.open('rb') as downloaded:
    digest = hashlib.file_digest(downloaded, 'sha256').hexdigest()
expected = 'a0236d97b8c30cd93b653656d2b6c293ff7c982a4130ef2a1a8beecdb124ef98'
if digest != expected:
    raise RuntimeError('TUM archive checksum mismatch: ' + digest)
sequence = root / name
def cache_complete():
    index = sequence / 'rgb.txt'
    if not (root / 'tum-source.json').is_file() or not index.is_file():
        return False
    rows = [row.split() for row in index.read_text().splitlines() if row.strip() and not row.startswith('#')]
    if not rows:
        return False
    for row in rows:
        if len(row) != 2:
            return False
        relative = PurePosixPath(row[1])
        if relative.is_absolute() or '..' in relative.parts or relative.parts[0] != 'rgb':
            raise RuntimeError('Unsafe RGB index path')
        image = sequence.joinpath(*relative.parts)
        if not image.is_file() or image.stat().st_size == 0:
            return False
    return True

if not cache_complete():
    with tarfile.open(archive, 'r:gz') as source:
        for member in source:
            path = PurePosixPath(member.name)
            if path.is_absolute() or '..' in path.parts:
                raise RuntimeError('Unsafe archive member')
            if not member.isfile() or len(path.parts) < 2 or path.parts[0] != name:
                continue
            if not (path.parts[1] == 'rgb' or path.parts[1:] == ('rgb.txt',)):
                continue
            destination = root.joinpath(*path.parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            with source.extractfile(member) as image, destination.open('wb') as output:
                shutil.copyfileobj(image, output)
manifest = {'name': name, 'url': url, 'archive_sha256': digest,
            'extracted': ['rgb/', 'rgb.txt'], 'depth_and_ground_truth_extracted': False}
(root / 'tum-source.json').write_text(json.dumps(manifest, indent=2) + '\n')
print(json.dumps(manifest, indent=2))
