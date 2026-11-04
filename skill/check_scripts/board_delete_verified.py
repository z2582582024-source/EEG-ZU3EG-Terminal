#!/usr/bin/env python3
"""Board side of the task-4 cleanup (class A).  usage: board_delete_verified.py <root> <expected.sha> <dirs.txt> [--apply]
Every listed file must exist with the expected SHA-256, and every listed directory must hold exactly the listed files (no extra
file, no symlink).  Any difference aborts the whole batch before anything is deleted.  With --apply the files are removed and then
the directories that became empty (inside the listed directories only)."""
import hashlib
import os
import shutil
import sys

root, expected_path, dirs_path = sys.argv[1:4]
apply = '--apply' in sys.argv
links = {}      # optional --links=<file>: "relpath -> target" lines; only the link itself is removed, never its target
for arg in sys.argv[4:]:
    if arg.startswith('--links='):
        for line in open(arg.split('=', 1)[1], encoding='utf-8'):
            rel, target = line.rstrip('\n').split(' -> ', 1)
            links[rel] = target
expected = {}
for line in open(expected_path, encoding='utf-8'):
    digest, rel = line.rstrip('\n').split('  ', 1)
    expected[rel] = digest
dirs = [d for d in open(dirs_path, encoding='utf-8').read().splitlines() if d]

problems = []
for rel, digest in expected.items():
    path = os.path.join(root, rel)
    if os.path.islink(path) or not os.path.isfile(path):
        problems.append(('MISSING_OR_NOT_FILE', rel))
        continue
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    if h.hexdigest() != digest:
        problems.append(('SHA_CHANGED', rel))
for rel, target in links.items():
    path = os.path.join(root, rel)
    if not os.path.islink(path) or os.readlink(path) != target:
        problems.append(('LINK_MISSING_OR_CHANGED', rel))
for d in dirs:
    for dirpath, dirnames, filenames in os.walk(os.path.join(root, d)):
        for name in filenames + [n for n in dirnames if os.path.islink(os.path.join(dirpath, n))]:
            rel = os.path.relpath(os.path.join(dirpath, name), root)
            if rel not in expected and rel not in links:
                problems.append(('EXTRA_ENTRY', rel))
total = sum(os.path.getsize(os.path.join(root, r)) for r in expected if os.path.isfile(os.path.join(root, r)))
print('files', len(expected), 'bytes', total, 'dirs', len(dirs), 'problems', len(problems))
for p in problems[:20]:
    print('  ', *p)
if problems:
    print('ABORT_NOTHING_DELETED')
    sys.exit(2)
if not apply:
    print('VERIFIED_DRY_RUN')
    sys.exit(0)
for rel in expected:
    os.remove(os.path.join(root, rel))
for rel in links:
    os.unlink(os.path.join(root, rel))     # the link only
removed_dirs = 0
for d in dirs:
    top = os.path.join(root, d)
    for dirpath, dirnames, filenames in os.walk(top, topdown=False):
        if not os.listdir(dirpath):
            os.rmdir(dirpath)
            removed_dirs += 1
print('DELETED files', len(expected), 'bytes', total, 'empty_dirs_removed', removed_dirs)
