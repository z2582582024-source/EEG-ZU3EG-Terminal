#!/usr/bin/env python3
"""Text-format gate for a directory tree: UTF-8 without BOM, no NUL, no U+FFFD, a single line-ending style per file (LF by default).

Why: a script that rewrote a status document silently changed its line endings (a shell escape bug), and a Windows console codepage turned Chinese
text into U+FFFD.  Counting carriage returns with grep is not reliable on files that mix styles, so the check decodes and counts bytes.

usage: python check_doc_format.py <root> [--ext .md .txt .py ...] [--crlf-ext .cmd .ps1 .bat] [--strict-lf] [--max-report 40]
CRLF endings are reported as a count only; add --strict-lf for documents that must be LF.  .cmd/.ps1/.bat files are not checked for endings.
exit code 0 = clean, 1 = problems found (listed), 2 = usage error.
"""
import argparse
import sys
from pathlib import Path

DEFAULT_EXT = ['.md', '.txt', '.py', '.sv', '.v', '.vh', '.tcl', '.xdc', '.c', '.h', '.cpp', '.hpp', '.sh', '.json', '.tsv', '.cmake', '.pro', '.svg', '.yaml', '.yml']
SKIP_DIRS = {'.git', '__pycache__', 'node_modules'}


def check(path, crlf_ok):
    """-> (problems, warnings).  CRLF is a warning (files inherited from Windows keep their endings; hash-pinned records must not be rewritten)."""
    data = path.read_bytes()
    problems, warnings = [], []
    if data.startswith(b'\xef\xbb\xbf'):
        problems.append('UTF-8 BOM')
    if b'\x00' in data:
        problems.append('NUL byte')
    try:
        text = data.decode('utf-8')
    except UnicodeDecodeError as e:
        return problems + ['not valid UTF-8 (%s)' % e.reason], warnings
    if chr(0xFFFD) in text:                                  # built from the code point: the check must not trip over its own source
        problems.append('contains U+FFFD (a decoding error was baked into the file)')
    crlf = data.count(b'\r\n')
    lone_cr = data.count(b'\r') - crlf
    if lone_cr:
        problems.append('%d lone CR' % lone_cr)
    if not crlf_ok and crlf:
        warnings.append('%d CRLF line endings' % crlf)
    return problems, warnings


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('root', type=Path)
    ap.add_argument('--ext', nargs='*', default=DEFAULT_EXT)
    ap.add_argument('--crlf-ext', nargs='*', default=['.cmd', '.bat', '.ps1'], help='script extensions whose line endings are not checked')
    ap.add_argument('--max-report', type=int, default=40)
    ap.add_argument('--strict-lf', action='store_true', help='treat CRLF in non-script files as an error (use for documents you write)')
    a = ap.parse_args()
    if not a.root.is_dir():
        print('not a directory: %s' % a.root)
        return 2
    exts = {e.lower() for e in a.ext + a.crlf_ext}
    bad, warned, scanned = [], 0, 0
    for p in sorted(a.root.rglob('*')):
        if not p.is_file() or any(part in SKIP_DIRS for part in p.parts) or p.suffix.lower() not in exts:
            continue
        scanned += 1
        problems, warnings = check(p, p.suffix.lower() in a.crlf_ext)
        if warnings and a.strict_lf:
            problems += warnings
        elif warnings:
            warned += 1
        if problems:
            bad.append((p, problems))
    for p, problems in bad[:a.max_report]:
        print('%s: %s' % (p.relative_to(a.root).as_posix(), '; '.join(problems)))
    if len(bad) > a.max_report:
        print('... %d more' % (len(bad) - a.max_report))
    print('checked %d files, %d with problems, %d more with CRLF endings only (not an error without --strict-lf)' % (scanned, len(bad), warned))
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
