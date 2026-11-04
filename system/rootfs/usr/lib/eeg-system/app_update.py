#!/usr/bin/env python3
"""Verify an application bundle and atomically switch a persistent release link."""
import argparse
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tarfile

BASELINE = Path('/etc/eeg-system/baseline.json')
ROOT = Path('/opt/eeg')
MAX_PAYLOAD = 1024 * 1024 * 1024
ABI_KEYS = ('kernel_release', 'driver_abi', 'terminal_abi', 'pl_abi')


def safe_name(name):
    path = PurePosixPath(name)
    if (not name or path.is_absolute() or '..' in path.parts or '\\' in name or
            '\x00' in name or path.as_posix() != name or name in ('.', 'app.manifest.json')):
        raise ValueError('UNSAFE_APPLICATION_PATH:' + name)
    return name


def validate_manifest(manifest, baseline):
    if manifest.get('schema') != 'eeg.application.bundle.v1':
        raise ValueError('APPLICATION_BUNDLE_MANIFEST_REQUIRED')
    release = manifest.get('release', '')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,79}', release):
        raise ValueError('INVALID_APPLICATION_RELEASE')
    for key in ABI_KEYS:
        if manifest.get(key) != baseline.get(key) or baseline.get(key) is None:
            raise ValueError('APPLICATION_BASELINE_MISMATCH:' + key)
    files = manifest.get('files')
    if not isinstance(files, dict) or not files:
        raise ValueError('APPLICATION_FILE_INVENTORY_REQUIRED')
    total = 0
    for name, row in files.items():
        safe_name(name)
        size = row.get('bytes')
        if not isinstance(size, int) or size < 0 or size > MAX_PAYLOAD:
            raise ValueError('INVALID_APPLICATION_FILE_LENGTH:' + name)
        if not re.fullmatch('[0-9a-f]{64}', str(row.get('sha256'))):
            raise ValueError('INVALID_APPLICATION_FILE_HASH:' + name)
        if row.get('mode') not in (0o644, 0o755):
            raise ValueError('APPLICATION_MODE_MUST_BE_0644_OR_0755:' + name)
        total += size
    if total > MAX_PAYLOAD:
        raise ValueError('APPLICATION_PAYLOAD_LIMIT_EXCEEDED')
    for name in ('bin/eeg-ui', 'bin/eegctl'):
        if files.get(name, {}).get('mode') != 0o755:
            raise ValueError('APPLICATION_ENTRYPOINT_MISSING:' + name)
    return total


def require_idle():
    mounted = subprocess.run(['findmnt', '-n', '-o', 'FSTYPE,OPTIONS', '--target', str(ROOT)],
                             check=True, text=True, stdout=subprocess.PIPE, timeout=5).stdout.split()
    if len(mounted) != 2 or mounted[0] != 'ext4' or 'rw' not in mounted[1].split(','):
        raise RuntimeError('APPLICATION_ROOT_NOT_PERSISTENT_WRITABLE_EXT4')
    for name in ('ui.active', 'display.active', 'capture.active'):
        if (Path('/run/eeg-operator') / name).exists():
            raise RuntimeError('STOP_APPLICATION_FIRST:' + name)
    # A missing marker alone cannot prove that a crashed supervisor released HW.
    # Open device descriptors are checked independently, without killing owners.
    for proc in Path('/proc').iterdir():
        if not proc.name.isdecimal():
            continue
        try:
            for fd in (proc / 'fd').iterdir():
                try:
                    target = os.readlink(str(fd))
                except FileNotFoundError:
                    continue
                if target == '/dev/eeg-dsp' or target.startswith('/dev/dri/card'):
                    raise RuntimeError('HARDWARE_OWNER_OPEN:pid=' + proc.name)
        except (FileNotFoundError, ProcessLookupError):
            continue


