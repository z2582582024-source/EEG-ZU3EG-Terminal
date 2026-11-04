#!/usr/bin/env python3
"""Build the real terminal on AArch64 and emit an updater-compatible bundle.

Compilation and offscreen checks do not open ADC, DMA, DRM or input devices.
The resulting bundle is verified but never installed or started automatically.
"""
import argparse
import datetime
import gzip
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import xml.etree.ElementTree as ET

BASELINE = Path('/etc/eeg-system/baseline.json')
RELEASES = Path('/opt/eeg/releases')
CC = Path('/usr/bin/gcc')
QMAKE = Path('/opt/eeg-qt/bin/qmake')
MAKE = Path('/usr/bin/make')
PKG_CONFIG = Path('/usr/bin/pkg-config')
PROJECT = Path('software/p5b_operator_v2/operator_terminal_01')
MAX_SOURCE_BYTES = 128 * 1024 * 1024
MAX_COMMAND_OUTPUT = 8 * 1024 * 1024
REQUIRED = (
    'userspace/eeg_ui.c', 'userspace/eegctl.c', 'display/eeg_display.c',
    'include/eeg_panel.h', 'include/qt_frame.h', 'include/lead_off_terminal.h',
    'rtl_dsp/eeg_dsp_uapi_v2.h', 'rtl_dsp/eeg_dsp_packet_v2.h',
    'qt/operator_terminal.pro', 'qt/resources/terminal_resources.qrc',
    'assets/logo.ppm', 'assets/fonts/NotoSansCJKsc-Regular.otf',
    'assets/fonts/LICENSE.txt', 'assets/fonts/source.json')
DICTIONARY = Path('src/eeg_zynq/operator_v2/resources')
DICTIONARY_FILES = ('pinyin_simp.json', 'LICENSE.pinyin_simp.txt', 'NOTICE.pinyin_simp.txt')
PROBE = r'''#include <stdio.h>
#include "eeg_dsp_uapi_v2.h"
#include "eeg_dsp_packet_v2.h"
#include "lead_off_terminal.h"
int main(void) {
    printf("{\"driver_abi\":%u,\"terminal_abi\":%u,\"pl_build_id\":%u,"
           "\"stream_algorithm_id\":%u,\"sample_interval_ns\":%u,\"raw_frame_bytes\":%u}\n",
           EEG_DSP_V2_UAPI_VERSION, TERMINAL_ABI, TERMINAL_BUILD_ID,
           EEG_DSP_V2_ALGORITHM_ID, EEG_DSP_V2_SAMPLE_INTERVAL_NS, EEG_DSP_V2_RAW_FRAME_BYTES);
    return 0;
}
'''


