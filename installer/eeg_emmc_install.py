#!/usr/bin/env python3
"""Plan and install the verified SD payload to identified eMMC user storage.

Planning is read-only. Applying requires an exact CID, a local backup decision,
the same running system identity, and a live hardware-idle proof. Boot partitions,
RPMB, QSPI and host media are never installation destinations.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime
import gzip
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import struct
import zlib

SCHEMA = "eeg.zu3eg.system_payload.v1"
ROLES = {"boot_bin": "BOOT.BIN", "fit": "image.ub", "boot_script": "boot.scr", "rootfs_tar": "rootfs.tar.gz"}
IDLE_KEYS = {"acquisition_enable": "low", "capture": "idle", "stimulus": "off", "dma": "idle",
             "irq": "quiesced", "owner": "released", "target_state": "known"}
PRIVATE_CONFIG = ("etc/shadow", "etc/ssh/ssh_host_rsa_key", "etc/ssh/ssh_host_rsa_key.pub",
                  "etc/ssh/ssh_host_ecdsa_key", "etc/ssh/ssh_host_ecdsa_key.pub",
                  "etc/ssh/ssh_host_ed25519_key", "etc/ssh/ssh_host_ed25519_key.pub",
                  "etc/eeg-system/network.json", "root/.ssh/authorized_keys")
SYSTEMD_MASK_PATHS = frozenset("etc/systemd/system/" + name for name in (
    "dropbear.service", "dropbear.socket", "dropbear@.service", "sshd.service", "sshd.socket",
    "systemd-networkd.service", "systemd-networkd.socket", "systemd-networkd-wait-online.service",
    "networking.service", "connman.service", "weston.service", "display-manager.service",
    "tcf-agent.service", "xinetd.service"))
OVERRIDE_PATHS = frozenset(PRIVATE_CONFIG) | {"etc/fstab"}
REQUIRED_ROOTFS_PATHS = (
    "sbin/init", "usr/bin/gcc", "usr/bin/g++", "usr/bin/make", "usr/bin/cmake",
    "usr/bin/pkg-config", "usr/bin/python3", "opt/eeg-qt/bin/qmake", "opt/eeg-qt/bin/moc", "opt/eeg-qt/bin/rcc",
    "opt/eeg-qt/bin/uic", "opt/eeg-qt/include/QtCore/QtCore", "opt/eeg-qt/lib/libQt5Core.so.5",
    "opt/eeg-qt/plugins/platforms", "usr/sbin/sshd", "usr/bin/ssh", "usr/bin/scp", "usr/bin/sftp", "usr/bin/ssh-keygen",
    "etc/shadow", "etc/ssh/sshd_config", "usr/sbin/eeg-network", "usr/sbin/eeg-app-update", "usr/sbin/eeg-app-rollback",
    "usr/sbin/eeg-system-check", "usr/sbin/eeg-maintenance-check", "usr/sbin/burn-emmc.sh",
    "usr/lib/eeg-system/eeg_emmc_install.py",
    "etc/hostname", "etc/profile.d/eeg.sh",
    "opt/eeg/current/bin/eeg-ui", "opt/eeg/current/bin/eegctl", "opt/eeg/current/bin/eeg-terminal",
    "opt/eeg/current/bin/eeg-display", "opt/eeg/workspace", "var/lib/eeg-operator/recordings",
    "etc/eeg-system/baseline.json", "sbin/modprobe", "sbin/depmod", "etc/modules-load.d/eeg-dsp.conf",
    "usr/bin/eeg-ui", "usr/bin/eegctl", "usr/lib/eeg-system/prepare",
    "usr/lib/eeg-system/storage-check", "usr/lib/eeg-system/dsp-prepare",
    "usr/lib/eeg-system/ssh-service", "usr/lib/eeg-system/password-ready",
    "usr/sbin/eeg-serial-login",
)
INIT_ROOTFS_PATHS = {
    "sysv": (
        "sbin/init.sysvinit", "sbin/getty", "sbin/sulogin", "etc/inittab", "etc/init.d/rcS",
        "etc/init.d/rc", "etc/init.d/eeg-system", "etc/rcS.d/S98eeg-system",
        "etc/rc0.d/K98eeg-system", "etc/rc6.d/K98eeg-system", "usr/sbin/eeg-serial-login",
        "usr/lib/eeg-system/password-ready",
    ),
    "systemd": (
        "lib/systemd/systemd", "usr/sbin/eeg-serial-login", "usr/lib/eeg-system/password-ready",
        "etc/systemd/system/default.target",
        "etc/systemd/system/eeg-prepare.service", "etc/systemd/system/eeg-network.service",
        "etc/systemd/system/eeg-dsp.service", "etc/systemd/system/eeg-sshd.service",
        "etc/systemd/system/eeg-menu.service",
        "etc/systemd/system/serial-getty@ttyPS0.service.d/eeg.conf",
        "etc/systemd/system/multi-user.target.wants/eeg-prepare.service",
        "etc/systemd/system/multi-user.target.wants/eeg-network.service",
        "etc/systemd/system/multi-user.target.wants/eeg-dsp.service",
        "etc/systemd/system/multi-user.target.wants/eeg-sshd.service",
        "etc/systemd/system/multi-user.target.wants/eeg-menu.service",
        "lib/systemd/system/serial-getty@.service",
        "etc/systemd/system/getty.target.wants/serial-getty@ttyPS0.service",
        "etc/systemd/system/serial-getty@ttyPS0.service.d/eeg.conf",
        "etc/systemd/system/dropbear.service", "etc/systemd/system/dropbear.socket",
        "etc/systemd/system/dropbear@.service", "etc/systemd/system/sshd.service",
        "etc/systemd/system/sshd.socket", "etc/systemd/system/systemd-networkd.service",
        "etc/systemd/system/systemd-networkd.socket", "etc/systemd/system/systemd-networkd-wait-online.service",
        "etc/systemd/system/networking.service", "etc/systemd/system/connman.service",
        "etc/systemd/system/weston.service", "etc/systemd/system/display-manager.service",
        "etc/systemd/system/tcf-agent.service", "etc/systemd/system/xinetd.service",
    ),
}
REQUIRED_TOOLS = ("sfdisk", "blockdev", "udevadm", "mkfs.vfat", "mkfs.ext4", "mount", "umount",
                  "blkid", "tar", "sync", "fsck.fat", "e2fsck")


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def fail(message):
    raise RuntimeError(message)


def sha(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def json_read(path, maximum=4 * 1024 * 1024):
    data = path.read_bytes()
    if len(data) > maximum:
        fail("BOUNDED_JSON_REQUIRED:" + str(path))
    if data.startswith(b"\xef\xbb\xbf") or b"\0" in data:
        fail("UTF8_WITHOUT_BOM_OR_NUL_REQUIRED:" + str(path))
    return json.loads(data.decode("utf-8"))


def safe_relative(name):
    if not isinstance(name, str) or not name:
        fail("RELATIVE_PAYLOAD_PATH_REQUIRED")
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or "\\" in name or "\0" in name or name in (".", ""):
        fail("UNSAFE_PAYLOAD_PATH:" + name)
    return path


def safe_root_path(name):
    if not isinstance(name, str) or not name or "\\" in name or "\0" in name:
        fail("INVALID_ROOTFS_REQUIRED_PATH")
    if name.startswith("/"):
        name = name[1:]
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or path.as_posix() != name:
        fail("UNSAFE_ROOTFS_REQUIRED_PATH:" + name)
    return path.as_posix()


def resolve_archive_path(name, names, symlinks):
    pending, parts, hops = list(PurePosixPath(safe_root_path(name)).parts), [], 0
    while pending:
        part = pending.pop(0)
        current = "/".join(parts + [part])
        if current in symlinks:
            hops += 1
            if hops > 40:
                fail("ROOTFS_SYMLINK_CYCLE_IN_REQUIRED_PATH:" + name)
            link = PurePosixPath(symlinks[current])
            if link.is_absolute():
                parts.clear()
            replacement = []
            for component in link.parts:
                if component in ("", "/", "."):
                    continue
                if component == "..":
                    if not parts:
                        fail("ROOTFS_SYMLINK_ESCAPES_ROOT:" + current)
                    parts.pop()
                else:
                    replacement.append(component)
            pending = replacement + pending
        else:
            parts.append(part)
    return "/".join(parts)


def identity_equal(actual, expected):
    if not isinstance(expected, dict) or not expected:
        fail("SYSTEM_IDENTITY_REQUIRED")
    for key, value in expected.items():
        if key not in ("kernel_release", "driver_abi", "terminal_abi", "pl_abi") or actual.get(key) != value:
            fail("RUNNING_SYSTEM_IDENTITY_MISMATCH:" + key)


def read_payload(path):
    original = path
    if original.is_symlink():
        fail("MANIFEST_MUST_NOT_BE_SYMLINK")
    path = path.resolve(strict=True)
    manifest = json_read(path)
    if manifest.get("schema") != SCHEMA or manifest.get("product") != "EEG-ZU3EG":
        fail("MATCHED_SYSTEM_PAYLOAD_MANIFEST_REQUIRED")
    for key in ("release", "kernel_release", "abi_id"):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.+-]{0,127}", str(manifest.get(key, ""))):
            fail("INVALID_MANIFEST_IDENTITY:" + key)
    if manifest.get("boot_root_selector") != "running_mmc_partuuid":
        fail("SD_EMMC_COMMON_PARTUUID_BOOT_REQUIRED")
    layout = manifest.get("layout", {})
    if type(layout.get("boot_mib")) is not int or not 128 <= layout["boot_mib"] <= 2048:
        fail("BOOT_PARTITION_SIZE_REQUIRED")
    if type(layout.get("root_min_free_bytes")) is not int or layout["root_min_free_bytes"] < 256 * 1024**2:
        fail("ROOT_DEVELOPMENT_FREE_SPACE_REQUIRED")
    rows = manifest.get("artifacts")
    if not isinstance(rows, list) or len(rows) != len(ROLES):
        fail("EXACT_BOOT_FIT_SCRIPT_ROOTFS_INVENTORY_REQUIRED")
    artifacts, summary = {}, []
    for item in rows:
        role = item.get("role")
        if role not in ROLES or role in artifacts:
            fail("DUPLICATE_OR_UNSUPPORTED_PAYLOAD_ROLE")
        relative = safe_relative(item.get("path"))
        source = path.parent
        for part in relative.parts:
            source = source / part
            if source.is_symlink():
                fail("PACKAGE_ARTIFACT_PATH_HAS_SYMLINK_COMPONENT:" + role)
        target = source.resolve(strict=True)
        if not target.is_relative_to(path.parent) or not target.is_file():
            fail("REGULAR_IN_PACKAGE_ARTIFACT_REQUIRED:" + role)
        length = item.get("bytes")
        expected_sha = item.get("sha256")
        if type(length) is not int or length <= 0 or not re.fullmatch(r"[0-9a-f]{64}", str(expected_sha)):
            fail("VALID_ARTIFACT_LENGTH_HASH_REQUIRED:" + role)
        if target.stat().st_size != length or sha(target) != expected_sha:
            fail("PAYLOAD_LENGTH_OR_SHA_MISMATCH:" + role)
        artifacts[role] = target
        summary.append({"role": role, "bytes": length, "sha256": expected_sha, "verified": True})
    root = manifest.get("rootfs", {})
    if type(root.get("unpacked_regular_bytes")) is not int or root["unpacked_regular_bytes"] <= 0:
        fail("ROOTFS_UNPACKED_LENGTH_REQUIRED")
    if type(root.get("member_count")) is not int or not 1 <= root["member_count"] <= 1000000:
        fail("ROOTFS_MEMBER_COUNT_REQUIRED")
    required = root.get("required_paths")
    if not isinstance(required, list) or len(required) < 6:
        fail("ROOTFS_REQUIRED_RUNTIME_DEVELOPMENT_PATHS_REQUIRED")
    for name in required:
        safe_relative(name)
    identity = manifest.get("system_identity")
    if not isinstance(identity, dict) or identity.get("kernel_release") != manifest["kernel_release"]:
        fail("ROOTFS_RUNNING_SYSTEM_IDENTITY_REQUIRED")
    return manifest, artifacts, summary


def audit_uboot_script(path):
    raw = path.read_bytes()
    if len(raw) < 64:
        fail("UBOOT_LEGACY_SCRIPT_HEADER_REQUIRED")
    fields = struct.unpack(">7I4B32s", raw[:64])
    magic, header_crc, _, size, _, _, data_crc, _, _, image_type, compression, _ = fields
    if magic != 0x27051956 or image_type != 6 or compression not in (0, 1):
        fail("UBOOT_LEGACY_SCRIPT_TYPE_OR_COMPRESSION_INVALID")
    header = bytearray(raw[:64])
    header[4:8] = b"\0" * 4
    if zlib.crc32(header) & 0xffffffff != header_crc:
        fail("UBOOT_SCRIPT_HEADER_CRC_MISMATCH")
    payload = raw[64:64 + size]
    if len(payload) != size or zlib.crc32(payload) & 0xffffffff != data_crc:
        fail("UBOOT_SCRIPT_DATA_CRC_OR_LENGTH_MISMATCH")
    if len(raw) != 64 + size:
        fail("UBOOT_SCRIPT_TRAILING_DATA")
    if compression == 1:
        try:
            payload = gzip.decompress(payload)
        except OSError:
            fail("UBOOT_SCRIPT_GZIP_DECOMPRESSION_FAILED")
    if len(payload) < 8:
        fail("UBOOT_SCRIPT_COMPONENT_TABLE_TRUNCATED")
    script_bytes, terminating_offset = struct.unpack_from(">II", payload, 0)
    if terminating_offset != 0 or script_bytes != len(payload) - 8:
        fail("UBOOT_SCRIPT_COMPONENT_TABLE_INVALID")
    body = payload[8:]
    if b"\0" in body:
        fail("UBOOT_SCRIPT_TEXT_HAS_NUL")
    try:
        body.decode("ascii", "strict")
    except UnicodeDecodeError:
        fail("UBOOT_SCRIPT_TEXT_NOT_ASCII")
    normalized = re.sub(rb"\s+", b" ", body)
    if not re.search(rb"part uuid mmc \$\{bootdev\}:2 rootpartuuid(?: |;|$)", normalized):
        fail("UBOOT_SCRIPT_MUST_READ_ROOT_PARTUUID_FROM_RUNNING_MMC")
    if not re.search(rb"root=PARTUUID=\$\{rootpartuuid\}(?: |;|['\"]|$)", normalized):
        fail("UBOOT_SCRIPT_MUST_BOOT_DYNAMIC_ROOT_PARTUUID")
    if not (re.search(rb"setenv bootdev \$\{devnum\}(?: |;|$)", normalized) or
            re.search(rb"setenv bootdev \$\{mmcdev\}(?: |;|$)", normalized)):
        fail("UBOOT_BOOTDEV_MUST_TRACK_ACTIVE_BOOT_DEVICE")
    for expression, error in (
            (rb"mmc rescan", "UBOOT_SCRIPT_MMC_RESCAN_REQUIRED"),
            (rb"fatload mmc \$\{bootdev\}:1", "UBOOT_SCRIPT_BOOT_PARTITION_MUST_FOLLOW_ACTIVE_DEVICE"),
            (rb"bootm \$\{[^}]+\}#config-3eg", "UBOOT_SCRIPT_MUST_BOOT_ZU3EG_FIT_CONFIGURATION")):
        if not re.search(expression, normalized):
            fail(error)
    partuuid_at = normalized.find(b"part uuid mmc ${bootdev}:2 rootpartuuid")
    bootargs_at = normalized.find(b"setenv bootargs", partuuid_at + 1)
    rootarg_at = normalized.find(b"root=PARTUUID=${rootpartuuid}", bootargs_at + 1)
    if partuuid_at < 0 or bootargs_at < partuuid_at or rootarg_at < bootargs_at:
        fail("UBOOT_SCRIPT_MUST_SELECT_ROOT_PARTUUID_BEFORE_SETTING_BOOTARGS")
    return {"bytes": len(raw), "header_crc_verified": True, "data_crc_verified": True,
            "script_type": "legacy script", "component_table_verified": True,
            "script_body_sha256": hashlib.sha256(body).hexdigest(),
            "active_mmc_partuuid_root_verified": True}


def fdt_properties(blob):
    if len(blob) < 40 or blob[:4] != b"\xd0\x0d\xfe\xed":
        fail("FIT_FDT_MAGIC_INVALID")
    total = struct.unpack_from(">I", blob, 4)[0]
    off_struct, off_strings = struct.unpack_from(">II", blob, 8)
    size_strings, size_struct = struct.unpack_from(">II", blob, 32)
    if total > len(blob) or total < 40 or off_struct + size_struct > total or off_strings + size_strings > total:
        fail("FIT_FDT_BOUNDS_INVALID")
    structure = blob[off_struct:off_struct + size_struct]
    strings = blob[off_strings:off_strings + size_strings]
    nodes, stack, pos = {}, [], 0
    while pos + 4 <= len(structure):
        token = struct.unpack_from(">I", structure, pos)[0]
        pos += 4
        if token == 1:
            end = structure.find(b"\0", pos)
            if end < 0:
                fail("FIT_FDT_NODE_TERMINATOR_MISSING")
            stack.append(structure[pos:end].decode("ascii", "strict"))
            pos = (end + 4) & ~3
            nodes.setdefault("/" + "/".join(value for value in stack if value), {})
        elif token == 2:
            if not stack:
                fail("FIT_FDT_NODE_STACK_UNBALANCED")
            stack.pop()
        elif token == 3:
            if pos + 8 > len(structure) or not stack:
                fail("FIT_FDT_PROPERTY_HEADER_TRUNCATED")
            length, name_offset = struct.unpack_from(">II", structure, pos)
            pos += 8
            end = strings.find(b"\0", name_offset)
            if name_offset >= len(strings) or end < 0 or pos + length > len(structure):
                fail("FIT_FDT_PROPERTY_BOUNDS_INVALID")
            name = strings[name_offset:end].decode("ascii", "strict")
            path = "/" + "/".join(value for value in stack if value)
            nodes[path][name] = structure[pos:pos + length]
            pos = (pos + length + 3) & ~3
        elif token == 4:
            continue
        elif token == 9:
            if stack:
                fail("FIT_FDT_END_WITH_OPEN_NODE")
            return total, nodes
        else:
            fail("FIT_FDT_UNKNOWN_TOKEN")
    fail("FIT_FDT_END_MISSING")


def audit_fit(path, manifest):
    fit = path.read_bytes()
    total, nodes = fdt_properties(fit)
    configs = {name.rsplit("/", 1)[-1]: data for name, data in nodes.items()
               if name.startswith("/configurations/") and name.count("/") == 2}
    expected_config = manifest.get("fit_config")
    if expected_config is None:
        expected_config = "config-3eg"
    if expected_config != "config-3eg":
        fail("FIT_CONFIGURATION_MUST_MATCH_ZU3EG")
    if expected_config not in configs:
        fail("FIT_REQUIRED_ZU3EG_CONFIGURATION_MISSING")
    config = configs[expected_config]
    results = {"configuration": expected_config, "components": {}}
    ranges = {"kernel": ("kernel", "kernel", ("none",)),
              "fdt": ("fdt", "flat_dt", ("none",))}
    if "ramdisk" in config:
        ranges["ramdisk"] = ("ramdisk", "ramdisk", ("gzip", "none"))
    for role, (reference, expected_type, expected_compression) in ranges.items():
        if reference not in config:
            if role == "ramdisk":
                fail("FIT_RAMDISK_REQUIRED_FOR_COMPLETE_SYSTEM")
            fail("FIT_CONFIGURATION_COMPONENT_MISSING:" + role)
        image_name = config[reference].split(b"\0", 1)[0].decode("ascii", "strict")
        image_path = "/images/" + image_name
        image = nodes.get(image_path)
        if not image:
            fail("FIT_IMAGE_NODE_MISSING:" + image_name)
        if "data" in image:
            payload = image["data"]
        elif "data-position" in image and "data-size" in image:
            offset, length = int.from_bytes(image["data-position"], "big"), int.from_bytes(image["data-size"], "big")
            if offset + length > len(fit):
                fail("FIT_EXTERNAL_IMAGE_BOUNDS_INVALID:" + image_name)
            payload = fit[offset:offset + length]
        elif "data-offset" in image and "data-size" in image:
            offset, length = total + int.from_bytes(image["data-offset"], "big"), int.from_bytes(image["data-size"], "big")
            if offset + length > len(fit):
                fail("FIT_EXTERNAL_IMAGE_BOUNDS_INVALID:" + image_name)
            payload = fit[offset:offset + length]
        else:
            fail("FIT_IMAGE_PAYLOAD_MISSING:" + image_name)
        get_text = lambda key: image.get(key, b"").split(b"\0", 1)[0].decode("ascii", "strict")
        compression = get_text("compression")
        if get_text("type") != expected_type or compression not in expected_compression or get_text("arch") != "arm64":
            fail("FIT_IMAGE_METADATA_MISMATCH:" + role)
        if role == "kernel" and get_text("os") != "linux":
            fail("FIT_KERNEL_OS_MUST_BE_LINUX")
        digest = hashlib.sha256(payload).digest()
        hashes = [(name, prop) for name, prop in nodes.items() if name.startswith(image_path + "/hash-")]
        if not hashes or not any(node.get("algo") == b"sha256\0" and node.get("value") == digest for _, node in hashes):
            fail("FIT_SUBIMAGE_SHA256_NODE_INVALID:" + role)
        row = {"name": image_name, "bytes": len(payload), "sha256": digest.hex(),
               "type": expected_type, "compression": compression, "arch": "arm64"}
        if role == "kernel":
            load = image.get("load")
            entry = image.get("entry")
            if not load or not entry:
                fail("FIT_KERNEL_LOAD_ENTRY_REQUIRED")
            row["load"] = int.from_bytes(load, "big")
            row["entry"] = int.from_bytes(entry, "big")
            if row["load"] != row["entry"] or not 0 < row["load"] < 0x80000000:
                fail("FIT_KERNEL_LOAD_ENTRY_OUTSIDE_VERIFIED_DDR_RANGE")
            if ("Linux version " + manifest["kernel_release"]).encode("ascii") not in payload:
                fail("FIT_KERNEL_RELEASE_STRING_MISMATCH")
        if role == "fdt":
            _, dt_nodes = fdt_properties(payload)
            model = dt_nodes.get("/", {}).get("model", b"").split(b"\0", 1)[0]
            if b"3EG" not in model.upper() and b"ZU3EG" not in model.upper():
                fail("FIT_DTB_MODEL_NOT_ZU3EG")
            bootargs = dt_nodes.get("/chosen", {}).get("bootargs", b"").split(b"\0", 1)[0]
            if b"rdinit=" in bootargs or re.search(rb"root=(?:/dev/mmcblk[0-9]+p[0-9]+|/dev/ram[0-9]*)", bootargs):
                fail("FIT_DTB_MUST_NOT_FORCE_RAMDISK_OR_MMC_INDEX_ROOT")
            row["model"] = model.decode("ascii", "replace")
            row["bootargs"] = bootargs.decode("ascii", "replace")
        results["components"][role] = row
    return results


def audit_rootfs(path, expected, kernel_release, system_identity):
    names, symlinks, regular_bytes, embedded_identity, root_fstab, ssh_config = set(), {}, 0, None, None, None
    init_contract = {}
    with tarfile.open(path, mode="r|*") as archive:
        for item in archive:
            name = item.name
            while name.startswith("./"):
                name = name[2:]
            if name in ("", ".") and item.isdir():
                continue
            name = name.rstrip("/")
            safe_relative(name)
            if name in names:
                fail("DUPLICATE_ROOTFS_MEMBER:" + name)
            names.add(name)
            if len(names) > 1000000:
                fail("ROOTFS_MEMBER_LIMIT")
            if item.islnk():
                target = item.linkname
                while target.startswith("./"):
                    target = target[2:]
                safe_relative(target)
            if not (item.isfile() or item.isdir() or item.issym() or item.islnk() or item.ischr() or item.isblk() or item.isfifo()):
                fail("UNSUPPORTED_ROOTFS_MEMBER:" + name)
            parts = PurePosixPath(name).parts
            if any("/".join(parts[:index]) in symlinks for index in range(1, len(parts))):
                fail("ROOTFS_MEMBER_PARENT_IS_SYMLINK:" + name)
            if item.issym():
                link = item.linkname
                if not link or "\0" in link or "\\" in link:
                    fail("INVALID_ROOTFS_SYMLINK:" + name)
                components = [] if link.startswith("/") else list(PurePosixPath(name).parent.parts)
                for component in PurePosixPath(link).parts:
                    if component in ("", ".", "/"):
                        continue
                    if component == "..":
                        if not components:
                            fail("ROOTFS_SYMLINK_ESCAPES_ROOT:" + name)
                        components.pop()
                    else:
                        components.append(component)
                symlinks[name] = link
            if item.isfile():
                regular_bytes += item.size
                if name == "etc/eeg-system/baseline.json":
                    if item.size > 1024 * 1024:
                        fail("BOUNDED_BASELINE_JSON_REQUIRED")
                    embedded_identity = json.loads(archive.extractfile(item).read().decode("utf-8"))
                elif name == "etc/fstab":
                    if item.size > 1024 * 1024:
                        fail("BOUNDED_FSTAB_REQUIRED")
                    root_fstab = archive.extractfile(item).read().decode("utf-8")
                elif name == "etc/ssh/sshd_config":
                    if item.size > 1024 * 1024:
                        fail("BOUNDED_SSHD_CONFIG_REQUIRED")
                    ssh_config = archive.extractfile(item).read().decode("utf-8")
                elif name in ("etc/inittab", "etc/init.d/eeg-system", "etc/rcS.d/S98eeg-system",
                              "etc/rc0.d/K98eeg-system", "etc/rc6.d/K98eeg-system",
                              "etc/systemd/system/eeg-prepare.service", "etc/systemd/system/eeg-network.service",
                              "etc/systemd/system/eeg-dsp.service", "etc/systemd/system/eeg-sshd.service",
                              "etc/systemd/system/eeg-menu.service",
                              "etc/systemd/system/serial-getty@ttyPS0.service.d/eeg.conf"):
                    if item.size > 1024 * 1024:
                        fail("BOUNDED_INIT_CONTRACT_FILE_REQUIRED:" + name)
                    init_contract[name] = archive.extractfile(item).read().decode("utf-8")
    if regular_bytes != expected["unpacked_regular_bytes"] or len(names) != expected["member_count"]:
        fail("ROOTFS_UNPACKED_LENGTH_OR_MEMBER_COUNT_MISMATCH")
    required = {safe_root_path(value) for value in expected["required_paths"]}
    init_system = expected.get("init_system")
    if init_system not in INIT_ROOTFS_PATHS:
        fail("ROOTFS_INIT_SYSTEM_MUST_BE_EXPLICIT")
    compulsory = set(REQUIRED_ROOTFS_PATHS) | {"lib/modules/" + kernel_release + "/build/Makefile",
                                                "lib/modules/" + kernel_release + "/build/Module.symvers",
                                                "lib/modules/" + kernel_release + "/extra/eeg_dsp_dma_v2.ko"} | set(INIT_ROOTFS_PATHS[init_system])
    missing = sorted(name for name in compulsory - required)
    for name in sorted(required):
        if required_systemd_mask(name, symlinks.get(name), init_system):
            continue
        resolved = resolve_archive_path(name, names, symlinks)
        if resolved not in names and not any(entry.startswith(resolved.rstrip("/") + "/") for entry in names):
            missing.append(name)
    if missing:
        fail("ROOTFS_REQUIRED_MEMBERS_MISSING:" + ",".join(missing))
    expected_init = "lib/systemd/systemd" if init_system == "systemd" else "sbin/init.sysvinit"
    if resolve_archive_path("sbin/init", names, symlinks) != expected_init:
        fail("ROOTFS_INIT_TARGET_MISMATCH:" + init_system)
    if not any(name.startswith("lib/modules/" + kernel_release + "/") for name in names):
        fail("MATCHED_MODULE_DIRECTORY_MISSING")
    identity_equal(embedded_identity or {}, system_identity)
    root_entries = 0
    for line in (root_fstab or "").splitlines():
        fields = line.split()
        if fields and not fields[0].startswith("#") and len(fields) >= 2 and fields[1] == "/":
            if fields[0] != "/dev/root" and not fields[0].startswith(("UUID=", "PARTUUID=")):
                fail("ROOTFS_FSTAB_ROOT_SOURCE_MUST_BE_REWRITTEN_BY_INSTALLER")
            root_entries += 1
    if root_entries != 1:
        fail("ROOTFS_FSTAB_MUST_HAVE_ONE_REWRITABLE_ROOT_ENTRY")
    if ssh_config is None:
        fail("OPENSSH_SERVER_CONFIG_REQUIRED")
    if init_system == "sysv":
        inittab = init_contract.get("etc/inittab", "")
        if not re.search(r"(?m)^si::sysinit:/etc/init.d/rcS\s*$", inittab):
            fail("SYSV_INIT_MUST_RUN_RCS")
        if not re.search(r"(?m)^PS0:12345:respawn:/sbin/getty\s+.*ttyPS0\s+vt102\s*$", inittab):
            fail("SYSV_SERIAL_LOGIN_MUST_RESPAWN_ON_TTYPS0")
        if "/etc/init.d/eeg-system start" not in init_contract.get("etc/rcS.d/S98eeg-system", ""):
            fail("EEG_SYSTEM_MUST_START_FROM_SYSV_RCS")
        for name in ("etc/rc0.d/K98eeg-system", "etc/rc6.d/K98eeg-system"):
            if "/etc/init.d/eeg-system stop" not in init_contract.get(name, ""):
                fail("EEG_SYSTEM_MUST_STOP_ON_SYSV_SHUTDOWN:" + name)
        service = init_contract.get("etc/init.d/eeg-system", "")
        for fragment in ("/usr/lib/eeg-system/prepare", "/usr/sbin/sshd -t", "/usr/sbin/sshd -f /etc/ssh/sshd_config"):
            if fragment not in service:
                fail("SYSV_SERVICE_INTEGRATION_REQUIRED:" + fragment)
    else:
        enablement = {
            "etc/systemd/system/default.target": "lib/systemd/system/multi-user.target",
            "etc/systemd/system/multi-user.target.wants/eeg-prepare.service": "etc/systemd/system/eeg-prepare.service",
            "etc/systemd/system/multi-user.target.wants/eeg-network.service": "etc/systemd/system/eeg-network.service",
            "etc/systemd/system/multi-user.target.wants/eeg-dsp.service": "etc/systemd/system/eeg-dsp.service",
            "etc/systemd/system/multi-user.target.wants/eeg-sshd.service": "etc/systemd/system/eeg-sshd.service",
            "etc/systemd/system/multi-user.target.wants/eeg-menu.service": "etc/systemd/system/eeg-menu.service",
            "etc/systemd/system/getty.target.wants/serial-getty@ttyPS0.service": "lib/systemd/system/serial-getty@.service",
        }
        for link, target in enablement.items():
            if resolve_archive_path(link, names, symlinks) != target:
                fail("SYSTEMD_UNIT_ENABLEMENT_LINK_MISMATCH:" + link)
        unit_contracts = {
            "eeg-prepare": ("ExecStart=/usr/lib/eeg-system/prepare",),
            "eeg-network": ("ExecStart=/usr/sbin/eeg-network apply", "ExecStartPre=/sbin/modprobe motorcomm_cont"),
            "eeg-dsp": ("ExecStart=/usr/lib/eeg-system/dsp-prepare",),
            "eeg-sshd": ("ExecStart=/usr/lib/eeg-system/ssh-service start", "ExecStop=/usr/lib/eeg-system/ssh-service stop"),
            "eeg-menu": ("ExecStart=/usr/bin/eeg-ui start", "ExecStop=/usr/bin/eeg-ui stop"),
        }
        for unit, fragments in unit_contracts.items():
            text = init_contract.get("etc/systemd/system/" + unit + ".service", "")
            if any(fragment not in text for fragment in fragments):
                fail("SYSTEMD_UNIT_CONTRACT_REQUIRED:" + unit)
        dropin = init_contract.get("etc/systemd/system/serial-getty@ttyPS0.service.d/eeg.conf", "")
        if "ExecStart=" not in dropin or "/usr/sbin/eeg-serial-login" not in dropin or "115200 %I" not in dropin:
            fail("SYSTEMD_SERIAL_GETTY_LOGIN_DROPIN_INVALID")
        competing_sysv = ("etc/rcS.d/S00psplash.sh", "etc/rcS.d/S29read-only-rootfs-hook.sh",
                          "etc/rc2.d/S01networking", "etc/rc3.d/S01networking", "etc/rc4.d/S01networking",
                          "etc/rc5.d/S01networking", "etc/rc2.d/S10dropbear", "etc/rc3.d/S10dropbear",
                          "etc/rc4.d/S10dropbear", "etc/rc5.d/S10dropbear", "etc/rc2.d/S09weston",
                          "etc/rc3.d/S09weston", "etc/rc5.d/S09weston", "etc/rc2.d/S90qtGUI.sh",
                          "etc/rc3.d/S90qtGUI.sh", "etc/rc4.d/S90qtGUI.sh", "etc/rc5.d/S90qtGUI.sh",
                          "etc/rc3.d/S99tcf-agent", "etc/rc5.d/S99tcf-agent", "etc/rc2.d/S20inetd.busybox",
                          "etc/rc3.d/S20inetd.busybox", "etc/rc4.d/S20inetd.busybox", "etc/rc5.d/S20inetd.busybox")
        if any(name in names for name in competing_sysv):
            fail("SYSV_COMPETING_BOOT_OWNER_REMAINS")
    ssh = {}
    for line in ssh_config.splitlines():
        fields = line.strip().split()
        if len(fields) >= 2 and not fields[0].startswith("#"):
            if fields[0].lower() in ("include", "match"):
                fail("OPENSSH_CONFIG_INCLUDE_OR_MATCH_BLOCK_REQUIRES_BOARD_RUNTIME_AUDIT")
            ssh[fields[0].lower()] = " ".join(fields[1:]).lower()
    if ssh.get("passwordauthentication") != "yes" or ssh.get("permitrootlogin") != "yes" or ssh.get("subsystem", "").split()[-1:] != ["internal-sftp"]:
        fail("OPENSSH_ROOT_PASSWORD_AND_SFTP_CONTRACT_INVALID")
    return {"member_count": len(names), "unpacked_regular_bytes": regular_bytes,
            "required_paths_verified": len(required), "identity_verified": True,
            "init_system": init_system,
            "openssh_contract": "root password and internal-sftp explicitly configured"}


def required_systemd_mask(name, link, init_system):
    """Only the declared competing services may satisfy a required path as a mask."""
    if init_system == "systemd" and name in SYSTEMD_MASK_PATHS:
        if link != "/dev/null":
            fail("SYSTEMD_CONFLICTING_OWNER_NOT_MASKED:" + name)
        return True
    if link == "/dev/null":
        fail("ROOTFS_REQUIRED_NULL_LINK_NOT_ALLOWED:" + name)
    return False


def installed_path(root, name, follow_final=True):
    """Resolve symlinks within the installed root, including absolute target links."""
    pending = list(safe_relative(name).parts)
    resolved, links = [], 0
    while pending:
        part = pending.pop(0)
        if part in ("", "."):
            continue
        if part == "..":
            if not resolved:
                fail("INSTALLED_PATH_ESCAPES_ROOT:" + name)
            resolved.pop()
            continue
        target = root.joinpath(*resolved, part)
        if target.is_symlink() and (follow_final or pending):
            links += 1
            if links > 40:
                fail("INSTALLED_SYMLINK_LOOP:" + name)
            link = PurePosixPath(os.readlink(target))
            if link.is_absolute():
                resolved.clear()
            pending = [piece for piece in link.parts if piece != "/"] + pending
        else:
            resolved.append(part)
    return root.joinpath(*resolved)


def verify_required_installed_paths(root, required_paths, init_system):
    for value in required_paths:
        name = safe_root_path(value)
        target = installed_path(root, name, follow_final=False)
        link = os.readlink(target) if target.is_symlink() else None
        if required_systemd_mask(name, link, init_system):
            continue
        if not installed_path(root, name).exists():
            fail("INSTALLED_ROOTFS_REQUIRED_PATH_MISSING:" + name)


def verified_override(root, name):
    if name not in OVERRIDE_PATHS:
        fail("INSTALLED_ROOTFS_OVERRIDE_PATH_NOT_ALLOWED:" + name)
    target = root
    for part in PurePosixPath(name).parts:
        target = target / part
        if target.is_symlink():
            fail("INSTALLED_ROOTFS_OVERRIDE_SYMLINK:" + name)
    try:
        metadata = target.lstat()
    except FileNotFoundError:
        fail("INSTALLED_ROOTFS_OVERRIDE_MISSING:" + name)
    if not stat.S_ISREG(metadata.st_mode):
        fail("INSTALLED_ROOTFS_OVERRIDE_NOT_REGULAR:" + name)
    private = name == "etc/shadow" or name == "root/.ssh/authorized_keys" or (
        name.startswith("etc/ssh/") and not name.endswith(".pub"))
    modes = {0o600} if private else {0o600, 0o644}
    if metadata.st_uid != 0 or metadata.st_gid != 0 or stat.S_IMODE(metadata.st_mode) not in modes:
        fail("INSTALLED_ROOTFS_OVERRIDE_UNSAFE_METADATA:" + name)
    return target


def verify_installed_rootfs(archive_path, root, overridden_paths=()):
    overrides = {safe_root_path(name) for name in overridden_paths}
    for name in sorted(overrides):
        verified_override(root, name)
    verified, regular_bytes = 0, 0
    with tarfile.open(archive_path, mode="r|*") as archive:
        for item in archive:
            name = item.name
            while name.startswith("./"):
                name = name[2:]
            if name in ("", ".") and item.isdir():
                continue
            if name in overrides:
                if not item.isfile():
                    fail("INSTALLED_ROOTFS_OVERRIDE_ARCHIVE_MEMBER_NOT_REGULAR:" + name)
                verified += 1
                continue
            target = installed_path(root, name, follow_final=not item.issym())
            if item.isfile():
                if not target.is_file() or target.stat().st_size != item.size:
                    fail("INSTALLED_ROOTFS_FILE_LENGTH_MISMATCH:" + name)
                metadata = target.stat()
                if stat.S_IMODE(metadata.st_mode) != item.mode or metadata.st_uid != item.uid or metadata.st_gid != item.gid:
                    fail("INSTALLED_ROOTFS_FILE_METADATA_MISMATCH:" + name)
                expected = hashlib.sha256()
                stream = archive.extractfile(item)
                for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
                    expected.update(block)
                if sha(target) != expected.hexdigest():
                    fail("INSTALLED_ROOTFS_FILE_READBACK_MISMATCH:" + name)
                regular_bytes += item.size
            elif item.issym():
                if not target.is_symlink() or os.readlink(target) != item.linkname:
                    fail("INSTALLED_ROOTFS_SYMLINK_MISMATCH:" + name)
                metadata = target.lstat()
                if metadata.st_uid != item.uid or metadata.st_gid != item.gid:
                    fail("INSTALLED_ROOTFS_SYMLINK_OWNER_MISMATCH:" + name)
            elif item.isdir() and not target.is_dir():
                fail("INSTALLED_ROOTFS_DIRECTORY_MISSING:" + name)
            elif item.isdir():
                metadata = target.stat()
                if stat.S_IMODE(metadata.st_mode) != item.mode or metadata.st_uid != item.uid or metadata.st_gid != item.gid:
                    fail("INSTALLED_ROOTFS_DIRECTORY_METADATA_MISMATCH:" + name)
            elif item.islnk():
                link = item.linkname
                while link.startswith("./"):
                    link = link[2:]
                original = installed_path(root, link)
                if target.stat().st_ino != original.stat().st_ino or target.stat().st_dev != original.stat().st_dev:
                    fail("INSTALLED_ROOTFS_HARDLINK_MISMATCH:" + name)
            elif item.ischr() or item.isblk():
                metadata = target.stat()
                expected_mode = stat.S_IFCHR if item.ischr() else stat.S_IFBLK
                expected_device = os.makedev(item.devmajor, item.devminor)
                if (metadata.st_mode & stat.S_IFMT(metadata.st_mode)) != expected_mode or metadata.st_rdev != expected_device:
                    fail("INSTALLED_ROOTFS_DEVICE_NODE_MISMATCH:" + name)
            elif item.isfifo() and not stat.S_ISFIFO(target.stat().st_mode):
                fail("INSTALLED_ROOTFS_FIFO_MISMATCH:" + name)
            verified += 1
    return {"members_verified": verified, "regular_bytes_sha256_readback": regular_bytes,
            "override_paths_metadata_verified": sorted(overrides)}


def mount_unescape(text):
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), text)


def read_mounts(path=Path("/proc/self/mountinfo")):
    mounts = []
    for line in path.read_text().splitlines():
        first, last = line.split(" - ", 1)
        fields, extra = first.split(), last.split()
        mounts.append({"major_minor": fields[2], "root": mount_unescape(fields[3]),
                       "mountpoint": mount_unescape(fields[4]), "options": fields[5],
                       "fstype": extra[0], "source": mount_unescape(extra[1])})
    return mounts


def mmc_disks(sys_root=Path("/sys")):
    disks = []
    for block in sorted((sys_root / "class/block").glob("mmcblk*")):
        if not re.fullmatch(r"mmcblk[0-9]+", block.name):
            continue
        try:
            kind = (block / "device/type").read_text().strip()
            cid = (block / "device/cid").read_text().strip().lower()
            size = int((block / "size").read_text().strip()) * 512
            removable = (block / "removable").read_text().strip()
            devno = (block / "dev").read_text().strip()
            ro = (block / "ro").read_text().strip()
        except (FileNotFoundError, ValueError) as exc:
            fail("MMC_IDENTITY_UNREADABLE:" + block.name + ":" + str(exc))
        if not re.fullmatch(r"[0-9a-f]{32}", cid):
            fail("MMC_CID_FORMAT_INVALID:" + block.name)
        partitions = []
        for part in sorted((sys_root / "class/block").glob(block.name + "p*")):
            if re.fullmatch(block.name + r"p[0-9]+", part.name):
                partitions.append({"name": part.name, "major_minor": (part / "dev").read_text().strip(),
                                   "bytes": int((part / "size").read_text().strip()) * 512})
        disks.append({"name": block.name, "device": "/dev/" + block.name, "type": kind,
                      "cid": cid, "bytes": size, "removable": removable == "1", "read_only": ro == "1",
                      "major_minor": devno, "partitions": partitions})
    return disks


def disk_for_devno(devno, disks):
    matches = [disk for disk in disks if disk["major_minor"] == devno or
               any(part["major_minor"] == devno for part in disk["partitions"])]
    if len(matches) != 1:
        fail("MMC_BACKING_DEVICE_UNKNOWN:" + devno)
    return matches[0]


def find_mount(path, mounts):
    path = str(path.resolve(strict=True))
    matches = [m for m in mounts if path == m["mountpoint"] or path.startswith(m["mountpoint"].rstrip("/") + "/")]
    if not matches:
        fail("PAYLOAD_MOUNT_UNKNOWN")
    return max(matches, key=lambda m: len(m["mountpoint"]))


def idle_proof(command="/usr/sbin/eeg-maintenance-check", maintenance_lock_held=False):
    if not Path(command).is_file():
        fail("LIVE_MAINTENANCE_STATE_CHECK_UNAVAILABLE")
    argv = [command, "--json"]
    if maintenance_lock_held:
        argv.append("--maintenance-lock-held")
    result = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            timeout=15, check=False)
    if result.returncode or len(result.stdout) > 256 * 1024:
        fail("LIVE_MAINTENANCE_STATE_CHECK_FAILED")
    state = json.loads(result.stdout.decode("utf-8"))
    if state.get("schema") != "eeg.zu3eg.maintenance_state.v1":
        fail("LIVE_MAINTENANCE_STATE_SCHEMA_MISMATCH")
    for name, value in IDLE_KEYS.items():
        if state.get(name) != value:
            fail("HARDWARE_MUST_BE_VERIFIED_IDLE:" + name + "=" + str(state.get(name, "UNKNOWN")))
    return {name: state[name] for name in IDLE_KEYS}


def required_bytes(manifest):
    boot = manifest["layout"]["boot_mib"] * 1024**2
    unpacked = manifest["rootfs"]["unpacked_regular_bytes"]
    # Ext4 metadata, small-file block rounding and reserved blocks need room too.
    root = max(unpacked * 120 // 100, unpacked + 256 * 1024**2) + manifest["layout"]["root_min_free_bytes"]
    return {"boot_bytes": boot, "root_minimum_bytes": root, "disk_minimum_bytes": root + boot + 16 * 1024**2}


def build_plan(manifest_path, cid=None, maintenance_lock_held=False):
    manifest, artifacts, hashes = read_payload(manifest_path)
    rootfs = audit_rootfs(artifacts["rootfs_tar"], manifest["rootfs"], manifest["kernel_release"], manifest["system_identity"])
    boot_script = audit_uboot_script(artifacts["boot_script"])
    fit = audit_fit(artifacts["fit"], manifest)
    disks, mounts = mmc_disks(), read_mounts()
    root_rows = [m for m in mounts if m["mountpoint"] == "/"]
    if len(root_rows) != 1 or root_rows[0]["fstype"] != "ext4":
        fail("RUNNING_PERSISTENT_EXT4_SD_ROOT_REQUIRED")
    root_disk = disk_for_devno(root_rows[0]["major_minor"], disks)
    if root_disk["type"] != "SD":
        fail("INSTALLER_MUST_RUN_FROM_SD_ROOT")
    payload_disk = disk_for_devno(find_mount(manifest_path, mounts)["major_minor"], disks)
    if payload_disk["cid"] != root_disk["cid"]:
        fail("PAYLOAD_AND_RUNNING_SD_SOURCE_DIFFER")
    expected_identity = manifest["system_identity"]
    identity_equal(json_read(Path("/etc/eeg-system/baseline.json")), expected_identity)
    if os.uname().release != manifest["kernel_release"]:
        fail("RUNNING_KERNEL_PAYLOAD_MISMATCH")
    candidates = [d for d in disks if d["type"] == "MMC" and not d["removable"] and d["cid"] != root_disk["cid"]]
    if cid:
        candidates = [d for d in candidates if d["cid"] == cid.lower()]
    blockers, target = [], None
    if len(candidates) != 1:
        blockers.append("EXACTLY_ONE_IDENTIFIED_EMMC_REQUIRED")
    else:
        target = candidates[0]
        if target["read_only"]:
            blockers.append("EMMC_IS_READ_ONLY")
        if target["bytes"] < required_bytes(manifest)["disk_minimum_bytes"]:
            blockers.append("EMMC_CAPACITY_INSUFFICIENT_FOR_COMPLETE_DEVELOPMENT_ROOTFS")
        devnos = {target["major_minor"]} | {p["major_minor"] for p in target["partitions"]}
        if any(m["major_minor"] in devnos for m in mounts):
            blockers.append("TARGET_EMMC_HAS_MOUNTED_FILESYSTEMS_UNMOUNT_EXPLICITLY_AFTER_BACKUP")
        swap = Path("/proc/swaps").read_text().splitlines()[1:]
        if any(re.match(r"^" + re.escape(target["device"]) + r"(?:p\d+)?\s", s) for s in swap):
            blockers.append("TARGET_EMMC_USED_AS_SWAP")
        if any(Path("/sys/class/block", n, "holders").exists() and list(Path("/sys/class/block", n, "holders").iterdir())
               for n in [target["name"]] + [p["name"] for p in target["partitions"]]):
            blockers.append("TARGET_EMMC_HAS_DEVICE_MAPPER_OR_OTHER_HOLDERS")
    tools_missing = [name for name in REQUIRED_TOOLS if shutil.which(name) is None]
    if tools_missing:
        blockers.append("INSTALLATION_TOOLS_MISSING:" + ",".join(tools_missing))
    live_state = None
    try:
        live_state = idle_proof(maintenance_lock_held=maintenance_lock_held)
    except (RuntimeError, ValueError, OSError, subprocess.TimeoutExpired) as exc:
        blockers.append(str(exc))
    plan = {"schema": "eeg.zu3eg.emmc_plan.v1", "created_at": now(), "release": manifest["release"],
            "manifest": {"path": str(manifest_path.resolve()), "sha256": sha(manifest_path)},
            "source_sd": root_disk, "current_root": root_rows[0], "emmc_candidates": candidates,
            "target": target, "capacity": required_bytes(manifest), "artifacts": hashes, "rootfs": rootfs,
            "boot_script": boot_script, "fit": fit,
            "live_maintenance_state": live_state, "blockers": blockers,
            "software_plan_clear": not blockers, "can_apply": False,
            "apply_requirements": ["operator separately checks target CID and backup record",
                                   "--target-cid and --confirm-erase must match the exact MMC CID",
                                   "--adc-isolated is a per-installation physical operator confirmation"],
            "overwrites": ["target eMMC user-area partition table", "target FAT BOOT", "target ext4 rootfs"],
            "excluded_from_writes": ["SD source", "eMMC boot0", "eMMC boot1", "eMMC RPMB", "EXT_CSD", "QSPI"],
            "next": "Back up unique eMMC data, retain bootable SD rescue, then apply with exact CID and backup record."}
    return plan, manifest, artifacts


def invoke(argv, log, input_data=None):
    row = {"started_at": now(), "argv": list(map(str, argv))}
    log.append(row)
    result = subprocess.run(row["argv"], input=input_data, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            check=False)
    row["returncode"] = result.returncode
    row["output_tail"] = result.stdout[-16384:].decode("utf-8", "backslashreplace")
    if result.returncode:
        fail("INSTALL_COMMAND_FAILED:" + str(argv[0]))
    return result.stdout.decode("utf-8", "strict").strip()


def wait_partitions(device):
    partitions = [Path(device + "p1"), Path(device + "p2")]
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if all(path.exists() and stat.S_ISBLK(path.stat().st_mode) for path in partitions):
            return partitions
        time.sleep(0.1)
    fail("PARTITION_DEVICE_CREATION_TIMEOUT")


def copy_private_config(root, receipt):
    copied = []
    for name in PRIVATE_CONFIG:
        source = Path("/") / name
        if not source.exists():
            continue
        if source.is_symlink() or not source.is_file():
            fail("PERSISTENT_IDENTITY_CONFIG_MUST_BE_REGULAR:" + name)
        target = installed_path(root, name, follow_final=False)
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.parent.is_relative_to(root.resolve()):
            fail("IDENTITY_CONFIG_PARENT_OUTSIDE_ROOTFS")
        if target.is_symlink():
            target.unlink()
        shutil.copy2(source, target)
        os.chown(target, 0, 0)
        if not name.endswith(".pub") and ("/ssh/" in name or name.endswith("shadow") or "/.ssh/" in name):
            target.chmod(0o600)
        copied.append(name)
    # Only path names are logged. Password hashes and private-key hashes are omitted.
    receipt["persistent_config_preserved"] = copied


def write_json_atomic(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("." + path.name + ".new")
    if temporary.exists():
        fail("UNFINISHED_RECEIPT_EXISTS:" + str(temporary))
    data = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    with temporary.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def apply(args):
    import fcntl
    if os.geteuid() != 0:
        fail("ROOT_REQUIRED_FOR_EMMC_INSTALL")
    if not args.target_cid or not re.fullmatch(r"[0-9a-fA-F]{32}", args.target_cid):
        fail("EXPLICIT_EXACT_TARGET_CID_REQUIRED")
    if args.confirm_erase != args.target_cid.lower():
        fail("CONFIRM_ERASE_MUST_EQUAL_EXACT_TARGET_CID")
    if not args.adc_isolated:
        fail("OPERATOR_PHYSICAL_ADC_ISOLATION_CONFIRMATION_REQUIRED")
    record = json_read(args.backup_record)
    if record.get("schema") != "eeg.zu3eg.emmc_backup_decision.v1" or record.get("target_cid") != args.target_cid.lower():
        fail("TARGET_BOUND_BACKUP_DECISION_REQUIRED")
    if record.get("decision") not in ("backup_completed", "no_unique_data_confirmed") or record.get("sd_rescue_retained") is not True:
        fail("BACKUP_OR_NO_UNIQUE_DATA_AND_SD_RESCUE_CONFIRMATION_REQUIRED")
    if record["decision"] == "backup_completed" and not record.get("backup_location"):
        fail("BACKUP_LOCATION_REQUIRED")
    if record.get("covers") != ["emmc_user_area", "emmc_boot0", "emmc_boot1"]:
        fail("EXISTING_EMMC_RECOVERY_SCOPE_MUST_BE_EXPLICIT")
    lock = Path("/run/eeg-operator/maintenance.lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        marker = Path("/run/eeg-operator/maintenance.active")
        marker.parent.mkdir(parents=True, exist_ok=True)
        with marker.open("x") as active:
            active.write("emmc-install\n")
        receipt = {"schema": "eeg.zu3eg.emmc_install_receipt.v1", "started_at": now(),
                   "result": "INCOMPLETE", "commands": [], "board_boot_verification": "NOT_EXERCISED"}
        run = None
        root_mount = boot_mount = None
        mounted = []
        report_path = Path("/var/log/eeg/emmc-install-" + datetime.datetime.now().strftime("%Y%m%dT%H%M%S") + ".json")
        try:
            run = Path(tempfile.mkdtemp(prefix="eeg-emmc-", dir="/run"))
            root_mount, boot_mount = run / "root", run / "boot"
            root_mount.mkdir()
            boot_mount.mkdir()
            plan, manifest, artifacts = build_plan(args.manifest, args.target_cid, maintenance_lock_held=True)
            receipt["plan"] = plan
            receipt["backup_decision"] = {key: record.get(key) for key in
                                           ("decision", "target_cid", "sd_rescue_retained", "covers", "backup_location")}
            receipt["operator_physical_confirmation"] = {"adc_isolated": True,
                                                           "scope": "this eMMC installation only; not inferred from software"}
            if plan["blockers"]:
                fail("INSTALLATION_BLOCKED:" + ",".join(plan["blockers"]))
            target = plan["target"]
            if target["cid"] != args.target_cid.lower() or target["cid"] == plan["source_sd"]["cid"]:
                fail("SOURCE_TARGET_IDENTITY_MISMATCH")
            # Re-read immediately before the first destructive command; no inferred mmc index.
            current = next((d for d in mmc_disks() if d["device"] == target["device"]), None)
            if current != target:
                fail("TARGET_IDENTITY_CHANGED_BEFORE_WRITE")
            idle_proof(maintenance_lock_held=True)
            print(json.dumps({"ERASING_CONFIRMED_TARGET": target, "release": manifest["release"]}, ensure_ascii=False), flush=True)
            device, commands = target["device"], receipt["commands"]
            boot_sectors = manifest["layout"]["boot_mib"] * 2048
            disk_id = os.urandom(4).hex()
            receipt["new_mbr_disk_id"] = disk_id
            table = ("label: dos\nlabel-id: 0x" + disk_id + "\nunit: sectors\n\nstart=2048, size=" + str(boot_sectors) +
                     ", type=c, bootable\nstart=" + str(2048 + boot_sectors) + ", type=83\n").encode("ascii")
            invoke(["sfdisk", "--wipe", "always", "--wipe-partitions", "always", device], commands, table)
            invoke(["blockdev", "--rereadpt", device], commands)
            invoke(["udevadm", "settle", "--timeout=15"], commands)
            boot_part, root_part = wait_partitions(device)
            invoke(["mkfs.vfat", "-F", "32", "-n", "EEGBOOT", str(boot_part)], commands)
            invoke(["mkfs.ext4", "-F", "-m", "1", "-L", "EEGROOT", str(root_part)], commands)
            invoke(["mount", "-t", "ext4", str(root_part), str(root_mount)], commands)
            mounted.append(root_mount)
            invoke(["mount", "-t", "vfat", str(boot_part), str(boot_mount)], commands)
            mounted.append(boot_mount)
            for role in ("boot_bin", "fit", "boot_script"):
                target_file = boot_mount / ROLES[role]
                shutil.copyfile(artifacts[role], target_file)
                with target_file.open("rb") as stream:
                    os.fsync(stream.fileno())
                if sha(target_file) != sha(artifacts[role]):
                    fail("EMMC_BOOT_READBACK_HASH_MISMATCH:" + role)
            shutil.copyfile(args.manifest, boot_mount / "system_payload.manifest.json")
            invoke(["tar", "--numeric-owner", "--xattrs", "--acls", "-xpf", str(artifacts["rootfs_tar"]), "-C", str(root_mount)], commands)
            receipt["rootfs_readback"] = verify_installed_rootfs(artifacts["rootfs_tar"], root_mount)
            identity_equal(json_read(root_mount / "etc/eeg-system/baseline.json"), manifest["system_identity"])
            verify_required_installed_paths(root_mount, manifest["rootfs"]["required_paths"],
                                            manifest["rootfs"]["init_system"])
            root_uuid = invoke(["blkid", "-s", "UUID", "-o", "value", str(root_part)], commands)
            boot_uuid = invoke(["blkid", "-s", "UUID", "-o", "value", str(boot_part)], commands)
            root_partuuid = invoke(["blkid", "-s", "PARTUUID", "-o", "value", str(root_part)], commands)
            if not root_uuid or not boot_uuid or not root_partuuid:
                fail("INSTALLED_FILESYSTEM_IDENTITIES_UNREADABLE")
            if root_partuuid.lower() != disk_id + "-02":
                fail("EMMC_PARTUUID_DOES_NOT_MATCH_NEW_MBR_ID")
            fstab = root_mount / "etc/fstab"
            preserved = []
            for line in fstab.read_text().splitlines() if fstab.exists() else []:
                parts = line.split()
                if parts and not parts[0].startswith("#") and len(parts) >= 2 and parts[1] in ("/", "/boot"):
                    continue
                preserved.append(line)
            fstab.write_text("\n".join(preserved + ["UUID=" + root_uuid + " / ext4 defaults,noatime 0 1", "UUID=" + boot_uuid + " /boot vfat defaults,nofail,umask=0077 0 2"]) + "\n")
            copy_private_config(root_mount, receipt)
            root_stat = os.statvfs(root_mount)
            free = root_stat.f_bavail * root_stat.f_frsize
            if free < manifest["layout"]["root_min_free_bytes"]:
                fail("INSTALLED_COMPLETE_ROOTFS_LEAVES_INSUFFICIENT_DEVELOPMENT_SPACE")
            receipt["installed"] = {"root_uuid": root_uuid, "boot_uuid": boot_uuid,
                                     "root_partuuid": root_partuuid, "root_free_bytes": free,
                                     "fstab_sha256": sha(fstab),
                                     "boot_hashes_verified": True, "required_paths_verified": True,
                                     "boot_partitions_modified": False, "boot_source_after_install": "UNVERIFIED_UNTIL_EMMC_BOOT"}
            invoke(["sync"], commands)
            for path in reversed(mounted):
                invoke(["umount", str(path)], commands)
            mounted.clear()
            invoke(["fsck.fat", "-n", str(boot_part)], commands)
            invoke(["e2fsck", "-f", "-n", str(root_part)], commands)
            invoke(["mount", "-t", "ext4", "-o", "ro,noload", str(root_part), str(root_mount)], commands)
            mounted.append(root_mount)
            receipt["rootfs_readback_after_flush"] = verify_installed_rootfs(
                artifacts["rootfs_tar"], root_mount, {"etc/fstab", *receipt["persistent_config_preserved"]})
            if sha(installed_path(root_mount, "etc/fstab")) != receipt["installed"]["fstab_sha256"]:
                fail("EMMC_FSTAB_POST_FLUSH_READBACK_MISMATCH")
            identity_equal(json_read(root_mount / "etc/eeg-system/baseline.json"), manifest["system_identity"])
            for name in receipt["persistent_config_preserved"]:
                if sha(Path("/") / name) != sha(installed_path(root_mount, name)):
                    fail("PERSISTENT_CONFIG_POST_FLUSH_READBACK_MISMATCH:" + name)
            invoke(["umount", str(root_mount)], commands)
            mounted.clear()
            # Re-mount read-only and hash actual boot contents after the filesystem flush.
            invoke(["mount", "-t", "vfat", "-o", "ro", str(boot_part), str(boot_mount)], commands)
            mounted.append(boot_mount)
            for role in ("boot_bin", "fit", "boot_script"):
                if sha(boot_mount / ROLES[role]) != sha(artifacts[role]):
                    fail("POST_FLUSH_BOOT_READBACK_MISMATCH:" + role)
            invoke(["umount", str(boot_mount)], commands)
            mounted.clear()
            receipt["result"] = "INSTALLED_AND_READBACK_VERIFIED_EMMC_BOOT_PENDING"
        except Exception as exc:
            receipt["error"] = str(exc)
            raise
        finally:
            for path in reversed(mounted):
                try:
                    invoke(["umount", str(path)], receipt["commands"])
                except Exception as exc:
                    receipt.setdefault("cleanup_errors", []).append(str(exc))
            receipt["finished_at"] = now()
            write_json_atomic(report_path, receipt)
            if not receipt.get("cleanup_errors"):
                marker.unlink()
                with contextlib.suppress(OSError):
                    if boot_mount is not None:
                        boot_mount.rmdir()
                    if root_mount is not None:
                        root_mount.rmdir()
                    if run is not None:
                        run.rmdir()
            print(json.dumps({"result": receipt["result"], "receipt": str(report_path),
                              "emmc_boot": "NOT_EXERCISED", "reboot_performed": False}, ensure_ascii=False), flush=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("action", choices=("plan", "apply"), nargs="?", default="plan")
    ap.add_argument("--manifest", type=Path, default=Path("/var/lib/eeg-system/install-payload/current/system_payload.manifest.json"))
    ap.add_argument("--target-cid")
    ap.add_argument("--confirm-erase")
    ap.add_argument("--backup-record", type=Path)
    ap.add_argument("--adc-isolated", action="store_true", help="operator confirms ADC physically isolated for this installation")
    args = ap.parse_args()
    if args.action == "plan":
        plan, _, _ = build_plan(args.manifest, args.target_cid)
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 0 if plan["software_plan_clear"] else 3
    if args.backup_record is None:
        fail("BACKUP_DECISION_RECORD_REQUIRED")
    apply(args)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(json.dumps({"schema": "eeg.zu3eg.emmc_install_error.v1", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        sys.exit(3)
