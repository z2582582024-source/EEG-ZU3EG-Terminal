#!/usr/bin/env python3
"""Plan and apply one manifest-bound, same-ABI P5B PL image update."""
import argparse
import datetime
try:
    import fcntl
except ImportError:  # Host-side contract tests do not execute the Linux apply path.
    fcntl = None
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import struct
import subprocess
import sys
import time

RUN = Path('/run/eeg-operator')
BASELINE = Path('/etc/eeg-system/baseline.json')
BUNDLE = Path('/usr/lib/eeg-system/pl_bundle')
BOOT_FIT = Path('/boot/image.ub')
DSP_DEVICE = Path('/dev/eeg-dsp')
MODULE_NAME = 'eeg_dsp_dma_v2'
IDENTITY_IOCTL = (2 << 30) | (96 << 16) | (ord('D') << 8) | 0x3A
UAPI_MAGIC = 0x32445045
UAPI_VERSION = 2
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_BITSTREAM_BYTES = 128 * 1024 * 1024
REGIONS = {
    'capture': {'compatible': 'eeg-zynq,p5b-cont-dsp-v2', 'reg': (0x80000000, 0x1000)},
    'dma': {'compatible': 'xlnx,axi-dma-1.00.a', 'reg': (0x80010000, 0x1000)},
}
IDENTITY_NAMES = (
    'magic', 'version', 'bytes', 'build_id', 'abi', 'capability', 'packet_bytes',
    'sample_interval_ns', 'fft_size', 'fft_channel_mask', 'algorithm_id',
    'filter_profile_id', 'time_basis_id', 'cycle_hz', 'cycle_event_id',
    'max_admission_cycles', 'core_base', 'core_bytes', 'dma_base', 'dma_bytes',
    'state', 'acquisition_enable', 'handoff', 'ready',
)
STATIC_IDENTITY_NAMES = IDENTITY_NAMES[:20]


class Blocked(RuntimeError):
    pass


