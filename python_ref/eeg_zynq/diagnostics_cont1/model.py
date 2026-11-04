"""Bounded file windows and host annotations, separate from acquisition control."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import time
import uuid

from eeg_zynq.continuous.protocol import (
    CHANNELS, CHUNK, Decoder, HEADER, HELLO, INTERVAL_NS, LIVE, MAX_PAYLOAD,
    RAW, RATE, SIMULATED, decode_raw, validate_hello,
)

PROJECT = Path(__file__).resolve().parents[3]
BUILD = PROJECT / "build/p5b_continuous_cap_v1"
OUTPUT = BUILD / "diagnostics/observer_01/sessions"
EVENT28_RAW = BUILD / "events/axis_dma_ddr_cont_cap_20261018_28/stream/raw_dma.bin"
MAX_RAW_BYTES = 8 * 1024 * 1024
MAX_WINDOW = 8192
SHANGHAI = dt.timezone(dt.timedelta(hours=8))
FC4_CHAIN = "FC4 / cap CH13 / board 2P / U1.IN2P"


def timestamp():
    return dt.datetime.now(SHANGHAI).isoformat(timespec="milliseconds")


def bounded_json(path):
    if path.stat().st_size > 65536:
        raise ValueError("METADATA_SIZE_BOUND")
    return json.loads(path.read_bytes().decode("utf-8"))


def quality(rows):
    """Only the visible raw window; never a replacement for full-record metrics."""
    if not rows:
        return {"frames": 0, "channels": [], "max_simultaneous_endpoints": 0}
    result = []
    for i in range(16):
        values = [row[i] for row in rows]
        negative = values.count(-8388608)
        positive = values.count(8388607)
        run = longest = 0
        for value in values:
            run = run + 1 if value in (-8388608, 8388607) else 0
            longest = max(longest, run)
        result.append(dict(name=CHANNELS[i], minimum=min(values), maximum=max(values),
                           mean=sum(values) / len(values), latest=values[-1],
                           negative=negative, positive=positive,
                           endpoint_ratio=(negative + positive) / len(values),
                           longest_endpoint_run=longest))
    return dict(frames=len(rows), channels=result,
                max_simultaneous_endpoints=max(
                    sum(value in (-8388608, 8388607) for value in row) for row in rows))


class RawObserver:
    """Open files read-only, at most 8192 frames per refresh. No socket/device API."""

    def __init__(self, path, *, observe=False, now=time.monotonic):
        self.path = Path(path).resolve(strict=True)
        if not self.path.is_relative_to(PROJECT / "build") or not self.path.is_file():
            raise ValueError("INPUT_MUST_BE_PROJECT_BUILD_FILE")
        self.observe, self.now = observe, now
        stat = self.path.stat()
        self.identity = (stat.st_dev, stat.st_ino)
        self.last_size = stat.st_size
        self.last_frames = stat.st_size // RAW.size
        self.growth_at = None
        self.skipped_display_frames = 0
        self.terminal = False
        self.source = {"source_mode": "FILE_REPLAY", "raw_path": str(self.path)}
        if observe:
            wire = self.path.parent / "net1_messages.bin"
            with wire.open("rb") as stream:
                header = stream.read(HEADER.size)
                if len(header) != HEADER.size:
                    raise ValueError("WAIT_FOR_COMPLETE_NET1_HELLO_BEFORE_OBSERVING")
                fields = HEADER.unpack(header)
                if fields[2] != HELLO or fields[3] not in (LIVE, SIMULATED) or fields[4] > MAX_PAYLOAD:
                    raise ValueError("NET1_HELLO_REQUIRED")
                data = header + stream.read(fields[4])
            messages = Decoder(fields[5], fields[3]).feed(data)
            if len(messages) != 1:
                raise ValueError("NET1_HELLO_INCOMPLETE")
            info = json.loads(messages[0].payload)
            validate_hello(info, fields[3])
            self.source.update(source_mode=info["source_mode"], session_tag=fields[5].hex(),
                               hello_sha256=hashlib.sha256(data).hexdigest(),
                               evidence="RECORDER_HELLO_AND_NEW_RAW_WRITES_NOT_INDEPENDENT_HARDWARE_ATTESTATION")
        self._check_size(stat.st_size)

    def _check_size(self, size):
        if size > MAX_RAW_BYTES:
            raise ValueError("RAW_SIZE_BOUND")
        if not self.observe and size % RAW.size:
            raise ValueError("REPLAY_PARTIAL_FRAME")

    def read_window(self, *, seconds=10, end_seconds=None):
        if seconds not in (2, 5, 10, 30):
            raise ValueError("WINDOW_BOUND")
        now = self.now()
        stat = self.path.stat()
        if (stat.st_dev, stat.st_ino) != self.identity or stat.st_size < self.last_size:
            raise ValueError("SOURCE_REPLACED_OR_TRUNCATED_NO_AUTOMATIC_RECOVERY")
        self._check_size(stat.st_size)
        total = stat.st_size // RAW.size
        grew = total > self.last_frames
        if grew:
            self.growth_at = now
            self.skipped_display_frames += max(0, total - self.last_frames - MAX_WINDOW)
        self.last_size, self.last_frames = stat.st_size, total
        end = total if self.observe or end_seconds is None else min(total, max(0, round(end_seconds * RATE)))
        start = max(0, end - min(MAX_WINDOW, round(seconds * RATE)))
        with self.path.open("rb") as stream:
            if (os.fstat(stream.fileno()).st_dev, os.fstat(stream.fileno()).st_ino) != self.identity:
                raise ValueError("SOURCE_REPLACED_DURING_READ")
            stream.seek(start * RAW.size)
            data = stream.read((end - start) * RAW.size)
        if len(data) != (end - start) * RAW.size:
            raise ValueError("SHORT_WINDOW_READ")
        rows = []
        for offset in range(0, len(data), CHUNK * RAW.size):
            rows.extend(decode_raw(data[offset:offset + CHUNK * RAW.size], start + offset // RAW.size))
        age = None if self.growth_at is None else max(0, now - self.growth_at)
        mode, state, target_state = "FILE_REPLAY", "SAVED_WINDOW_NOT_LIVE", "NOT_QUERIED"
        if self.observe:
            manifest = self.path.parent / "manifest.json"
            if manifest.exists():
                self.terminal = True  # Even an in-progress final manifest ends fresh-stream claims.
                state = "RECORDING_CLOSED_TARGET_STATE_SEPARATE"
                try:
                    target_state = bounded_json(manifest).get("state", {}).get("state", "UNKNOWN")
                except json.JSONDecodeError:
                    target_state = "MANIFEST_WRITE_PENDING"
            if self.terminal:
                mode = "ARCHIVED_FILE_NOT_LIVE"
            elif age is None:
                mode, state = "AWAITING_NEW_RECORDED_DATA", "NO_NEW_COMPLETE_RAW_FRAME_OBSERVED"
            elif age > 2:
                mode, state = "STALE_RECORDED_STREAM", "NO_FRESH_RAW_WRITES_TARGET_STATE_UNKNOWN"
            else:
                mode, state = self.source["source_mode"], "READ_ONLY_RECORDED_STREAM_OBSERVER"
        return dict(samples=rows, start_sequence=start, end_sequence_exclusive=end,
                    total_recorded_frames=total, pending_partial_bytes=stat.st_size % RAW.size,
                    mode=mode, state=state, target_state=target_state, source=dict(self.source),
                    fresh_write_age_seconds=age, skipped_observer_display_frames=self.skipped_display_frames,
                    quality=quality(rows), interval_ns=INTERVAL_NS,
                    raw_checks="VISIBLE_WINDOW_HEADERS_SEQUENCE_SIGN_ONLY_NOT_FULL_RECORD_REVALIDATION")


class AnnotationJournal:
    """New sidecar notes only; never write inside an input/event directory."""

    def __init__(self, source, *, folder=None):
        self.folder = (Path(folder) if folder else OUTPUT / (
            dt.datetime.now(SHANGHAI).strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:8])).resolve()
        if not self.folder.is_relative_to(OUTPUT.resolve()):
            raise ValueError("NOTES_MUST_USE_DIAGNOSTICS_SESSION_DIRECTORY")
        self.folder.mkdir(parents=True, exist_ok=False)
        self.source, self.entries, self.bytes = dict(source), 0, 0
        self.began = time.monotonic()
        self.closed = False
        (self.folder / "session.json").write_text(json.dumps(dict(
            schema="eeg.cont1.observer.v1", created_at=timestamp(), source=source,
            hardware_controller=False, hardware_operations=0,
            annotation_time_basis="HOST_SHANGHAI_NOT_ADC_MEASUREMENT"), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8", newline="\n")
        self.file = (self.folder / "annotations.jsonl").open("xb")

    def add(self, snapshot, text, *, displayed_end=None):
        if self.closed:
            raise ValueError("JOURNAL_CLOSED")
        if not text.strip() or "\ufffd" in text or len(text.encode("utf-8")) > 512 or any(
                ord(char) < 32 or ord(char) == 127 for char in text):
            raise ValueError("NOTE_REQUIRES_1_TO_512_UTF8_BYTES_WITHOUT_CONTROLS")
        note = dict(schema="eeg.cont1.observer.note.v1", host_shanghai=timestamp(),
                    observer_elapsed_seconds=time.monotonic() - self.began,
                    text=text, mode=snapshot["mode"], state=snapshot["state"],
                    latest_observed_sequence=snapshot["end_sequence_exclusive"] - 1,
                    displayed_end_sequence_exclusive=displayed_end,
                    design_elapsed_seconds=snapshot["end_sequence_exclusive"] * INTERVAL_NS / 1e9,
                    physical_action_proven=False,
                    relation="POSTHOC_ANNOTATION" if not self.source.get("observe") else
                             "HOST_NOTE_LINKED_TO_LAST_OBSERVED_RAW_NOT_SYNCHRONIZED_ACTION_TIME")
        data = (json.dumps(note, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        if self.entries >= 2048 or self.bytes + len(data) > 1024 * 1024:
            raise ValueError("NOTE_LIMIT_STOP_ANNOTATING_ACQUISITION_UNCHANGED")
        if self.file.write(data) != len(data):
            raise OSError("SHORT_NOTE_WRITE")
        self.file.flush()
        os.fsync(self.file.fileno())
        self.entries += 1
        self.bytes += len(data)
        return note

    def close(self):
        if not self.closed:
            self.closed = True
            self.file.close()
            with (self.folder / "closed.json").open("x", encoding="utf-8", newline="\n") as stream:
                json.dump(dict(closed_at=timestamp(), entries=self.entries, note_bytes=self.bytes,
                               acquisition_stop_requested=False, hardware_operations=0), stream, indent=2)
                stream.write("\n")
