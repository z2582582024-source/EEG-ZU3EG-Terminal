#!/usr/bin/env python3
"""Naming gate for a delivery tree: every directory and file name is lowercase ASCII letters, digits, '_' , '-' and '.'.

Chinese (or any non-ASCII) text belongs inside files, not in names.  A small allow-list covers names that tools expect in capitals
(README.md, CMakeLists.txt, Makefile, and Python's __init__.py / __main__.py by default).  Also reports names that differ only by case (they collide on Windows and macOS).

usage: python check_names.py <root> [--allow README.md CMakeLists.txt Makefile ...]
exit code 0 = clean, 1 = violations found.
"""
import argparse
import re
import sys
from collections import defaultdict
from pathlib import Path

OK = re.compile(r'^[a-z0-9][a-z0-9_.\-]*$')


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('root', type=Path)
    ap.add_argument('--allow', nargs='*', default=['README.md', 'CMakeLists.txt', 'Makefile', '__init__.py', '__main__.py'])
    a = ap.parse_args()
    allow = set(a.allow)
    bad, by_lower = [], defaultdict(set)
    count = 0
    for p in sorted(a.root.rglob('*')):
        count += 1
        name = p.name
        by_lower[(p.parent, name.lower())].add(name)
        if name in allow or OK.match(name):
            continue
        bad.append(p.relative_to(a.root).as_posix())
    clashes = [sorted(v) for v in by_lower.values() if len(v) > 1]
    for b in bad[:60]:
        print('bad name: %s' % b)
    for c in clashes[:20]:
        print('case clash: %s' % c)
    print('checked %d names, %d violations, %d case clashes' % (count, len(bad), len(clashes)))
    return 1 if (bad or clashes) else 0


if __name__ == '__main__':
    sys.exit(main())