def sha(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def json_read(path):
    data = path.read_bytes()
    if len(data) > MAX_MANIFEST_BYTES:
        raise Blocked('MANIFEST_TOO_LARGE')
    return json.loads(data.decode('utf-8', 'strict'))


def safe_member(root, name):
    item = PurePosixPath(name)
    if (not name or item.is_absolute() or '..' in item.parts or '\\' in name or
            item.as_posix() != name or name in ('.', 'manifest.json')):
        raise Blocked('UNSAFE_BUNDLE_PATH:' + str(name))
    path = root.joinpath(*item.parts)
    if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
        raise Blocked('BUNDLE_FILE_REQUIRED:' + name)
    return path


def verify_bundle(root, manifest):
    if manifest.get('schema') != 'eeg.zu3eg.pl_update_bundle.v1':
        raise Blocked('PL_UPDATE_MANIFEST_SCHEMA_MISMATCH')
    if manifest.get('target') != 'P5B XCZU3EG' or manifest.get('mode') != 'same-abi-full-pl':
        raise Blocked('PL_UPDATE_TARGET_OR_MODE_MISMATCH')
    files = manifest.get('files')
    if not isinstance(files, dict) or set(files) != {
            'update_bitstream', 'rollback_bitstream', 'system_fit', 'system_dtb', 'dsp_module',
            'update_qualification', 'rollback_qualification', 'update_evidence', 'rollback_evidence'}:
        raise Blocked('PL_UPDATE_FILE_ROLES_MISMATCH')
    verified = {}
    for role, record in files.items():
        if not isinstance(record, dict):
            raise Blocked('PL_UPDATE_FILE_RECORD_INVALID:' + role)
        path = safe_member(root, record.get('path', ''))
        if path.stat().st_size != record.get('bytes'):
            raise Blocked('PL_UPDATE_FILE_LENGTH_MISMATCH:' + role)
        if not re.fullmatch(r'[0-9a-f]{64}', str(record.get('sha256', ''))) or sha(path) != record['sha256']:
            raise Blocked('PL_UPDATE_FILE_HASH_MISMATCH:' + role)
        if role in ('update_bitstream', 'rollback_bitstream') and path.stat().st_size > MAX_BITSTREAM_BYTES:
            raise Blocked('PL_BITSTREAM_LIMIT_EXCEEDED:' + role)
        verified[role] = path
    if files['update_bitstream']['sha256'] == files['rollback_bitstream']['sha256']:
        raise Blocked('DISTINCT_ROLLBACK_BITSTREAM_REQUIRED')
    contract = manifest.get('identity')
    if not isinstance(contract, dict) or set(contract) != set(STATIC_IDENTITY_NAMES):
        raise Blocked('PL_IDENTITY_CONTRACT_REQUIRED')
    provenance = manifest.get('provenance', {})
    for role, source in (('system_fit', 'fit'), ('system_dtb', 'dtb')):
        expected = provenance.get(source, {})
        actual = files[role]
        if expected.get('bytes') != actual.get('bytes') or expected.get('sha256') != actual.get('sha256'):
            raise Blocked('PL_PROVENANCE_FILE_BINDING_MISMATCH:' + role)
    if manifest.get('checks', {}).get('dtb_sha256') != files['system_dtb']['sha256']:
        raise Blocked('PL_DTB_CHECKSUM_BINDING_MISMATCH')
    rollback = manifest.get('rollback_provenance')
    if not isinstance(rollback, dict) or rollback.get('schema') != 'eeg.p5b.ter2_prior_good.v1':
        raise Blocked('BOARD_VERIFIED_PRIOR_GOOD_ROLLBACK_REQUIRED')
    required_rollback = {
        'status': 'BOARD_VERIFIED_PRIOR_GOOD', 'target': 'P5B XCZU3EG',
        'bitstream_sha256': files['rollback_bitstream']['sha256'],
        'dtb_sha256': files['system_dtb']['sha256'],
        'kernel_release': manifest.get('kernel_release'), 'driver_abi': manifest.get('driver_abi'),
        'terminal_abi': manifest.get('system_identity', {}).get('terminal_abi'),
        'pl_abi': manifest.get('system_identity', {}).get('pl_abi'),
        'build_id': manifest.get('system_identity', {}).get('pl_build_id'),
    }
    for name, value in required_rollback.items():
        if rollback.get(name) != value:
            raise Blocked('ROLLBACK_PROVENANCE_MISMATCH:' + name)
    if (not re.fullmatch(r'[0-9a-f]{64}', str(rollback.get('verification_receipt_sha256', ''))) or
            not rollback.get('verification_receipt')):
        raise Blocked('ROLLBACK_VERIFICATION_RECEIPT_REQUIRED')
    if (rollback.get('verification_receipt_sha256') != files['rollback_evidence']['sha256'] or
            rollback.get('qualification_receipt_sha256') != files['rollback_qualification']['sha256'] or
            rollback.get('verification_receipt') != files['rollback_evidence']['path']):
        raise Blocked('ROLLBACK_EVIDENCE_FILE_BINDING_MISMATCH')
    rollback_qual = json_read(verified['rollback_qualification'])
    rollback_evidence = json_read(verified['rollback_evidence'])
    rollback_qual_bit = rollback_qual.get('bitstream', {})
    rollback_qual_evidence = rollback_qual.get('verification_receipt', {})
    if (rollback_qual.get('schema') != 'eeg.p5b.ter2_prior_good.v1' or
            rollback_qual.get('status') != 'BOARD_VERIFIED_PRIOR_GOOD' or
            rollback_qual.get('role') != 'prior_good_rollback' or
            rollback_qual.get('target') != 'P5B XCZU3EG' or
            rollback_qual_bit.get('sha256') != files['rollback_bitstream']['sha256'] or
            rollback_qual_bit.get('bytes') != files['rollback_bitstream']['bytes'] or
            rollback_qual.get('dtb_sha256') != files['system_dtb']['sha256'] or
            rollback_qual.get('kernel_release') != manifest.get('kernel_release') or
            rollback_qual.get('driver_abi') != manifest.get('driver_abi') or
            rollback_qual.get('terminal_abi') != manifest.get('system_identity', {}).get('terminal_abi') or
            rollback_qual.get('pl_abi') != manifest.get('system_identity', {}).get('pl_abi') or
            rollback_qual.get('build_id') != manifest.get('system_identity', {}).get('pl_build_id') or
            rollback_qual_evidence.get('sha256') != files['rollback_evidence']['sha256'] or
            rollback_evidence.get('schema') != 'eeg.p5b.ter2_prior_good_board.v1' or
            rollback_evidence.get('passed') is not True or
            rollback_evidence.get('status') != 'BOARD_VERIFIED_PRIOR_GOOD' or
            rollback_evidence.get('target') != 'P5B XCZU3EG' or
            rollback_evidence.get('bitstream_sha256') != files['rollback_bitstream']['sha256'] or
            rollback_evidence.get('dtb_sha256') != files['system_dtb']['sha256'] or
            rollback_evidence.get('kernel_release') != manifest.get('kernel_release') or
            rollback_evidence.get('driver_abi') != manifest.get('driver_abi') or
            rollback_evidence.get('terminal_abi') != manifest.get('system_identity', {}).get('terminal_abi') or
            rollback_evidence.get('pl_abi') != manifest.get('system_identity', {}).get('pl_abi') or
            rollback_evidence.get('build_id') != manifest.get('system_identity', {}).get('pl_build_id')):
        raise Blocked('ROLLBACK_BOARD_EVIDENCE_INVALID')
    update = manifest.get('update_provenance')
    if not isinstance(update, dict) or update.get('schema') != 'eeg.p5b.ter2_update_candidate.v1':
        raise Blocked('QUALIFIED_SAME_ABI_UPDATE_CANDIDATE_REQUIRED')
    required_update = {
        'status': 'OFFLINE_IMPLEMENTATION_GATES_PASS_NO_BOARD_TEST', 'target': 'P5B XCZU3EG',
        'bitstream_sha256': files['update_bitstream']['sha256'],
        'dtb_sha256': files['system_dtb']['sha256'],
        'kernel_release': manifest.get('kernel_release'), 'driver_abi': manifest.get('driver_abi'),
        'terminal_abi': manifest.get('system_identity', {}).get('terminal_abi'),
        'pl_abi': manifest.get('system_identity', {}).get('pl_abi'),
        'build_id': manifest.get('system_identity', {}).get('pl_build_id'),
    }
    for name, value in required_update.items():
        if update.get(name) != value:
            raise Blocked('UPDATE_PROVENANCE_MISMATCH:' + name)
    if (not re.fullmatch(r'[0-9a-f]{64}', str(update.get('verification_receipt_sha256', ''))) or
            not update.get('verification_receipt')):
        raise Blocked('UPDATE_BUILD_RECEIPT_REQUIRED')
    if (update.get('verification_receipt_sha256') != files['update_evidence']['sha256'] or
            update.get('qualification_receipt_sha256') != files['update_qualification']['sha256'] or
            update.get('verification_receipt') != files['update_evidence']['path']):
        raise Blocked('UPDATE_EVIDENCE_FILE_BINDING_MISMATCH')
    update_qual = json_read(verified['update_qualification'])
    update_evidence = json_read(verified['update_evidence'])
    update_qual_bit = update_qual.get('bitstream', {})
    update_qual_evidence = update_qual.get('verification_receipt', {})
    if (update_qual.get('schema') != 'eeg.p5b.ter2_update_candidate.v1' or
            update_qual.get('status') != 'OFFLINE_IMPLEMENTATION_GATES_PASS_NO_BOARD_TEST' or
            update_qual.get('role') != 'update_candidate' or
            update_qual.get('target') != 'P5B XCZU3EG' or
            update_qual_bit.get('sha256') != files['update_bitstream']['sha256'] or
            update_qual_bit.get('bytes') != files['update_bitstream']['bytes'] or
            update_qual.get('dtb_sha256') != files['system_dtb']['sha256'] or
            update_qual.get('kernel_release') != manifest.get('kernel_release') or
            update_qual.get('driver_abi') != manifest.get('driver_abi') or
            update_qual.get('terminal_abi') != manifest.get('system_identity', {}).get('terminal_abi') or
            update_qual.get('pl_abi') != manifest.get('system_identity', {}).get('pl_abi') or
            update_qual.get('build_id') != manifest.get('system_identity', {}).get('pl_build_id') or
            update_qual_evidence.get('sha256') != files['update_evidence']['sha256'] or
            update_evidence.get('schema') != 'eeg.p5b.ter2_update_candidate_build.v1' or
            update_evidence.get('passed') is not True or
            update_evidence.get('status') != 'OFFLINE_IMPLEMENTATION_GATES_PASS_NO_BOARD_TEST' or
            update_evidence.get('target') != 'P5B XCZU3EG' or
            update_evidence.get('bitstream_sha256') != files['update_bitstream']['sha256'] or
            update_evidence.get('dtb_sha256') != files['system_dtb']['sha256'] or
            update_evidence.get('kernel_release') != manifest.get('kernel_release') or
            update_evidence.get('driver_abi') != manifest.get('driver_abi') or
            update_evidence.get('terminal_abi') != manifest.get('system_identity', {}).get('terminal_abi') or
            update_evidence.get('pl_abi') != manifest.get('system_identity', {}).get('pl_abi') or
            update_evidence.get('build_id') != manifest.get('system_identity', {}).get('pl_build_id')):
        raise Blocked('UPDATE_BUILD_EVIDENCE_INVALID')
    return verified


def identity_error(actual, expected):
    if not isinstance(actual, dict):
        return 'PL_IDENTITY_RESULT_UNKNOWN'
    for name in STATIC_IDENTITY_NAMES:
        if actual.get(name) != expected.get(name):
            return 'PL_IDENTITY_MISMATCH:' + name
    if actual.get('magic') != UAPI_MAGIC or actual.get('version') != UAPI_VERSION or actual.get('bytes') != 96:
        return 'PL_IDENTITY_UAPI_MISMATCH'
    if actual.get('state') not in (0, 2, 6):
        return 'PL_IDENTITY_STATE_NOT_QUIESCENT'
    if actual.get('acquisition_enable') != 0 or actual.get('handoff') != 1 or actual.get('ready') != 1:
        return 'PL_IDENTITY_NOT_SAFE_OR_READY'
    return None


def manager_state_allowed(state):
    return state.strip() == 'operating'


def read_cstring(path):
    return path.read_bytes().split(b'\0', 1)[0].decode('ascii', 'strict')


def read_cstrings(path):
    return [value.decode('ascii', 'strict') for value in path.read_bytes().split(b'\0') if value]


def read_u32_cells(data):
    if len(data) % 4:
        raise Blocked('DEVICE_TREE_PROPERTY_LENGTH_INVALID')
    return tuple(struct.unpack('>' + 'I' * (len(data) // 4), data))


def node_properties(root, compatible):
    for directory, _, _ in os.walk(root, followlinks=False):
        base = Path(directory)
        compat = base / 'compatible'
        try:
            values = compat.read_bytes().rstrip(b'\0').decode('ascii').split('\0')
        except (OSError, UnicodeError):
            continue
        if compatible in values:
            yield base


def node_reg(node, expected):
    try:
        cells = read_u32_cells((node / 'reg').read_bytes())
    except OSError as exc:
        raise Blocked('DEVICE_TREE_REG_MISSING:' + str(node)) from exc
    if cells != (0, expected[0], 0, expected[1]):
        raise Blocked('DEVICE_TREE_REG_MISMATCH:' + str(node))


def check_live_device_tree(root):
    model = read_cstring(root / 'model')
    if model != 'P5B XCZU3EG TER2 miniDP EEG terminal':
        raise Blocked('DEVICE_TREE_MODEL_MISMATCH')
    capture_dmas = None
    for role, expected in REGIONS.items():
        matches = list(node_properties(root, expected['compatible']))
        if len(matches) != 1:
            raise Blocked('DEVICE_TREE_NODE_COUNT_MISMATCH:' + role)
        node = matches[0]
        node_reg(node, expected['reg'])
        status = node / 'status'
        if status.exists() and read_cstring(status) != 'okay':
            raise Blocked('DEVICE_TREE_NODE_DISABLED:' + role)
        if role == 'capture':
            if read_cstring(node / 'dma-names') != 'rx' or read_cstring(node / 'clock-names') != 'control':
                raise Blocked('DEVICE_TREE_CAPTURE_DMA_CLOCK_NAMES_MISMATCH')
            dmas = read_u32_cells((node / 'dmas').read_bytes())
            if len(dmas) != 2 or dmas[1] != 1:
                raise Blocked('DEVICE_TREE_CAPTURE_DMA_SPECIFIER_MISMATCH')
            capture_dmas = dmas
            gpio_expectations = {'acquisition-enable-gpios': 78, 'handoff-gpios': 79}
            for name, line in gpio_expectations.items():
                cells = read_u32_cells((node / name).read_bytes())
                if len(cells) != 3 or cells[1:] != (line, 0):
                    raise Blocked('DEVICE_TREE_GPIO_MISMATCH:' + name)
        else:
            names = read_cstrings(node / 'clock-names')
            if names != ['s_axi_lite_aclk', 'm_axi_s2mm_aclk']:
                raise Blocked('DEVICE_TREE_DMA_CLOCK_NAMES_MISMATCH')
            channels = list(node.glob('dma-channel'))
            if len(channels) != 1 or read_u32_cells((channels[0] / 'interrupts').read_bytes()) != (0, 89, 4):
                raise Blocked('DEVICE_TREE_DMA_IRQ_MISMATCH')
            phandle = (node / 'phandle').read_bytes()
            if len(phandle) != 4 or capture_dmas is None or int.from_bytes(phandle, 'big') != capture_dmas[0]:
                raise Blocked('DEVICE_TREE_DMA_PHANDLE_MISMATCH')
    return {'model': model, 'capture_reg': ['0x80000000', '0x1000'],
            'dma_reg': ['0x80010000', '0x1000'], 'dma_irq_gic': 89,
            'acquisition_enable_mio': 78, 'handoff_mio': 79}


def open_device_paths():
    busy = []
    for proc in Path('/proc').iterdir():
        if not proc.name.isdecimal():
            continue
        try:
            fds = (proc / 'fd').iterdir()
            for fd in fds:
                try:
                    target = os.readlink(fd)
                except (FileNotFoundError, ProcessLookupError, PermissionError):
                    continue
                if target == str(DSP_DEVICE) or re.fullmatch(r'/dev/dri/card[0-9]+', target):
                    busy.append((proc.name, target))
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue
    return busy


def read_live_identity(device= DSP_DEVICE):
    import fcntl as linux_fcntl
    fd = os.open(str(device), os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        payload = bytearray(96)
        linux_fcntl.ioctl(fd, IDENTITY_IOCTL, payload, True)
    finally:
        os.close(fd)
    words = struct.unpack('<24I', payload)
    return dict(zip(IDENTITY_NAMES, words))


def maintenance_state(maintenance_lock_held=False):
    command = '/usr/sbin/eeg-maintenance-check'
    argv = [command, '--json']
    if maintenance_lock_held:
        argv.append('--maintenance-lock-held')
    try:
        result = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Blocked('MAINTENANCE_STATE_CHECK_FAILED:' + type(exc).__name__) from exc
    if result.returncode or len(result.stdout) > MAX_MANIFEST_BYTES:
        raise Blocked('MAINTENANCE_STATE_CHECK_FAILED')
    state = json.loads(result.stdout.decode('utf-8', 'strict'))
    if state.get('schema') != 'eeg.zu3eg.maintenance_state.v1' or state.get('target_state') != 'known':
        reasons = ','.join(state.get('reasons', []))
        raise Blocked('HARDWARE_NOT_VERIFIED_IDLE:' + reasons)
    return state


def persistent_root_check():
    root_rows = [line.split() for line in Path('/proc/self/mounts').read_text().splitlines()
                 if len(line.split()) >= 4 and line.split()[1] == '/']
    if len(root_rows) != 1 or root_rows[0][2] != 'ext4' or 'rw' not in root_rows[0][3].split(','):
        raise Blocked('PERSISTENT_WRITABLE_EXT4_ROOT_REQUIRED')


def verify_fit_file(path, record):
    if path.is_symlink() or not path.is_file():
        raise Blocked('RUNNING_BOOT_FIT_FILE_REQUIRED')
    if path.stat().st_size != record.get('bytes') or sha(path) != record.get('sha256'):
        raise Blocked('RUNNING_BOOT_FIT_HASH_MISMATCH')
    return {'path': str(path), 'bytes': path.stat().st_size, 'sha256': sha(path)}


def runtime_preflight(bundle_dir, manifest, files, maintenance_lock_held=False):
    if os.geteuid() != 0:
        raise Blocked('ROOT_REQUIRED_FOR_PL_UPDATE')
    persistent_root_check()
    baseline = json_read(BASELINE)
    for key in ('product', 'kernel_release', 'driver_abi', 'terminal_abi', 'pl_abi', 'pl_build_id'):
        if baseline.get(key) != manifest.get('system_identity', {}).get(key):
            raise Blocked('SYSTEM_IDENTITY_MISMATCH:' + key)
    if os.uname().release != manifest.get('kernel_release'):
        raise Blocked('RUNNING_KERNEL_RELEASE_MISMATCH')
    module = Path('/lib/modules') / manifest['kernel_release'] / 'extra/eeg_dsp_dma_v2.ko'
    if module.is_symlink() or not module.is_file():
        raise Blocked('DSP_MODULE_PATH_INVALID')
    if sha(module) != manifest['files']['dsp_module']['sha256']:
        raise Blocked('DSP_MODULE_HASH_MISMATCH')
    for role, field in (('update_bitstream', 'update'), ('rollback_bitstream', 'rollback')):
        firmware_name = manifest.get('firmware_names', {}).get(field, '')
        if not re.fullmatch(r'eeg/[A-Za-z0-9_.-]+\.bin', firmware_name):
            raise Blocked('FIRMWARE_NAME_INVALID:' + field)
        firmware = Path('/lib/firmware') / firmware_name
        if firmware.is_symlink() or not firmware.is_file() or sha(firmware) != manifest['files'][role]['sha256']:
            raise Blocked('INSTALLED_FIRMWARE_HASH_MISMATCH:' + field)
    modinfo = subprocess.run(['modinfo', '-F', 'vermagic', str(module)], stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True, timeout=10, check=False)
    if modinfo.returncode or modinfo.stdout.strip() != manifest['module_vermagic']:
        raise Blocked('DSP_MODULE_VERMAGIC_MISMATCH')
    if not any(line.split() and line.split()[0] == MODULE_NAME for line in Path('/proc/modules').read_text().splitlines()):
        raise Blocked('DSP_MODULE_NOT_LOADED')
    if open_device_paths():
        raise Blocked('HARDWARE_DEVICE_FD_STILL_OPEN')
    state = maintenance_state(maintenance_lock_held)
    current = read_live_identity()
    error = identity_error(current, manifest['identity'])
    if error:
        raise Blocked('CURRENT_PL_IDENTITY_NOT_MATCHED:' + error)
    dt_audit = check_live_device_tree(Path('/proc/device-tree'))
    fit_identity = verify_fit_file(BOOT_FIT, manifest['files']['system_fit'])
    firmware_manager = find_firmware_manager()
    return {'baseline': baseline, 'maintenance': state, 'pl_identity': current,
            'device_tree': dt_audit, 'firmware_manager': firmware_manager,
            'system_fit': fit_identity,
            'dsp_module': {'path': str(module), 'bytes': module.stat().st_size, 'sha256': sha(module),
                           'vermagic': modinfo.stdout.strip()}}


def find_firmware_manager():
    root = Path('/sys/class/fpga_manager')
    managers = sorted(p for p in root.glob('fpga*') if (p / 'state').is_file() and (p / 'firmware').exists())
    if len(managers) != 1:
        raise Blocked('EXACTLY_ONE_FPGA_MANAGER_FIRMWARE_INTERFACE_REQUIRED')
    manager = managers[0]
    try:
        state = read_cstring(manager / 'state')
        firmware = manager / 'firmware'
        if not manager.resolve().is_relative_to(Path('/sys/devices').resolve()):
            raise Blocked('FPGA_MANAGER_DEVICE_PATH_UNEXPECTED')
        if not os.access(firmware, os.W_OK):
            raise Blocked('FPGA_MANAGER_FIRMWARE_STORE_NOT_WRITABLE')
    except OSError as exc:
        raise Blocked('FPGA_MANAGER_INTERFACE_UNAVAILABLE') from exc
    if not manager_state_allowed(state):
        raise Blocked('FPGA_MANAGER_NOT_OPERATING:' + state)
    return {'device': manager.name, 'path': str(manager), 'state': state}


def read_plan(bundle_dir, manifest_path, maintenance_lock_held=False):
    manifest = json_read(manifest_path)
    files = verify_bundle(bundle_dir, manifest)
    checks = {}
    try:
        checks = runtime_preflight(bundle_dir, manifest, files, maintenance_lock_held)
        blockers = []
    except (Blocked, OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        blockers = [str(exc)]
    return {'schema': 'eeg.zu3eg.pl_update_plan.v1', 'release': manifest.get('release'),
            'manifest': {'path': str(manifest_path), 'bytes': manifest_path.stat().st_size,
                         'sha256': sha(manifest_path)},
            'files': {role: {'path': str(path), 'bytes': path.stat().st_size, 'sha256': sha(path)}
                      for role, path in files.items()},
            'checks': checks, 'blockers': blockers, 'can_apply': not blockers,
            'apply_requirements': ['root on persistent writable ext4 root',
                                   '--confirm-manifest-sha256 must equal the plan digest',
                                   '--adc-isolated is a current physical operator confirmation',
                                   'owner, DMA, IRQ, FPGA manager, DT, module and PL identity checks rerun under lock'],
            'write_scope': ['ZynqMP FPGA manager firmware operation',
                            'no SD/eMMC/QSPI partitions or boot files', 'no kernel rebuild or module replacement'],
            'rollback': 'On any load or ABI failure, request the exact manifest rollback bitstream and verify its PL ABI. A failed/mismatched rollback remains UNKNOWN and is recorded as INCOMPLETE.'}


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name('.' + path.name + '.new')
    if temporary.exists() or temporary.is_symlink():
        raise Blocked('UNFINISHED_RECEIPT_REQUIRES_REVIEW:' + str(temporary))
    data = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode('utf-8')
    fd = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    dfd = os.open(str(path.parent), os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


_PL_DRIVER_EVENTS = []
_PL_LIFECYCLE = None

def _driver_event(name, detail):
    _PL_DRIVER_EVENTS.append({'event': name, 'detail': detail, 'monotonic_ns': time.monotonic_ns()})

def load_image(manager, firmware_name, timeout=60):
    if not re.fullmatch(r'eeg/[A-Za-z0-9_.-]+\.bin', firmware_name):
        raise Blocked('UNSAFE_FIRMWARE_NAME')
    firmware_path = Path('/lib/firmware') / firmware_name
    if firmware_path.is_symlink() or not firmware_path.is_file():
        raise Blocked('FIRMWARE_FILE_MISSING:' + firmware_name)
    global _PL_LIFECYCLE
    if _PL_LIFECYCLE is None:
        from pl_driver_lifecycle import PlDriverLifecycle
        _PL_LIFECYCLE = PlDriverLifecycle(event=_driver_event)
    _PL_LIFECYCLE.retire()
    _driver_event('FPGA_FIRMWARE_WRITE_REQUEST', firmware_name)
    with (Path(manager['path']) / 'firmware').open('w', encoding='ascii') as output:
        output.write(firmware_name + '\n')
        output.flush()
    deadline = time.monotonic() + timeout
    last = 'unknown'
    while time.monotonic() < deadline:
        last = read_cstring(Path(manager['path']) / 'state').strip()
        if last == 'operating':
            _driver_event('FPGA_OPERATING', firmware_name)
            _PL_LIFECYCLE.restore()
            return last
        if last in ('write_error', 'error'):
            raise Blocked('FPGA_MANAGER_WRITE_FAILED:' + last)
        time.sleep(0.25)
    raise Blocked('FPGA_MANAGER_STATE_TIMEOUT:' + last)


def apply(args):
    if fcntl is None:
        raise Blocked('LINUX_FLOCK_REQUIRED')
    if os.geteuid() != 0:
        raise Blocked('ROOT_REQUIRED_FOR_PL_UPDATE')
    if not args.adc_isolated:
        raise Blocked('OPERATOR_ADC_ISOLATION_CONFIRMATION_REQUIRED')
    if not re.fullmatch(r'[0-9a-fA-F]{64}', args.confirm_manifest_sha256 or ''):
        raise Blocked('EXACT_MANIFEST_SHA256_CONFIRMATION_REQUIRED')
    lock_path = RUN / 'maintenance.lock'
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    marker = RUN / 'maintenance.active'
    owns_marker = False
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        marker_fd = os.open(str(marker), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(marker_fd, 'w', encoding='ascii') as output:
            owns_marker = True
            output.write('pl-update\n')
            output.flush()
            os.fsync(output.fileno())
        return apply_locked(args)
    finally:
        if owns_marker and marker.exists() and marker.is_file() and marker.read_text(encoding='ascii', errors='ignore') == 'pl-update\n':
            marker.unlink()
        os.close(lock_fd)


def apply_locked(args):
    manifest_path = args.manifest.resolve(strict=True)
    manifest_digest = sha(manifest_path)
    if manifest_digest != args.confirm_manifest_sha256.lower():
        raise Blocked('CONFIRMED_MANIFEST_HASH_MISMATCH')
    manifest = json_read(manifest_path)
    bundle_dir = manifest_path.parent
    plan = read_plan(bundle_dir, manifest_path, maintenance_lock_held=True)
    if plan['blockers']:
        raise Blocked('PL_UPDATE_BLOCKED:' + ','.join(plan['blockers']))
    if not plan['can_apply']:
        raise Blocked('PL_UPDATE_PLAN_NOT_APPLICABLE')
    files = verify_bundle(bundle_dir, manifest)
    for role, field in (('update_bitstream', 'update'), ('rollback_bitstream', 'rollback')):
        firmware = Path('/lib/firmware') / manifest['firmware_names'][field]
        if firmware.is_symlink() or not firmware.is_file() or sha(firmware) != manifest['files'][role]['sha256']:
            raise Blocked('INSTALLED_FIRMWARE_HASH_MISMATCH:' + field)
    previous = runtime_preflight(bundle_dir, manifest, files, maintenance_lock_held=True)
    if identity_error(previous['pl_identity'], manifest['identity']):
        raise Blocked('PRE_UPDATE_PL_IDENTITY_CHANGED')
    manager = previous['firmware_manager']
    receipt = {'schema': 'eeg.zu3eg.pl_update_receipt.v1', 'started_at': datetime.datetime.now().astimezone().isoformat(),
               'manifest_sha256': manifest_digest, 'plan': plan, 'result': 'INCOMPLETE',
               'update_load': 'NOT_STARTED', 'rollback_load': 'NOT_REQUIRED',
               'post_update_identity': None, 'post_rollback_identity': None, 'driver_lifecycle': _PL_DRIVER_EVENTS, 'roundtrip_selftest': bool(getattr(args, 'roundtrip_selftest', False))}
    log_dir = Path('/var/log/eeg')
    log_dir.mkdir(parents=True, exist_ok=True)
    receipt_path = log_dir / ('pl-update-' + datetime.datetime.now().strftime('%Y%m%dT%H%M%S') + '.json')
    try:
        maintenance_state(maintenance_lock_held=True)
        receipt['update_load'] = 'REQUESTED'
        load_image(manager, manifest['firmware_names']['update'])
        identity = read_live_identity()
        receipt['post_update_identity'] = identity
        error = identity_error(identity, manifest['identity'])
        if error:
            receipt['update_load'] = 'ABI_MISMATCH:' + error
            raise Blocked('POST_UPDATE_PL_ABI_MISMATCH:' + error)
        receipt['update_load'] = 'VERIFIED'
        receipt['result'] = 'PL_UPDATED_AND_ABI_VERIFIED'
        if getattr(args, 'roundtrip_selftest', False):
            raise Blocked('INTENTIONAL_VERIFIED_UPDATE_ROUNDTRIP_SELFTEST')
    except (Blocked, OSError, RuntimeError, subprocess.SubprocessError) as failure:
        receipt['failure'] = str(failure)
        receipt['rollback_load'] = 'REQUESTED'
        try:
            load_image(manager, manifest['firmware_names']['rollback'])
            rollback_identity = read_live_identity()
            receipt['post_rollback_identity'] = rollback_identity
            error = identity_error(rollback_identity, manifest['identity'])
            if error:
                receipt['rollback_load'] = 'IDENTITY_UNKNOWN:' + error
                receipt['result'] = 'UNKNOWN_ROLLBACK_NOT_VERIFIED'
            else:
                receipt['rollback_load'] = 'VERIFIED'
                receipt['result'] = 'SELFTEST_UPDATE_AND_ROLLBACK_VERIFIED' if getattr(args, 'roundtrip_selftest', False) and receipt['update_load'] == 'VERIFIED' else 'UPDATE_FAILED_ROLLED_BACK_AND_VERIFIED'
        except (Blocked, OSError, RuntimeError, subprocess.SubprocessError) as rollback_failure:
            receipt['rollback_load'] = 'FAILED_OR_UNKNOWN:' + str(rollback_failure)
            receipt['result'] = 'UNKNOWN_ROLLBACK_NOT_VERIFIED'
        receipt['finished_at'] = datetime.datetime.now().astimezone().isoformat()
        atomic_json(receipt_path, receipt)
        if receipt['result'] == 'SELFTEST_UPDATE_AND_ROLLBACK_VERIFIED':
            print(json.dumps({'result': receipt['result'], 'receipt': str(receipt_path), 'manifest_sha256': manifest_digest}))
            return 0
        raise Blocked(receipt['result'] + ':receipt=' + str(receipt_path)) from failure
    receipt['finished_at'] = datetime.datetime.now().astimezone().isoformat()
    atomic_json(receipt_path, receipt)
    print(json.dumps({'result': receipt['result'], 'receipt': str(receipt_path),
                      'manifest_sha256': manifest_digest}, sort_keys=True))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest='command', required=True)
    plan_parser = subs.add_parser('plan', help='read-only validation of package, idle state and current PL')
    plan_parser.add_argument('--manifest', type=Path, default=BUNDLE / 'manifest.json')
    apply_parser = subs.add_parser('apply', help='perform one confirmed, maintenance-locked PL update')
    apply_parser.add_argument('--manifest', type=Path, default=BUNDLE / 'manifest.json')
    apply_parser.add_argument('--confirm-manifest-sha256', required=True)
    apply_parser.add_argument('--adc-isolated', action='store_true')
    apply_parser.add_argument('--roundtrip-selftest', action='store_true', help='Load/verify update, then intentionally exercise rollback and verify prior-good; not a real update failure.')
    args = parser.parse_args(argv)
    try:
        if args.command == 'plan':
            manifest_path = args.manifest.resolve(strict=True)
            plan = read_plan(manifest_path.parent, manifest_path)
            print(json.dumps(plan, ensure_ascii=False, sort_keys=True, indent=2))
            return 0 if plan['can_apply'] else 3
        return apply(args)
    except (Blocked, OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(json.dumps({'schema': 'eeg.zu3eg.pl_update_result.v1',
                          'result': 'BLOCKED_OR_UNKNOWN', 'reason': str(exc)},
                         ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return 3


if __name__ == '__main__':
    sys.exit(main())
