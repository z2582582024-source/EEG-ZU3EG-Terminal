#!/usr/bin/env python3
"""Independent JTAG/RAM eMMC installer; the frozen SD entry is not changed.

fetch and backup never write target storage. plan is read-only. apply is bound to
one RAM boot, exact CID, payload, plan digest and a fresh operator decision. Every
storage mutation is preceded by a host-fsynced event; an incomplete attempt cannot
be replayed automatically. This program never changes boot0/1, RPMB or EXT_CSD.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
import uuid

_spec = importlib.util.spec_from_file_location("eeg_frozen_sd_validator", Path(__file__).with_name("eeg_emmc_install.py"))
legacy = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(legacy)
REVISION = "ram-installer-20261023-01"
ENVIRONMENT = Path("/etc/eeg-jtag-installer.json")
CACHE = Path("/run/eeg-install/payload")
TRANSPORT_CHUNK = 8 * 1024**2
BACKUP_CHUNK = 4 * 1024**2
RAM_RESERVE = 512 * 1024**2
RAM_FS = {"rootfs", "tmpfs", "ramfs"}
NON_TARGET_FS = RAM_FS | {"nfs", "nfs4", "cifs", "9p"}
TERMINAL_PHASE = "READBACK_VERIFIED_BOOT_PENDING"


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def valid_sha(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def require_cid(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{32}", value):
        legacy.fail("EXACT_LOWERCASE_TARGET_CID_REQUIRED")
    return value


def url_base(value):
    parts = urllib.parse.urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.netloc or parts.username or parts.password or parts.query or parts.fragment:
        legacy.fail("EXPLICIT_CREDENTIAL_FREE_HOST_URL_REQUIRED")
    return value.rstrip("/")


def request_json(base, suffix, value=None, method=None, opener=urllib.request.urlopen):
    data = None if value is None else canonical(value)
    req = urllib.request.Request(url_base(base) + suffix, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with opener(req, timeout=120) as response:
        raw = response.read(4 * 1024**2 + 1)
        if len(raw) > 4 * 1024**2:
            legacy.fail("BOUNDED_HOST_JSON_REQUIRED")
        return json.loads(raw.decode("utf-8"))


def create_transport_manifest(manifest_path, chunk_bytes=TRANSPORT_CHUNK):
    """Host helper: one source of lengths, segment SHA and final SHA; no copies."""
    manifest, artifacts, _ = legacy.read_payload(Path(manifest_path))
    if type(chunk_bytes) is not int or not 65536 <= chunk_bytes <= TRANSPORT_CHUNK:
        legacy.fail("BOUNDED_TRANSPORT_CHUNK_REQUIRED")
    rows = []
    inventory = {item["role"]: item for item in manifest["artifacts"]}
    for role, path in artifacts.items():
        chunks, offset = [], 0
        whole = hashlib.sha256()
        with path.open("rb") as stream:
            for data in iter(lambda: stream.read(chunk_bytes), b""):
                chunks.append({"offset": offset, "bytes": len(data), "sha256": digest(data)})
                whole.update(data)
                offset += len(data)
        row = inventory[role]
        if whole.hexdigest() != row["sha256"] or offset != row["bytes"]:
            legacy.fail("PAYLOAD_CHANGED_DURING_TRANSPORT_MANIFEST:" + role)
        rows.append(dict(row, chunks=chunks))
    return {"schema": "eeg.zu3eg.http_transport.v1", "payload_manifest_sha256": legacy.sha(Path(manifest_path)),
            "payload_manifest_bytes": Path(manifest_path).stat().st_size,
            "chunk_bytes": chunk_bytes, "artifacts": rows}


def validate_transport(transport, manifest_sha):
    if transport.get("schema") != "eeg.zu3eg.http_transport.v1" or transport.get("payload_manifest_sha256") != manifest_sha:
        legacy.fail("PINNED_HTTP_TRANSPORT_REQUIRED")
    if not valid_sha(manifest_sha) or type(transport.get("payload_manifest_bytes")) is not int or not 0 < transport["payload_manifest_bytes"] <= 4 * 1024**2:
        legacy.fail("BOUNDED_PINNED_PAYLOAD_MANIFEST_REQUIRED")
    chunk_bytes = transport.get("chunk_bytes")
    if type(chunk_bytes) is not int or not 65536 <= chunk_bytes <= TRANSPORT_CHUNK:
        legacy.fail("BOUNDED_TRANSPORT_CHUNK_REQUIRED")
    roles, total = set(), 0
    if not isinstance(transport.get("artifacts"), list) or len(transport["artifacts"]) != 4:
        legacy.fail("EXACT_FOUR_TRANSPORT_ARTIFACTS_REQUIRED")
    for row in transport["artifacts"]:
        role = row.get("role")
        if role not in legacy.ROLES or role in roles or row.get("path") != legacy.ROLES[role]:
            legacy.fail("UNIQUE_CANONICAL_TRANSPORT_ROLE_REQUIRED")
        roles.add(role)
        if type(row.get("bytes")) is not int or row["bytes"] <= 0 or not valid_sha(row.get("sha256")):
            legacy.fail("TRANSPORT_ARTIFACT_IDENTITY_REQUIRED")
        offset = 0
        if not isinstance(row.get("chunks"), list) or not row["chunks"]:
            legacy.fail("TRANSPORT_CHUNKS_REQUIRED")
        for index, chunk in enumerate(row["chunks"]):
            length = chunk.get("bytes")
            if chunk.get("offset") != offset or type(length) is not int or not 0 < length <= chunk_bytes or not valid_sha(chunk.get("sha256")):
                legacy.fail("TRANSPORT_CHUNK_CONTIGUITY_OR_IDENTITY_INVALID")
            if index < len(row["chunks"]) - 1 and length != chunk_bytes:
                legacy.fail("ONLY_LAST_TRANSPORT_CHUNK_MAY_BE_SHORT")
            offset += length
        if offset != row["bytes"]:
            legacy.fail("TRANSPORT_LAST_CHUNK_LENGTH_MISMATCH")
        total += offset
    return total


def no_symlink_components(path):
    absolute = path.absolute()
    for parent in [absolute, *absolute.parents]:
        if parent.is_symlink():
            legacy.fail("SOURCE_OR_CACHE_PATH_SYMLINK:" + str(parent))


def available_memory():
    values = dict(re.findall(r"^(\w+):\s+(\d+) kB$", Path("/proc/meminfo").read_text(), re.M))
    if "MemAvailable" not in values:
        legacy.fail("MEMAVAILABLE_REQUIRED_FOR_RAM_CACHE")
    return int(values["MemAvailable"]) * 1024


def fetch_payload(base, transport, manifest_sha, destination=CACHE, opener=urllib.request.urlopen):
    """Resume only from individually rehashed existing segments; never target writes."""
    total = validate_transport(transport, manifest_sha)
    no_symlink_components(destination)
    destination.mkdir(parents=True, exist_ok=True)
    mount = legacy.find_mount(destination, legacy.read_mounts())
    if mount["fstype"] not in RAM_FS:
        legacy.fail("FETCH_REQUIRES_EXPLICIT_RAM_CACHE")
    existing = sum((destination / row["path"]).stat().st_size if (destination / row["path"]).is_file()
                   else (destination / (row["path"] + ".partial")).stat().st_size if (destination / (row["path"] + ".partial")).is_file() else 0
                   for row in transport["artifacts"])
    remaining = max(0, total - existing) + transport["payload_manifest_bytes"]
    if available_memory() < remaining + RAM_RESERVE:
        legacy.fail("RAM_CACHE_AND_KERNEL_RESERVE_EXCEED_MEMAVAILABLE")
    fs = os.statvfs(destination)
    if fs.f_bavail * fs.f_frsize < remaining:
        legacy.fail("RAM_CACHE_MOUNT_CAPACITY_INSUFFICIENT")
    manifest_req = urllib.request.Request(url_base(base) + "/payload/system_payload.manifest.json")
    with opener(manifest_req, timeout=120) as response:
        raw = response.read(transport["payload_manifest_bytes"] + 1)
    if len(raw) != transport["payload_manifest_bytes"] or digest(raw) != manifest_sha:
        legacy.fail("FETCHED_PAYLOAD_MANIFEST_PIN_MISMATCH")
    manifest = json.loads(raw.decode("utf-8"))
    inventory = {row["role"]: row for row in manifest.get("artifacts", [])}
    for row in transport["artifacts"]:
        if {key: row[key] for key in ("role", "path", "bytes", "sha256")} != inventory.get(row["role"]):
            legacy.fail("TRANSPORT_AND_PAYLOAD_INVENTORY_DIFFER")
        final, partial = destination / row["path"], destination / (row["path"] + ".partial")
        no_symlink_components(final)
        no_symlink_components(partial)
        if partial.exists() and (not partial.is_file() or partial.stat().st_size > row["bytes"]):
            legacy.fail("PARTIAL_CACHE_EXCEEDS_DECLARED_ARTIFACT")
        if final.exists():
            if not final.is_file() or final.stat().st_size != row["bytes"] or legacy.sha(final) != row["sha256"]:
                legacy.fail("EXISTING_FINAL_CACHE_FILE_INVALID:" + row["role"])
            continue
        print(json.dumps({"FETCH_ARTIFACT": row["role"], "bytes": row["bytes"], "verified_resume": True}), file=sys.stderr, flush=True)
        with partial.open("r+b" if partial.exists() else "x+b") as stream:
            for chunk in row["chunks"]:
                offset, length = chunk["offset"], chunk["bytes"]
                stream.seek(offset)
                current = stream.read(length)
                if len(current) == length and digest(current) == chunk["sha256"]:
                    continue
                end = offset + length - 1
                req = urllib.request.Request(url_base(base) + "/payload/" + urllib.parse.quote(row["path"]),
                                             headers={"Range": "bytes=" + str(offset) + "-" + str(end), "Accept-Encoding": "identity"})
                with opener(req, timeout=120) as response:
                    expected_range = "bytes " + str(offset) + "-" + str(end) + "/" + str(row["bytes"])
                    if response.status != 206 or response.headers.get("Content-Range") != expected_range:
                        legacy.fail("EXACT_HTTP_RANGE_RESPONSE_REQUIRED")
                    data = response.read(length + 1)
                if len(data) != length or digest(data) != chunk["sha256"]:
                    legacy.fail("HTTP_CHUNK_LENGTH_OR_SHA_MISMATCH:" + row["role"] + ":" + str(offset))
                stream.seek(offset)
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            stream.truncate(row["bytes"])
        if legacy.sha(partial) != row["sha256"]:
            legacy.fail("HTTP_FINAL_ARTIFACT_SHA_MISMATCH:" + row["role"])
        os.replace(partial, final)
    manifest_path = destination / "system_payload.manifest.json"
    if manifest_path.exists() and legacy.sha(manifest_path) != manifest_sha:
        legacy.fail("EXISTING_CACHE_MANIFEST_DIFFERS")
    if not manifest_path.exists():
        with manifest_path.open("xb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
    legacy.read_payload(manifest_path)
    return {"manifest": str(manifest_path), "manifest_sha256": manifest_sha, "cache_bytes": total,
            "chunk_and_final_sha256_verified": True, "target_writes": False}


def environment_check(path, manifest, manifest_path, mounts):
    environment = legacy.json_read(path)
    if environment.get("schema") != "eeg.zu3eg.jtag_environment.v1" or environment.get("product") != "EEG-ZU3EG" or environment.get("revision") != REVISION:
        legacy.fail("MATCHED_JTAG_INSTALLER_ENVIRONMENT_REQUIRED")
    root_rows = [row for row in mounts if row["mountpoint"] == "/"]
    if len(root_rows) != 1 or root_rows[0]["fstype"] not in RAM_FS:
        legacy.fail("JTAG_INSTALLER_MUST_RUN_FROM_RAM_ROOT")
    if "eeg_jtag_install=1" not in Path("/proc/cmdline").read_text().split():
        legacy.fail("EXPLICIT_JTAG_INSTALL_BOOT_IDENTITY_REQUIRED")
    if os.uname().release != manifest["kernel_release"] or environment.get("kernel_release") != manifest["kernel_release"]:
        legacy.fail("RAM_KERNEL_PAYLOAD_MISMATCH")
    legacy.identity_equal(environment.get("system_identity", {}), manifest["system_identity"])
    for key, actual in (("payload_manifest_sha256", legacy.sha(manifest_path)),
                        ("installer_sha256", legacy.sha(Path(__file__))),
                        ("validator_sha256", legacy.sha(Path(legacy.__file__))),
                        ("platform_xsa_sha256", manifest.get("platform", {}).get("ps_xsa", {}).get("sha256"))):
        if not valid_sha(actual) or environment.get(key) != actual:
            legacy.fail("RAM_ENVIRONMENT_BINDING_MISMATCH:" + key)
    boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    if not re.fullmatch(r"[0-9a-f-]{36}", boot_id):
        legacy.fail("CURRENT_RAM_BOOT_ID_REQUIRED")
    return {"path": str(path), "sha256": legacy.sha(path), "boot_id": boot_id, "root": root_rows[0],
            "revision": REVISION, "identity_verified": True}


def target_blockers(target, mounts):
    blockers = []
    if target["read_only"]:
        blockers.append("EMMC_IS_READ_ONLY")
    devnos = {target["major_minor"]} | {part["major_minor"] for part in target["partitions"]}
    if any(row["major_minor"] in devnos for row in mounts):
        blockers.append("TARGET_EMMC_HAS_MOUNTED_FILESYSTEMS")
    swap = Path("/proc/swaps").read_text().splitlines()[1:]
    if any(re.match(r"^" + re.escape(target["device"]) + r"(?:p\d+)?\s", line) for line in swap):
        blockers.append("TARGET_EMMC_USED_AS_SWAP")
    for name in [target["name"], *[part["name"] for part in target["partitions"]]]:
        holders = Path("/sys/class/block", name, "holders")
        if holders.exists() and list(holders.iterdir()):
            blockers.append("TARGET_EMMC_HAS_HOLDERS:" + name)
    return blockers


def build_plan(manifest_path, environment_path=ENVIRONMENT, cid=None, maintenance_lock_held=False):
    manifest_path = Path(manifest_path)
    manifest, artifacts, hashes = legacy.read_payload(manifest_path)
    mounts, disks = legacy.read_mounts(), legacy.mmc_disks()
    environment = environment_check(environment_path, manifest, manifest_path, mounts)
    for source in [manifest_path, *artifacts.values()]:
        no_symlink_components(source)
        backing = legacy.find_mount(source, mounts)
        if backing["fstype"] not in NON_TARGET_FS:
            legacy.fail("RAM_OR_NETWORK_PAYLOAD_SOURCE_REQUIRED")
    rootfs = legacy.audit_rootfs(artifacts["rootfs_tar"], manifest["rootfs"], manifest["kernel_release"], manifest["system_identity"])
    fit, boot = legacy.audit_fit(artifacts["fit"], manifest), legacy.audit_uboot_script(artifacts["boot_script"])
    candidates = [disk for disk in disks if disk["type"] == "MMC" and not disk["removable"] and (cid is None or disk["cid"] == require_cid(cid))]
    blockers, target = [], None
    if len(candidates) != 1:
        blockers.append("EXACTLY_ONE_IDENTIFIED_EMMC_REQUIRED")
    else:
        target = candidates[0]
        blockers.extend(target_blockers(target, mounts))
        if target["bytes"] < legacy.required_bytes(manifest)["disk_minimum_bytes"]:
            blockers.append("EMMC_CAPACITY_INSUFFICIENT_FOR_COMPLETE_DEVELOPMENT_ROOTFS")
    missing = [tool for tool in legacy.REQUIRED_TOOLS if shutil.which(tool) is None]
    if missing:
        blockers.append("INSTALLATION_TOOLS_MISSING:" + ",".join(missing))
    idle = None
    try:
        idle = legacy.idle_proof(maintenance_lock_held=maintenance_lock_held)
    except (RuntimeError, ValueError, OSError, subprocess.TimeoutExpired) as exc:
        blockers.append(str(exc))
    plan = {"schema": "eeg.zu3eg.emmc_ram_plan.v1", "environment": environment,
            "manifest": {"path": str(manifest_path.resolve()), "sha256": legacy.sha(manifest_path)},
            "release": manifest["release"], "artifacts": hashes, "rootfs": rootfs, "fit": fit, "boot_script": boot,
            "capacity": legacy.required_bytes(manifest), "target": target, "emmc_candidates": candidates,
            "source_model": "verified RAM/network payload; independent RAM root", "live_maintenance_state": idle,
            "overwrite": None if target is None else {"area": "emmc_user_area", "offset": 0, "bytes": target["bytes"]},
            "excluded_from_writes": ["boot0", "boot1", "RPMB", "EXT_CSD", "QSPI"],
            "blockers": blockers, "software_plan_clear": not blockers, "can_apply": False}
    plan["plan_sha256"] = digest(canonical(plan))
    return plan, manifest, artifacts


class HostJournal:
    def __init__(self, base, attempt, cid, requester=request_json):
        if not re.fullmatch(r"[0-9a-f]{32}", attempt):
            legacy.fail("UNIQUE_UUIDHEX_ATTEMPT_REQUIRED")
        self.base, self.attempt, self.cid, self.request = url_base(base), attempt, require_cid(cid), requester
        self.sequence, self.previous = 0, None

    def assert_no_unknown(self):
        status = self.request(self.base, "/v1/journal/target/" + self.cid)
        attempts = status.get("attempts")
        if not isinstance(attempts, list):
            legacy.fail("HOST_TARGET_ATTEMPT_STATUS_REQUIRED")
        if any(item.get("terminal") is not True for item in attempts):
            legacy.fail("PRIOR_TARGET_ACTION_UNKNOWN_OBSERVE_BEFORE_NEW_WRITE")
        if any(item.get("attempt") == self.attempt for item in attempts):
            legacy.fail("ATTEMPT_ALREADY_USED_NEW_CONFIRMED_PLAN_REQUIRED")

    def event(self, phase, action, payload):
        event = {"schema": "eeg.zu3eg.ram_journal_event.v1", "attempt": self.attempt,
                 "sequence": self.sequence, "prev_sha256": self.previous, "target_cid": self.cid,
                 "phase": phase, "action": action, "payload": payload}
        raw = canonical(event)
        reply = self.request(self.base, "/v1/journal/" + self.attempt + "/" + format(self.sequence, "06d"), event, "POST")
        expected = digest(raw)
        if reply.get("accepted") is not True or reply.get("sha256") != expected or reply.get("sequence") != self.sequence:
            legacy.fail("DURABLE_HOST_JOURNAL_ACK_REQUIRED_NO_TARGET_ACTION_STARTED")
        self.sequence += 1
        self.previous = expected
        return event


def backup_area(base, cid, device, size_bytes, area, backup_id=None, opener=urllib.request.urlopen, requester=request_json):
    """Only opens device rb. Host validates every chunk and re-reads complete backup."""
    require_cid(cid)
    backup_id = backup_id or uuid.uuid4().hex
    if not re.fullmatch(r"[0-9a-f]{32}", backup_id) or area not in ("user_area", "boot0", "boot1") or type(size_bytes) is not int or size_bytes <= 0:
        legacy.fail("EXACT_READ_ONLY_BACKUP_SCOPE_REQUIRED")
    prefix = "/v1/backup/" + backup_id
    meta = {"schema": "eeg.zu3eg.emmc_backup.v1", "target_cid": cid, "size_bytes": size_bytes,
            "chunk_bytes": BACKUP_CHUNK, "area": area}
    print(json.dumps({"BACKUP_READ_ONLY_BEGIN": meta, "backup_id": backup_id}), file=sys.stderr, flush=True)
    reply = requester(base, prefix + "/begin", meta, "POST")
    if reply.get("accepted") is not True:
        legacy.fail("HOST_BACKUP_BEGIN_ACK_REQUIRED")
    status = requester(base, prefix + "/status")
    for key, expected in (("target_cid", cid), ("size_bytes", size_bytes), ("chunk_bytes", BACKUP_CHUNK), ("area", area)):
        if status.get(key) != expected:
            legacy.fail("HOST_BACKUP_RESUME_METADATA_MISMATCH:" + key)
    previous = {row["sequence"]: row for row in status.get("chunks", [])}
    whole, offset, sequence = hashlib.sha256(), 0, 0
    with Path(device).open("rb", buffering=0) as stream:
        while offset < size_bytes:
            data = stream.read(min(BACKUP_CHUNK, size_bytes - offset))
            if len(data) != min(BACKUP_CHUNK, size_bytes - offset):
                legacy.fail("BACKUP_SOURCE_SHORT_READ")
            piece_sha = digest(data)
            whole.update(data)
            existing = previous.get(sequence)
            if existing is not None:
                if existing.get("sha256") != piece_sha or existing.get("bytes") != len(data):
                    legacy.fail("BACKUP_RESUME_SOURCE_DIFFERS_FROM_HOST_VERIFIED_CHUNK")
            else:
                request = urllib.request.Request(url_base(base) + prefix + "/chunks/" + format(sequence, "06d"), data=data, method="PUT",
                                                 headers={"Content-Type": "application/octet-stream", "X-Content-SHA256": piece_sha})
                with opener(request, timeout=120) as response:
                    ack = json.loads(response.read(65537).decode("utf-8"))
                if ack.get("accepted") is not True or ack.get("sequence") != sequence or ack.get("sha256") != piece_sha or ack.get("bytes") != len(data):
                    legacy.fail("HOST_BACKUP_CHUNK_DURABLE_ACK_REQUIRED")
            offset += len(data)
            sequence += 1
            if offset % (64 * 1024**2) == 0 or offset == size_bytes:
                print(json.dumps({"BACKUP_READ_ONLY_PROGRESS": backup_id, "verified_bytes": offset, "total_bytes": size_bytes}), file=sys.stderr, flush=True)
    final_sha = whole.hexdigest()
    reply = requester(base, prefix + "/complete", {"sha256": final_sha, "size_bytes": size_bytes}, "POST")
    if reply.get("verified") is not True or reply.get("sha256") != final_sha or reply.get("bytes") != size_bytes or not reply.get("backup_location"):
        legacy.fail("HOST_FULL_BACKUP_READBACK_ACK_REQUIRED")
    return dict(reply, backup_id=backup_id, area=area, target_cid=cid)


def backup(args):
    plan, _, _ = build_plan(args.manifest, args.environment, require_cid(args.target_cid))
    if plan["blockers"]:
        legacy.fail("BACKUP_PLAN_BLOCKED:" + ",".join(plan["blockers"]))
    target, areas = plan["target"], []
    areas.append(backup_area(args.host_url, target["cid"], target["device"], target["bytes"], "user_area"))
    for suffix in ("boot0", "boot1"):
        path = Path("/sys/class/block", target["name"] + suffix, "size")
        if path.exists():
            length = int(path.read_text().strip()) * 512
            areas.append(backup_area(args.host_url, target["cid"], target["device"] + suffix, length, suffix))
    current = next((item for item in legacy.mmc_disks() if item["cid"] == target["cid"]), None)
    if current != target:
        legacy.fail("TARGET_CHANGED_DURING_BACKUP")
    result = {"schema": "eeg.zu3eg.emmc_ram_backup_result.v1", "target_cid": target["cid"], "user_bytes": target["bytes"],
              "areas": areas, "target_writes": False, "boot_areas_modified": False}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


def validate_decision(record, plan):
    target = plan["target"]
    if record.get("schema") != "eeg.zu3eg.emmc_ram_consent.v1" or record.get("target_cid") != target["cid"] or record.get("user_bytes") != target["bytes"]:
        legacy.fail("CURRENT_EXACT_TARGET_CONSENT_REQUIRED")
    if record.get("plan_sha256") != plan["plan_sha256"] or record.get("ram_boot_id") != plan["environment"]["boot_id"]:
        legacy.fail("CONSENT_MUST_BIND_CURRENT_PLAN_AND_RAM_BOOT")
    if record.get("operator_confirmed_erase") is not True or record.get("adc_isolated") is not True or record.get("overwrite") != plan["overwrite"]:
        legacy.fail("CURRENT_PHYSICAL_AND_EXACT_USER_AREA_ERASE_CONFIRMATION_REQUIRED")
    recovery = record.get("recovery", {})
    if recovery.get("route") != "jtag_ram" or recovery.get("environment_sha256") != plan["environment"]["sha256"] or not valid_sha(recovery.get("board_boot_receipt_sha256")):
        legacy.fail("CURRENT_VERIFIED_JTAG_RECOVERY_RECEIPT_REQUIRED")
    decision = record.get("backup_decision", {})
    if decision.get("decision") == "no_unique_data_confirmed":
        if decision.get("operator_confirmed_no_unique_data") is not True or not decision.get("reason"):
            legacy.fail("EXPLICIT_NO_UNIQUE_DATA_DECISION_REQUIRED")
    elif decision.get("decision") == "backup_completed":
        areas = decision.get("areas", [])
        user = [row for row in areas if row.get("area") == "user_area"]
        if len(user) != 1 or user[0].get("bytes") != target["bytes"] or any(
                row.get("verified") is not True or row.get("target_cid") != target["cid"] or not valid_sha(row.get("sha256"))
                or not row.get("backup_location") or not re.fullmatch(r"[0-9a-f]{32}", str(row.get("backup_id", ""))) for row in areas):
            legacy.fail("COMPLETE_HOST_VERIFIED_TARGET_BACKUP_REQUIRED")
    else:
        legacy.fail("BACKUP_OR_EXPLICIT_NO_UNIQUE_DATA_DECISION_REQUIRED")
    return record


def run_command(argv, log, journal, input_data=None):
    journal.event("BEFORE_COMMAND", str(argv[0]), {"argv": list(map(str, argv)), "stdin_sha256": None if input_data is None else digest(input_data)})
    value = legacy.invoke(argv, log, input_data)
    journal.event("AFTER_COMMAND", str(argv[0]), {"returncode": 0})
    return value


def install_worker(plan, manifest, artifacts, manifest_path, journal, workdir, receipt):
    """Real production worker. Tests fake command I/O, not the orchestration."""
    target, commands = plan["target"], receipt["commands"]
    current = next((item for item in legacy.mmc_disks() if item["device"] == target["device"]), None)
    if current != target or target_blockers(target, legacy.read_mounts()):
        legacy.fail("TARGET_CHANGED_OR_BECAME_BUSY_BEFORE_WRITE")
    legacy.idle_proof(maintenance_lock_held=True)
    root, boot = workdir / "root", workdir / "boot"
    root.mkdir()
    boot.mkdir()
    mounted = []
    command = lambda argv, data=None: run_command(argv, commands, journal, data)
    device = target["device"]
    disk_id = os.urandom(4).hex()
    boot_sectors = manifest["layout"]["boot_mib"] * 2048
    table = ("label: dos\nlabel-id: 0x" + disk_id + "\nunit: sectors\n\nstart=2048, size=" + str(boot_sectors) +
             ", type=c, bootable\nstart=" + str(2048 + boot_sectors) + ", type=83\n").encode("ascii")
    try:
        command(["sfdisk", "--wipe", "always", "--wipe-partitions", "always", device], table)
        command(["blockdev", "--rereadpt", device])
        command(["udevadm", "settle", "--timeout=15"])
        boot_part, root_part = legacy.wait_partitions(device)
        command(["mkfs.vfat", "-F", "32", "-n", "EEGBOOT", str(boot_part)])
        command(["mkfs.ext4", "-F", "-m", "1", "-L", "EEGROOT", str(root_part)])
        command(["mount", "-t", "ext4", str(root_part), str(root)])
        mounted.append(root)
        command(["mount", "-t", "vfat", str(boot_part), str(boot)])
        mounted.append(boot)
        journal.event("BEFORE_BOOT_COPY", "copy_verified_boot", {"roles": ["boot_bin", "fit", "boot_script"]})
        for role in ("boot_bin", "fit", "boot_script"):
            shutil.copyfile(artifacts[role], boot / legacy.ROLES[role])
            if legacy.sha(boot / legacy.ROLES[role]) != legacy.sha(artifacts[role]):
                legacy.fail("EMMC_BOOT_READBACK_HASH_MISMATCH:" + role)
        shutil.copyfile(manifest_path, boot / "system_payload.manifest.json")
        command(["tar", "--numeric-owner", "--xattrs", "--acls", "-xpf", str(artifacts["rootfs_tar"]), "-C", str(root)])
        receipt["rootfs_readback"] = legacy.verify_installed_rootfs(artifacts["rootfs_tar"], root)
        legacy.identity_equal(legacy.json_read(root / "etc/eeg-system/baseline.json"), manifest["system_identity"])
        legacy.verify_required_installed_paths(root, manifest["rootfs"]["required_paths"], manifest["rootfs"]["init_system"])
        root_uuid = command(["blkid", "-s", "UUID", "-o", "value", str(root_part)])
        boot_uuid = command(["blkid", "-s", "UUID", "-o", "value", str(boot_part)])
        partuuid = command(["blkid", "-s", "PARTUUID", "-o", "value", str(root_part)])
        if not root_uuid or not boot_uuid or partuuid.lower() != disk_id + "-02":
            legacy.fail("NEW_FILESYSTEM_IDENTITIES_OR_PARTUUID_INVALID")
        fstab = root / "etc/fstab"
        preserved = []
        for line in fstab.read_text().splitlines():
            fields = line.split()
            if fields and not fields[0].startswith("#") and len(fields) >= 2 and fields[1] in ("/", "/boot"):
                continue
            preserved.append(line)
        journal.event("BEFORE_FSTAB", "write_target_uuid_fstab", {"root_uuid": root_uuid, "boot_uuid": boot_uuid})
        fstab.write_text("\n".join(preserved + ["UUID=" + root_uuid + " / ext4 defaults,noatime 0 1", "UUID=" + boot_uuid + " /boot vfat defaults,nofail,umask=0077 0 2"]) + "\n")
        # RAM credentials and temporary host keys MUST NOT become daily-system identity.
        receipt["persistent_config_preserved"] = []
        free = os.statvfs(root).f_bavail * os.statvfs(root).f_frsize
        if free < manifest["layout"]["root_min_free_bytes"]:
            legacy.fail("COMPLETE_ROOTFS_LEAVES_INSUFFICIENT_DEVELOPMENT_SPACE")
        receipt["installed"] = {"root_uuid": root_uuid, "boot_uuid": boot_uuid, "root_partuuid": partuuid,
                                "root_free_bytes": free, "fstab_sha256": legacy.sha(fstab), "boot_partitions_modified": False,
                                "boot_source_after_install": "UNVERIFIED_UNTIL_EMMC_BOOT"}
        command(["sync"])
        for path in reversed(mounted):
            command(["umount", str(path)])
        mounted.clear()
        command(["fsck.fat", "-n", str(boot_part)])
        command(["e2fsck", "-f", "-n", str(root_part)])
        command(["mount", "-t", "ext4", "-o", "ro,noload", str(root_part), str(root)])
        mounted.append(root)
        receipt["rootfs_readback_after_flush"] = legacy.verify_installed_rootfs(artifacts["rootfs_tar"], root, {"etc/fstab"})
        if legacy.sha(legacy.installed_path(root, "etc/fstab")) != receipt["installed"]["fstab_sha256"]:
            legacy.fail("EMMC_FSTAB_POST_FLUSH_READBACK_MISMATCH")
        legacy.identity_equal(legacy.json_read(root / "etc/eeg-system/baseline.json"), manifest["system_identity"])
        legacy.verify_required_installed_paths(root, manifest["rootfs"]["required_paths"], manifest["rootfs"]["init_system"])
        command(["umount", str(root)])
        mounted.clear()
        command(["mount", "-t", "vfat", "-o", "ro", str(boot_part), str(boot)])
        mounted.append(boot)
        for role in ("boot_bin", "fit", "boot_script"):
            if legacy.sha(boot / legacy.ROLES[role]) != legacy.sha(artifacts[role]):
                legacy.fail("POST_FLUSH_BOOT_READBACK_MISMATCH:" + role)
        if legacy.sha(boot / "system_payload.manifest.json") != legacy.sha(manifest_path):
            legacy.fail("POST_FLUSH_MANIFEST_READBACK_MISMATCH")
        command(["umount", str(boot)])
        mounted.clear()
        receipt["result"] = TERMINAL_PHASE
    finally:
        for path in reversed(mounted):
            try:
                command(["umount", str(path)])
            except Exception as exc:
                receipt.setdefault("cleanup_errors", []).append(str(exc))


def apply(args):
    import fcntl
    if os.geteuid() != 0:
        legacy.fail("ROOT_REQUIRED_FOR_EMMC_INSTALL")
    cid = require_cid(args.target_cid)
    if args.confirm_erase != cid or args.consent is None:
        legacy.fail("EXACT_CID_AND_CURRENT_CONSENT_REQUIRED")
    journal = HostJournal(args.host_url, args.attempt, cid)
    journal.assert_no_unknown()
    lock = Path("/run/eeg-operator/maintenance.lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    marker = lock.with_name("maintenance.active")
    with lock.open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with marker.open("x") as stream:
            stream.write("emmc-ram-install " + args.attempt + "\n")
        receipt = {"schema": "eeg.zu3eg.emmc_ram_install_receipt.v1", "attempt": args.attempt,
                   "started_at": legacy.now(), "result": "INCOMPLETE", "commands": [], "board_boot_verification": "NOT_EXERCISED"}
        written = False
        try:
            plan, manifest, artifacts = build_plan(args.manifest, args.environment, cid, maintenance_lock_held=True)
            if plan["blockers"]:
                legacy.fail("INSTALLATION_BLOCKED:" + ",".join(plan["blockers"]))
            record = validate_decision(legacy.json_read(args.consent), plan)
            if record.get("attempt") != args.attempt:
                legacy.fail("CONSENT_MUST_BIND_EXACT_SINGLE_USE_ATTEMPT")
            # A backup decision is checked against durable host state, not trusted JSON alone.
            for area in record["backup_decision"].get("areas", []):
                status = request_json(args.host_url, "/v1/backup/" + area["backup_id"] + "/status")
                if status.get("verified") is not True or status.get("sha256") != area["sha256"] or status.get("size_bytes") != area["bytes"] or status.get("target_cid") != cid:
                    legacy.fail("HOST_BACKUP_STATUS_DOES_NOT_MATCH_CONSENT")
            receipt["plan"] = plan
            receipt["consent_sha256"] = legacy.sha(args.consent)
            journal.event("AUTHORIZED", "confirmed_exact_user_area", {"plan_sha256": plan["plan_sha256"], "consent_sha256": receipt["consent_sha256"]})
            # Once this flag is set, a failure remains UNKNOWN even if command ACK is lost.
            written = True
            work = Path(tempfile.mkdtemp(prefix="eeg-emmc-ram-", dir="/run"))
            install_worker(plan, manifest, artifacts, args.manifest, journal, work, receipt)
            if receipt.get("cleanup_errors"):
                legacy.fail("CLEANUP_INCOMPLETE_TARGET_STATE_UNKNOWN")
            journal.event(TERMINAL_PHASE, "install_complete_boot_pending", receipt)
            marker.unlink()
        except Exception as exc:
            receipt["error"] = str(exc)
            receipt["result"] = "FAILED_UNKNOWN" if written else "REJECTED_BEFORE_WRITE"
            with contextlib.suppress(Exception):
                journal.event(receipt["result"], "stop_observe_before_any_repeat", receipt)
            if not written:
                marker.unlink()
            raise
        finally:
            print(json.dumps({"result": receipt["result"], "attempt": args.attempt,
                              "host_journal": args.host_url, "emmc_boot": "NOT_EXERCISED", "reboot_performed": False}, ensure_ascii=False), flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("transport", "fetch", "plan", "backup", "apply"), nargs="?", default="plan")
    parser.add_argument("--manifest", type=Path, default=CACHE / "system_payload.manifest.json")
    parser.add_argument("--environment", type=Path, default=ENVIRONMENT)
    parser.add_argument("--transport", type=Path)
    parser.add_argument("--transport-sha256")
    parser.add_argument("--payload-manifest-sha256")
    parser.add_argument("--cache", type=Path, default=CACHE)
    parser.add_argument("--host-url")
    parser.add_argument("--target-cid")
    parser.add_argument("--confirm-erase")
    parser.add_argument("--consent", type=Path)
    parser.add_argument("--attempt", default=uuid.uuid4().hex)
    args = parser.parse_args(argv)
    if args.action == "transport":
        print(json.dumps(create_transport_manifest(args.manifest), ensure_ascii=False, indent=2))
    elif args.action == "fetch":
        if args.transport is None or legacy.sha(args.transport) != args.transport_sha256:
            legacy.fail("PINNED_TRANSPORT_FILE_SHA_REQUIRED")
        print(json.dumps(fetch_payload(args.host_url, legacy.json_read(args.transport), args.payload_manifest_sha256, args.cache), indent=2))
    elif args.action == "plan":
        plan, _, _ = build_plan(args.manifest, args.environment, args.target_cid)
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 0 if plan["software_plan_clear"] else 3
    elif args.action == "backup":
        backup(args)
    else:
        apply(args)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(json.dumps({"schema": "eeg.zu3eg.emmc_ram_error.v1", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        sys.exit(3)