def fsync_directory(path):
    directory = os.open(str(path), os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def release_link(name):
    path = ROOT / name
    if not path.is_symlink():
        return None
    target = path.resolve(strict=True)
    releases = (ROOT / 'releases').resolve(strict=True)
    if target.parent != releases or not target.is_dir():
        raise RuntimeError('RELEASE_LINK_OUTSIDE_RELEASES:' + name)
    return target


def set_link(name, target):
    temporary = ROOT / ('.' + name + '.new')
    if temporary.exists() or temporary.is_symlink():
        raise RuntimeError('UNFINISHED_LINK_UPDATE_REQUIRES_REVIEW:' + str(temporary))
    os.symlink('releases/' + target.name, str(temporary))
    os.replace(str(temporary), str(ROOT / name))
    fsync_directory(ROOT)


def read_manifest(archive):
    members = archive.getmembers()
    if len(members) > 100000:
        raise ValueError('APPLICATION_FILE_COUNT_LIMIT_EXCEEDED')
    by_name = {}
    for member in members:
        if member.name in by_name:
            raise ValueError('DUPLICATE_APPLICATION_MEMBER:' + member.name)
        by_name[member.name] = member
        if not member.isfile():
            raise ValueError('REGULAR_APPLICATION_FILES_ONLY:' + member.name)
        if member.name != 'app.manifest.json':
            safe_name(member.name)
    info = by_name.get('app.manifest.json')
    if info is None or info.size > 2 * 1024 * 1024:
        raise ValueError('BOUNDED_APPLICATION_MANIFEST_REQUIRED')
    manifest = json.loads(archive.extractfile(info).read().decode('utf-8'))
    expected = set(manifest.get('files', {})) | {'app.manifest.json'}
    if set(by_name) != expected:
        raise ValueError('APPLICATION_ARCHIVE_INVENTORY_MISMATCH')
    return manifest, by_name


def verify_or_install(bundle, install):
    baseline = json.loads(BASELINE.read_text())
    with tarfile.open(str(bundle), 'r:*') as archive:
        manifest, members = read_manifest(archive)
        total = validate_manifest(manifest, baseline)
        stage = None
        destination = ROOT / 'releases' / manifest['release']
        if install:
            require_idle()
            if destination.exists():
                raise RuntimeError('RELEASE_ALREADY_EXISTS_USE_NEW_VERSION')
            if shutil.disk_usage(str(ROOT)).free < total + 64 * 1024 * 1024:
                raise RuntimeError('INSUFFICIENT_PERSISTENT_APPLICATION_SPACE')
            stage = ROOT / 'releases' / ('.' + manifest['release'] + '.staging')
            stage.mkdir(mode=0o755)
        for name, expected in sorted(manifest['files'].items()):
            member = members[name]
            if member.size != expected['bytes']:
                raise ValueError('APPLICATION_MEMBER_LENGTH_MISMATCH:' + name)
            digest = hashlib.sha256()
            stream = archive.extractfile(member)
            output = None
            try:
                if stage:
                    target = stage.joinpath(*PurePosixPath(name).parts)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    output = target.open('xb')
                    os.fchmod(output.fileno(), expected['mode'])
                for block in iter(lambda: stream.read(1024 * 1024), b''):
                    digest.update(block)
                    if output:
                        output.write(block)
                if output:
                    output.flush()
                    os.fsync(output.fileno())
            finally:
                if output:
                    output.close()
            if digest.hexdigest() != expected['sha256']:
                raise ValueError('APPLICATION_MEMBER_HASH_MISMATCH:' + name)
        if stage:
            with (stage / 'app.manifest.json').open('x') as output:
                json.dump(manifest, output, indent=2)
                output.write('\n')
                output.flush()
                os.fsync(output.fileno())
            for directory, children, files in os.walk(str(stage), topdown=False):
                fsync_directory(Path(directory))
            os.rename(str(stage), str(destination))
            fsync_directory(destination.parent)
            old = release_link('current')
            if old:
                set_link('previous', old)
            set_link('current', destination)
        return {'release': manifest['release'], 'bytes': total,
                'verified': True, 'installed': bool(stage), 'automatic_start': False}


def rollback():
    require_idle()
    previous = release_link('previous')
    current = release_link('current')
    if not previous or not current or previous == current:
        raise RuntimeError('DISTINCT_CURRENT_AND_PREVIOUS_RELEASES_REQUIRED')
    manifest = json.loads((previous / 'app.manifest.json').read_text())
    validate_manifest(manifest, json.loads(BASELINE.read_text()))
    # Recheck stored bytes; previous is a rollback source, not a trust assertion.
    for name, expected in manifest['files'].items():
        path = previous.joinpath(*PurePosixPath(name).parts)
        if path.is_symlink() or not path.is_file() or path.stat().st_size != expected['bytes']:
            raise RuntimeError('ROLLBACK_FILE_IDENTITY_MISMATCH:' + name)
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected['sha256']:
            raise RuntimeError('ROLLBACK_FILE_HASH_MISMATCH:' + name)
    set_link('current', previous)
    set_link('previous', current)
    return dict(release=previous.name, rollback=True, automatic_start=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('verify', 'install', 'rollback', 'status'))
    parser.add_argument('bundle', type=Path, nargs='?')
    args = parser.parse_args()
    if args.command in ('verify', 'install') and args.bundle is None:
        parser.error('bundle required for verify/install')
    if args.command in ('install', 'rollback') and os.geteuid() != 0:
        raise PermissionError('ROOT_REQUIRED')
    if args.command == 'status':
        result = {name: str(release_link(name)) for name in ('current', 'previous')}
    elif args.command == 'verify':
        result = verify_or_install(args.bundle, False)
    else:
        Path('/run/lock').mkdir(parents=True, exist_ok=True)
        with open('/run/lock/eeg-app-update.lock', 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = rollback() if args.command == 'rollback' else verify_or_install(args.bundle, True)
    result['timestamp_utc'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    print(json.dumps(result, indent=2))
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, ValueError, RuntimeError, tarfile.TarError) as exc:
        print('EEG_APPLICATION_UPDATE_FAILED: ' + str(exc), file=sys.stderr)
        sys.exit(3)
