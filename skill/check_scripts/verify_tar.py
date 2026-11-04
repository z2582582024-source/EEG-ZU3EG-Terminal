"""Verify an archive against the board's expected SHA list: every expected file must be in the tar with the same SHA-256 (a hard-link member
counts with the content of the member it links to); listed symlinks must be stored as links with the same target.
usage: python -I verify_tar.py <archive.tar> <expected.sha> <links.txt>"""
import hashlib
import sys
import tarfile

archive, expected_path, links_path = sys.argv[1:4]
expected = {}
for line in open(expected_path, encoding='utf-8'):
    digest, rel = line.rstrip('\n').split('  ', 1)
    expected[rel] = digest
links = {}
for line in open(links_path, encoding='utf-8'):
    if line.strip():
        rel, target = line.rstrip('\n').split(' -> ', 1)
        links[rel] = target

seen = {}
hardlinks = {}
stored_links = {}
regular = 0
with tarfile.open(archive, 'r:') as tar:
    for m in tar:
        if m.isreg():
            h = hashlib.sha256()
            f = tar.extractfile(m)
            for chunk in iter(lambda: f.read(1 << 20), b''):
                h.update(chunk)
            seen[m.name] = h.hexdigest()
            regular += 1
        elif m.islnk():
            hardlinks[m.name] = m.linkname
        elif m.issym():
            stored_links[m.name] = m.linkname
for name, target in hardlinks.items():
    seen[name] = seen.get(target)
missing = [p for p in expected if p not in seen]
changed = [p for p in expected if p in seen and seen[p] != expected[p]]
extra = [p for p in seen if p not in expected]
link_problems = [p for p, t in links.items() if stored_links.get(p) != t]
print('regular members', regular, 'hardlink members', len(hardlinks), 'symlinks', len(stored_links))
print('expected', len(expected), 'missing', len(missing), 'sha_mismatch', len(changed), 'extra', len(extra), 'link_problems', len(link_problems))
for p in (missing + changed + extra + link_problems)[:10]:
    print('  ', p)
print('ARCHIVE_EQUALS_BOARD_LIST' if not (missing or changed or extra or link_problems) else 'ARCHIVE_MISMATCH')