def checker():
    path = Path(__file__).with_name('app_update.py')
    spec = importlib.util.spec_from_file_location('eeg_native_build_updater', str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def digest(path):
    value = hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def within(path, root):
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def source_files(source):
    source = source.resolve(strict=True)
    if not source.is_dir() or (RELEASES.exists() and within(source, RELEASES.resolve())):
        raise ValueError('COPY_SOURCE_TO_WORKSPACE_BEFORE_BUILDING')
    product = source / PROJECT
    names = {PROJECT / name for name in REQUIRED}
    names.update(DICTIONARY / name for name in DICTIONARY_FILES)
    for directory, suffixes in (('qt/src', ('.cpp',)), ('qt/include', ('.h',)),
                                ('qt/tests', ('.cpp', '.h')),
                                ('include', ('.h',)), ('rtl_dsp', ('.h',))):
        folder = product / directory
        if not folder.is_dir() or folder.is_symlink():
            raise ValueError('APPLICATION_SOURCE_DIRECTORY_REQUIRED:' + directory)
        for path in folder.rglob('*'):
            if path.is_dir() and not path.is_symlink():
                continue
            if path.suffix in suffixes:
                names.add(path.relative_to(source))
    for name in ('qt/qt_terminal_tests.pro', 'qt/CMakeLists.txt'):
        if (product / name).exists():
            names.add(PROJECT / name)
    for resource in (product / 'qt/resources').rglob('*.qrc'):
        names.add(resource.relative_to(source))
        try:
            resource_tree = ET.parse(str(resource))
        except ET.ParseError as exc:
            raise ValueError('INVALID_QT_RESOURCE_XML:' + str(exc))
        for item in resource_tree.getroot().iter('file'):
            if not item.text:
                raise ValueError('EMPTY_QT_RESOURCE_PATH')
            path = resource.parent / item.text
            try:
                resolved = path.resolve(strict=True)
            except OSError:
                raise ValueError('QT_RESOURCE_REQUIRED:' + item.text)
            if not within(resolved, source) or path.is_symlink():
                raise ValueError('QT_RESOURCE_OUTSIDE_SOURCE')
            names.add(resolved.relative_to(source))
    files = {}
    total = 0
    for name in sorted(names):
        path = source / name
        if path.is_symlink() or not path.is_file() or path.resolve(strict=True) != path:
            raise ValueError('REGULAR_IN_TREE_APPLICATION_SOURCE_REQUIRED:' + name.as_posix())
        size = path.stat().st_size
        total += size
        if total > MAX_SOURCE_BYTES:
            raise ValueError('APPLICATION_SOURCE_SIZE_LIMIT_EXCEEDED')
        files[name.as_posix()] = path
    return source, files, total


def require_target(path):
    with path.open('rb') as stream:
        header = stream.read(20)
    if header[:6] != b'\x7fELF\x02\x01' or len(header) != 20 or int.from_bytes(header[18:20], 'little') != 183:
        raise ValueError('AARCH64_ELF_REQUIRED:' + str(path))


def recording_policy(source):
    """Bind new recording semantics to the actual snapshotted C++ inputs."""
    product = source / PROJECT
    header = (product / 'qt/include/raw_recorder.h').read_text(encoding='utf-8')
    implementation = (product / 'qt/src/raw_recorder.cpp').read_text(encoding='utf-8')
    if 'p5b.eeg.raw_recording_session.v1' not in implementation:
        return dict(schema='eeg.recording.contract.v1', mode='LEGACY_SINGLE_FILE',
                    note='No segmented-session implementation in this source snapshot.')
    values = {}
    for name in ('DefaultSegmentBytes', 'DefaultMinimumFreeBytes', 'DefaultMaximumQueuedPackets'):
        match = re.search(r'\b' + name + r'\s*=\s*([0-9]+)(?:ULL)?\s*;', header)
        if not match:
            raise ValueError('RECORDING_POLICY_SOURCE_CONSTANT_REQUIRED:' + name)
        values[name] = int(match.group(1))
    block_bytes = 32 * 96
    return dict(schema='eeg.recording.contract.v2', mode='BOUNDED_SEGMENTED_SESSION', default='OFF',
                maximum_session_bytes=None, maximum_segment_bytes=values['DefaultSegmentBytes'] // block_bytes * block_bytes,
                maximum_queued_packets=values['DefaultMaximumQueuedPackets'], minimum_free_bytes=values['DefaultMinimumFreeBytes'],
                segment_sidecar_schema='p5b.eeg.raw_recording.v1', session_schema='p5b.eeg.raw_recording_session.v1',
                automatic_deletion=False, capacity_exhaustion='RECORDING_DISABLED_ACQUISITION_CONTINUES',
                replay='LOCAL_FILE_REPLAY_RAW_SEGMENTS_WITH_SIDECAR_VALIDATION',
                hardware_abi_changed=False)


def validate_probe(value, baseline):
    expected = dict(driver_abi=int(baseline['driver_abi']),
                    terminal_abi=int(baseline['terminal_abi']),
                    pl_build_id=int(str(baseline['pl_build_id']), 0),
                    stream_algorithm_id=int(baseline['stream_algorithm_id']),
                    sample_interval_ns=4096000, raw_frame_bytes=96)
    if value != expected or int(str(baseline['pl_abi']), 0) != expected['terminal_abi']:
        raise ValueError('NATIVE_SOURCE_ABI_BASELINE_MISMATCH')


def execute(argv, cwd, env, receipt, timeout=900):
    with tempfile.TemporaryFile(dir=str(cwd)) as output:
        process = subprocess.run([str(value) for value in argv], cwd=str(cwd), env=env,
                                 stdin=subprocess.DEVNULL, stdout=output,
                                 stderr=subprocess.STDOUT, timeout=timeout)
        size = output.tell()
        output.seek(max(0, size - 8192))
        tail = output.read().decode('utf-8', 'replace')
        receipt['commands'].append(dict(argv=[str(value) for value in argv],
            returncode=process.returncode, output_tail=tail, output_bytes=size))
        if process.returncode or size > MAX_COMMAND_OUTPUT:
            raise RuntimeError('NATIVE_BUILD_COMMAND_FAILED:' + str(argv[0]))
        output.seek(0)
        return output.read()


def write_bundle(path, manifest, files, validator, baseline):
    validator.validate_manifest(manifest, baseline)
    with path.open('xb') as raw, gzip.GzipFile(fileobj=raw, mode='wb', filename='', mtime=0) as zipped:
        with tarfile.open(fileobj=zipped, mode='w', format=tarfile.GNU_FORMAT) as archive:
            for name, origin in sorted(files.items()):
                row = manifest['files'][name]
                item = tarfile.TarInfo(name)
                item.size, item.mode, item.uid, item.gid, item.mtime = row['bytes'], row['mode'], 0, 0, 0
                with origin.open('rb') as stream:
                    archive.addfile(item, stream)
            body = (json.dumps(manifest, indent=2) + '\n').encode('utf-8')
            item = tarfile.TarInfo('app.manifest.json')
            item.size, item.mode, item.uid, item.gid, item.mtime = len(body), 0o644, 0, 0, 0
            import io
            archive.addfile(item, io.BytesIO(body))
    with path.open('r+b') as stream:
        os.fsync(stream.fileno())
    with tarfile.open(str(path), 'r:gz') as archive:
        read_manifest, members = validator.read_manifest(archive)
        validator.validate_manifest(read_manifest, baseline)
        for name, row in read_manifest['files'].items():
            member = members[name]
            if member.size != row['bytes']:
                raise ValueError('BUILT_BUNDLE_LENGTH_MISMATCH:' + name)
            value = hashlib.sha256()
            stream = archive.extractfile(member)
            for block in iter(lambda: stream.read(1024 * 1024), b''):
                value.update(block)
            if value.hexdigest() != row['sha256']:
                raise ValueError('BUILT_BUNDLE_HASH_MISMATCH:' + name)


def build(source, release, output, jobs=1):
    if platform.machine().lower() not in ('aarch64', 'arm64'):
        raise RuntimeError('RUN_NATIVE_BUILDER_ON_AARCH64_TARGET')
    if jobs not in (1, 2) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,79}', release):
        raise ValueError('VALID_NEW_RELEASE_AND_JOBS_1_OR_2_REQUIRED')
    baseline = json.loads(BASELINE.read_text())
    validator = checker()
    if (RELEASES / release).exists():
        raise ValueError('NEW_APPLICATION_RELEASE_NAME_REQUIRED')
    source, inputs, source_bytes = source_files(source)
    output = output.absolute()
    if output.exists() or output.is_symlink() or not output.name.endswith('.tar.gz'):
        raise ValueError('NEW_TAR_GZ_OUTPUT_REQUIRED')
    output.parent.mkdir(parents=True, exist_ok=True)
    if (RELEASES.exists() and within(output.resolve(), RELEASES.resolve())) or within(output, source):
        raise ValueError('OUTPUT_MUST_BE_OUTSIDE_SOURCE_AND_INSTALLED_RELEASES')
    if shutil.disk_usage(str(output.parent)).free < 512 * 1024 * 1024 + source_bytes * 2:
        raise RuntimeError('INSUFFICIENT_NATIVE_BUILD_SPACE')
    work = Path(tempfile.mkdtemp(prefix='.eeg-app-build-', dir=str(output.parent)))
    receipt = dict(schema='eeg.native.application_build.v1', passed=False,
        started_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        execution='AArch64 userspace build; not a board acceptance receipt',
        hardware_operations=0, physical_media_writes=0, board_verified=False,
        automatic_install=False, automatic_start=False, release=release,
        workspace=str(work), commands=[], source_files={})
    try:
        snapshot = work / 'source'
        for name, origin in inputs.items():
            target = snapshot / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(str(origin), str(target))
            receipt['source_files'][name] = dict(bytes=target.stat().st_size, sha256=digest(target))
        product = snapshot / PROJECT
        env = dict(os.environ, PATH='/opt/eeg-qt/bin:/usr/sbin:/usr/bin:/sbin:/bin',
                   TMPDIR=str(work), LD_LIBRARY_PATH='/opt/eeg-qt/lib:/usr/lib:/lib',
                   QT_QPA_PLATFORM='offscreen', QT_PLUGIN_PATH='/opt/eeg-qt/plugins',
                   QT_QPA_FONTDIR='/opt/eeg-qt/fonts', XDG_RUNTIME_DIR=str(work))
        for name in ('CC', 'CXX', 'CFLAGS', 'CXXFLAGS', 'CPPFLAGS', 'LDFLAGS',
                     'QMAKESPEC', 'QMAKEPATH', 'QMAKEFEATURES', 'QT_SELECT'):
            env.pop(name, None)
        bins = work / 'bin'
        bins.mkdir()
        probe = work / 'abi_probe.c'
        probe.write_text(PROBE)
        execute([CC, '-std=c11', '-O2', '-Wall', '-Wextra', '-Werror',
                 '-I' + str(product / 'rtl_dsp'), '-I' + str(product / 'include'),
                 probe, '-o', bins / 'abi-probe'], work, env, receipt)
        require_target(bins / 'abi-probe')
        abi = json.loads(execute([bins / 'abi-probe'], work, env, receipt, timeout=30).decode('ascii'))
        validate_probe(abi, baseline)
        receipt['compiled_source_abi'] = abi
        for name, source_name in (('eeg-ui', 'eeg_ui.c'), ('eegctl', 'eegctl.c')):
            execute([CC, '-std=c11', '-O2', '-Wall', '-Wextra', '-Werror',
                     product / 'userspace' / source_name, '-o', bins / name], work, env, receipt)
        flags = shlex.split(execute([PKG_CONFIG, '--cflags', '--libs', 'libdrm'],
                                   work, env, receipt, timeout=30).decode('utf-8'))
        execute([CC, '-std=c11', '-O2', '-Wall', '-Wextra', '-Werror',
                 product / 'display/eeg_display.c', *flags, '-lm', '-o', bins / 'eeg-display'],
                work, env, receipt)
        qt_build = work / 'qt-build'
        qt_build.mkdir()
        execute([QMAKE, '-spec', 'linux-g++', product / 'qt/operator_terminal.pro',
                 'CONFIG+=release', 'QMAKE_CXXFLAGS+=-Wall -Wextra -Werror'], qt_build, env, receipt)
        execute([MAKE, '-j' + str(jobs)], qt_build, env, receipt)
        shutil.copyfile(str(qt_build / 'eeg-terminal'), str(bins / 'eeg-terminal'))
        (bins / 'eeg-terminal').chmod(0o755)
        for name in ('eeg-ui', 'eegctl', 'eeg-display', 'eeg-terminal'):
            require_target(bins / name)
        execute([bins / 'eeg-ui'], work, env, receipt, timeout=30)
        execute([bins / 'eeg-terminal', '--version'], work, env, receipt, timeout=30)
        files = {'src/' + name: snapshot / name for name in inputs}
        files.update({'bin/' + name: bins / name for name in ('eeg-ui', 'eegctl', 'eeg-display', 'eeg-terminal')})
        files['fonts/NotoSansCJKsc-Regular.otf'] = product / 'assets/fonts/NotoSansCJKsc-Regular.otf'
        manifest = dict(schema='eeg.application.bundle.v1', release=release,
                        recording=recording_policy(snapshot),
                        files={}, provenance=dict(native_build=receipt['source_files'],
                            builder='eeg-app-build.v1', compiled_source_abi=abi))
        for key in validator.ABI_KEYS:
            manifest[key] = baseline[key]
        for name, path in sorted(files.items()):
            validator.safe_name(name)
            manifest['files'][name] = dict(bytes=path.stat().st_size, sha256=digest(path),
                                           mode=0o755 if name.startswith('bin/') else 0o644)
        bundle = work / 'application.tar.gz'
        write_bundle(bundle, manifest, files, validator, baseline)
        # A hard link publishes a complete file atomically and cannot overwrite
        # an existing release archive, even if another builder races this one.
        os.link(str(bundle), str(output))
        directory = os.open(str(output.parent), os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        receipt.update(passed=True, bundle=dict(path=str(output), bytes=output.stat().st_size,
            sha256=digest(output)), application_files=len(manifest['files']))
    except Exception as exc:
        receipt['error'] = type(exc).__name__ + ': ' + str(exc)
        raise
    finally:
        receipt['ended_at'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        (work / 'build.receipt.json').write_text(json.dumps(receipt, indent=2) + '\n')
    return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True,
                        help='workspace copy of the application src/ tree')
    parser.add_argument('--release', required=True, help='new unique application release name')
    parser.add_argument('--output', type=Path, required=True, help='new bundle .tar.gz path')
    parser.add_argument('--jobs', type=int, choices=(1, 2), default=1)
    args = parser.parse_args(argv)
    try:
        result = build(args.source, args.release, args.output, args.jobs)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print('EEG_NATIVE_APP_BUILD_FAILED: ' + str(exc), file=sys.stderr)
        return 3
    print(json.dumps(result, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
