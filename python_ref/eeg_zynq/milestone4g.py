"""Offline sustained-stream recording and deterministic replay for milestone 4G.

The binary container keeps every payload record as the existing 112-byte
``EegFrameV1``.  A fixed header and footer add session identity, loss counters,
termination state, and independent SHA-256 coverage without assigning a PS
address or exposing a physical MMIO backend.
"""

from __future__ import annotations

import hashlib
import json
import struct
import uuid
import zlib
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import BinaryIO, Callable, Iterator

from .dsp import DspProcessor, FilteredFrame
from .frame import EEG_CHANNELS, EEG_FRAME_V1_BYTES, EegFrameV1, ErrorFlag, SourceType
from .milestone4f import (
    FrameBufferControl,
    FrameBufferController,
    FrameBufferFakeMMIO,
    FrameBufferRegister,
    FramePair,
)

HARDWARE_EXECUTION_ALLOWED = False
from .milestone4_common import (
    ADS1299_ID_EXPECTED as EXPECTED_ID,
    ADS1299_ID_MASK as ID_MASK,
    SYSTEM_AXI_BASE_ADDRESS,
    dual_id_gate_passed,
)
REAL_PS_AXI_INTEGRATED = False
DEFAULT_SAMPLE_RATE_MILLIHZ = 250_000
DEFAULT_FIFO_DEPTH = 4
SEQUENCE_MASK = 0xFFFF_FFFF

SESSION_VERSION = 1
SESSION_HEADER_BYTES = 1024
SESSION_FOOTER_BYTES = 1024
SESSION_HEADER_MAGIC = b"EEG4GHD1"
SESSION_FOOTER_MAGIC = b"EEG4GFT1"
_BLOCK_PREFIX = struct.Struct("<8sHHII")


class SessionError(ValueError):
    """Base class for invalid 4G sessions or operations."""


class SessionWriteError(SessionError):
    """A streaming session could not be written or finalized."""


class SessionIntegrityError(SessionError):
    """A session failed structural, frame, or session-level integrity checks."""


class ReplayMode(str, Enum):
    ONE_X = "1X"
    ACCELERATED = "ACCELERATED"
    STEP = "STEP"


@dataclass(frozen=True, slots=True)
class ThroughputCapacity:
    sample_rate_hz: int
    paired_ads_payload_bytes: int
    paired_ads_payload_bytes_per_second: int
    frame_bytes: int
    payload_bytes_per_second: int
    payload_bytes_per_minute: int
    payload_bytes_per_hour: int
    one_minute_session_bytes: int
    one_hour_session_bytes: int
    one_minute_average_file_bytes_per_second: float
    one_hour_average_file_bytes_per_second: float
    fifo_depth: int
    buffered_time_ms: float
    first_overflow_at_pause_ms: float


def throughput_capacity(
    *, sample_rate_hz: int = 250, frame_bytes: int = EEG_FRAME_V1_BYTES,
    fifo_depth: int = DEFAULT_FIFO_DEPTH,
) -> ThroughputCapacity:
    if sample_rate_hz <= 0 or frame_bytes <= 0 or fifo_depth <= 0:
        raise ValueError("rate, frame size, and FIFO depth must be positive")
    per_second = sample_rate_hz * frame_bytes
    paired_ads_bytes = 2 * 216 // 8
    minute_session = SESSION_HEADER_BYTES + per_second * 60 + SESSION_FOOTER_BYTES
    hour_session = SESSION_HEADER_BYTES + per_second * 3600 + SESSION_FOOTER_BYTES
    return ThroughputCapacity(
        sample_rate_hz=sample_rate_hz,
        paired_ads_payload_bytes=paired_ads_bytes,
        paired_ads_payload_bytes_per_second=sample_rate_hz * paired_ads_bytes,
        frame_bytes=frame_bytes,
        payload_bytes_per_second=per_second,
        payload_bytes_per_minute=per_second * 60,
        payload_bytes_per_hour=per_second * 3600,
        one_minute_session_bytes=minute_session,
        one_hour_session_bytes=hour_session,
        one_minute_average_file_bytes_per_second=minute_session / 60.0,
        one_hour_average_file_bytes_per_second=hour_session / 3600.0,
        fifo_depth=fifo_depth,
        buffered_time_ms=1000.0 * fifo_depth / sample_rate_hz,
        first_overflow_at_pause_ms=1000.0 * (fifo_depth + 1) / sample_rate_hz,
    )


def expected_session_bytes(frame_count: int) -> int:
    if frame_count < 0:
        raise ValueError("frame_count cannot be negative")
    return SESSION_HEADER_BYTES + frame_count * EEG_FRAME_V1_BYTES + SESSION_FOOTER_BYTES


def id_triplets_pass(ads1_ids: tuple[int, int, int], ads2_ids: tuple[int, int, int]) -> bool:
    return dual_id_gate_passed(ads1_ids, ads2_ids)


def _canonical_json(document: dict[str, object]) -> bytes:
    return json.dumps(document, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True).encode("ascii")


def _encode_block(magic: bytes, size: int, document: dict[str, object]) -> bytes:
    payload = _canonical_json(document)
    if len(payload) > size - _BLOCK_PREFIX.size:
        raise SessionWriteError(f"session metadata exceeds fixed block size {size}")
    prefix = _BLOCK_PREFIX.pack(magic, SESSION_VERSION, size, len(payload),
                                zlib.crc32(payload) & 0xFFFF_FFFF)
    return prefix + payload + bytes(size - len(prefix) - len(payload))


def _decode_block(raw: bytes, magic: bytes, label: str) -> dict[str, object]:
    if len(raw) < _BLOCK_PREFIX.size:
        raise SessionIntegrityError(f"{label} is truncated")
    found_magic, version, block_size, length, stored_crc = _BLOCK_PREFIX.unpack(
        raw[:_BLOCK_PREFIX.size]
    )
    if found_magic != magic:
        raise SessionIntegrityError(f"invalid {label} magic")
    if version != SESSION_VERSION:
        raise SessionIntegrityError(f"unsupported {label} version {version}")
    if block_size != len(raw):
        raise SessionIntegrityError(f"{label} size field is inconsistent")
    if length > len(raw) - _BLOCK_PREFIX.size:
        raise SessionIntegrityError(f"{label} JSON length is invalid")
    payload = raw[_BLOCK_PREFIX.size:_BLOCK_PREFIX.size + length]
    if zlib.crc32(payload) & 0xFFFF_FFFF != stored_crc:
        raise SessionIntegrityError(f"{label} CRC32 mismatch")
    try:
        document = json.loads(payload.decode("ascii"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise SessionIntegrityError(f"invalid {label} JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise SessionIntegrityError(f"{label} JSON root is not an object")
    return document


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _sequence_relation(previous: int, current: int) -> tuple[str, int]:
    previous &= SEQUENCE_MASK
    current &= SEQUENCE_MASK
    expected = (previous + 1) & SEQUENCE_MASK
    if current == expected:
        return "CONTIGUOUS", 0
    if current == previous:
        return "DUPLICATE", 0
    distance = (current - previous) & SEQUENCE_MASK
    if 1 < distance < 0x8000_0000:
        return "GAP", distance - 1
    return "OUT_OF_ORDER", 0


@dataclass(frozen=True, slots=True)
class SessionInfo:
    path: Path
    session_id: str
    frame_count: int
    first_sequence: int
    last_sequence: int
    sample_rate_millihz: int
    time_source: str
    overflow_count: int
    dropped_count: int
    partial_pair_count: int
    sequence_gap_count: int
    recorder_gap_events: int
    missing_sequence_count: int
    duplicate_count: int
    out_of_order_count: int
    timestamp_error_count: int
    shutdown_reason: str
    complete_end: bool
    session_complete: bool
    data_sha256: str
    header_sha256: str
    file_sha256: str
    file_size: int


class SustainedSessionWriter:
    """Stream EegFrameV1 payloads into a fixed-header/fixed-footer container."""

    def __init__(
        self, path: str | Path, *, session_id: str | None = None,
        sample_rate_millihz: int = DEFAULT_SAMPLE_RATE_MILLIHZ,
        time_source: str = "SIMULATED_TIME",
        ads1_ids: tuple[int, int, int] = (0x3E, 0x3E, 0x3E),
        ads2_ids: tuple[int, int, int] = (0x3E, 0x3E, 0x3E),
        config_summary: str = "4D_OFFLINE_INTERNAL_TEST_CONFIG_PASSED",
        stream_factory: Callable[[Path], BinaryIO] | None = None,
    ) -> None:
        if not id_triplets_pass(ads1_ids, ads2_ids):
            raise SessionWriteError("ID gate failed; a normal 4G session cannot be created")
        if sample_rate_millihz <= 0:
            raise ValueError("sample rate must be positive")
        if time_source not in {"SIMULATED_TIME", "HOST_TIME_NOT_HARDWARE_TIMESTAMP"}:
            raise ValueError("time source must explicitly identify simulated or host time")
        self.path = Path(path).expanduser().resolve()
        if self.path.exists():
            raise SessionWriteError(f"session file already exists: {self.path}")
        self.session_id = session_id or str(uuid.uuid4())
        self.sample_rate_millihz = sample_rate_millihz
        self.time_source = time_source
        self.ads1_ids = ads1_ids
        self.ads2_ids = ads2_ids
        self.config_summary = config_summary
        self._stream_factory = stream_factory or (lambda value: value.open("x+b"))
        self._stream: BinaryIO | None = None
        self._data_hash = hashlib.sha256()
        self._frame_count = 0
        self._first_sequence: int | None = None
        self._last_sequence: int | None = None
        self._last_timestamp: int | None = None
        self._gap_events = self._missing_sequences = 0
        self._duplicates = self._out_of_order = self._timestamp_errors = 0
        self._closed = False
        self._write_error: str | None = None
        self._header = self._header_document(first_sequence=None)
        try:
            self._stream = self._stream_factory(self.path)
            self._write_exact(_encode_block(SESSION_HEADER_MAGIC, SESSION_HEADER_BYTES,
                                            self._header))
        except (OSError, ValueError, SessionWriteError) as exc:
            if self._stream is not None:
                self._stream.close()
            self._closed = True
            raise SessionWriteError(f"cannot create session {self.path}: {exc}") from exc

    def _header_document(self, *, first_sequence: int | None) -> dict[str, object]:
        return {
            "classification": "OFFLINE_SIMULATION_ONLY",
            "config_summary": self.config_summary,
            "format": "eeg-sustained-session-v1",
            "frame_format": "EegFrameV1",
            "frame_record_bytes": EEG_FRAME_V1_BYTES,
            "channel_count": EEG_CHANNELS,
            "id_gate": {
                "ads1": list(self.ads1_ids), "ads2": list(self.ads2_ids),
                "mask": ID_MASK, "expected": EXPECTED_ID, "passed": True,
            },
            "sample_rate_millihz": self.sample_rate_millihz,
            "session_id": self.session_id,
            "time_source": self.time_source,
            "virtual_time_base_ns": 0,
            "producer_sequence_bits": 32,
            "first_sequence": first_sequence,
            "hardware_execution": "NOT_EXECUTED",
        }

    def _write_exact(self, data: bytes) -> None:
        assert self._stream is not None
        written = self._stream.write(data)
        if written != len(data):
            raise OSError(f"short write: {written!r} of {len(data)} bytes")

    def append_frame(self, frame: EegFrameV1) -> None:
        if self._closed:
            raise SessionWriteError("session is already closed")
        if frame.source is not SourceType.SIMULATED:
            self._fail(f"4G records SIMULATED frames, not {frame.source.name}")
        if frame.sample_rate_millihz != self.sample_rate_millihz:
            self._fail("sample rate changed during the session")
        if frame.sequence > SEQUENCE_MASK:
            self._fail("4G producer sequence must fit the defined 32-bit domain")
        raw = frame.pack()
        try:
            if self._last_sequence is not None:
                relation, missing = _sequence_relation(self._last_sequence, frame.sequence)
                if relation == "GAP":
                    self._gap_events += 1
                    self._missing_sequences += missing
                elif relation == "DUPLICATE":
                    self._duplicates += 1
                elif relation == "OUT_OF_ORDER":
                    self._out_of_order += 1
                if self._last_timestamp is not None:
                    if frame.timestamp_ns <= self._last_timestamp:
                        self._timestamp_errors += 1
                    elif relation in {"CONTIGUOUS", "GAP"}:
                        delta = ((frame.sequence - self._last_sequence) & SEQUENCE_MASK)
                        expected_ns = self._last_timestamp + delta * (
                            1_000_000_000_000 // self.sample_rate_millihz
                        )
                        if frame.timestamp_ns != expected_ns:
                            self._timestamp_errors += 1
            self._write_exact(raw)
        except (OSError, ValueError) as exc:
            self._fail(f"write failed at frame {self._frame_count}: {exc}")
        self._data_hash.update(raw)
        if self._first_sequence is None:
            self._first_sequence = frame.sequence
        self._last_sequence = frame.sequence
        self._last_timestamp = frame.timestamp_ns
        self._frame_count += 1

    def _fail(self, message: str) -> None:
        self._write_error = message
        try:
            if self._stream is not None:
                self._stream.close()
        finally:
            self._closed = True
        raise SessionWriteError(message)

    def close(
        self, *, shutdown_reason: str = "SHUTDOWN_SAFE", complete_end: bool = True,
        overflow_count: int = 0, dropped_count: int = 0,
        partial_pair_count: int = 0, sequence_gap_count: int = 0,
    ) -> SessionInfo:
        if self._closed:
            raise SessionWriteError(self._write_error or "session is already closed")
        if self._frame_count == 0 or self._first_sequence is None or self._last_sequence is None:
            self._fail("cannot finalize an empty session")
        counts = (overflow_count, dropped_count, partial_pair_count, sequence_gap_count)
        if any(not isinstance(value, int) or value < 0 for value in counts):
            self._fail("integrity counters must be non-negative integers")
        assert self._stream is not None
        final_header = _encode_block(
            SESSION_HEADER_MAGIC, SESSION_HEADER_BYTES,
            self._header_document(first_sequence=self._first_sequence),
        )
        header_sha256 = hashlib.sha256(final_header).hexdigest().upper()
        session_complete = bool(
            complete_end and not any(counts) and not self._gap_events
            and not self._duplicates and not self._out_of_order
            and not self._timestamp_errors and shutdown_reason == "SHUTDOWN_SAFE"
        )
        footer_document: dict[str, object] = {
            "complete_end": bool(complete_end),
            "data_sha256": self._data_hash.hexdigest().upper(),
            "first_sequence": self._first_sequence,
            "format": "eeg-sustained-session-v1-footer",
            "frame_count": self._frame_count,
            "header_sha256": header_sha256,
            "last_sequence": self._last_sequence,
            "overflow_count": overflow_count,
            "dropped_count": dropped_count,
            "partial_pair_count": partial_pair_count,
            "sequence_gap_count": sequence_gap_count,
            "recorder_gap_events": self._gap_events,
            "missing_sequence_count": self._missing_sequences,
            "duplicate_count": self._duplicates,
            "out_of_order_count": self._out_of_order,
            "timestamp_error_count": self._timestamp_errors,
            "shutdown_reason": shutdown_reason,
            "session_complete": session_complete,
        }
        footer = _encode_block(SESSION_FOOTER_MAGIC, SESSION_FOOTER_BYTES,
                               footer_document)
        try:
            self._stream.seek(0)
            self._write_exact(final_header)
            self._stream.seek(0, 2)
            self._write_exact(footer)
            self._stream.flush()
            self._stream.close()
        except (OSError, ValueError) as exc:
            self._closed = True
            self._write_error = f"cannot finalize session: {exc}"
            raise SessionWriteError(self._write_error) from exc
        self._closed = True
        return inspect_session(self.path)

    def abort(self, reason: str = "FAULT_SAFE", **counters: int) -> SessionInfo:
        return self.close(shutdown_reason=reason, complete_end=False, **counters)

    def __enter__(self) -> "SustainedSessionWriter":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if not self._closed:
            if exc_type is None:
                self.close()
            elif self._stream is not None:
                self._stream.close()
                self._closed = True


def inspect_session(path: str | Path) -> SessionInfo:
    value = Path(path).expanduser().resolve()
    try:
        size = value.stat().st_size
    except OSError as exc:
        raise SessionIntegrityError(f"cannot stat session: {exc}") from exc
    if size < SESSION_HEADER_BYTES + SESSION_FOOTER_BYTES + EEG_FRAME_V1_BYTES:
        raise SessionIntegrityError("session is truncated or missing its footer")
    try:
        with value.open("rb") as stream:
            header_raw = stream.read(SESSION_HEADER_BYTES)
            stream.seek(-SESSION_FOOTER_BYTES, 2)
            footer_raw = stream.read(SESSION_FOOTER_BYTES)
    except OSError as exc:
        raise SessionIntegrityError(f"cannot read session: {exc}") from exc
    header = _decode_block(header_raw, SESSION_HEADER_MAGIC, "session header")
    footer = _decode_block(footer_raw, SESSION_FOOTER_MAGIC, "session footer")
    data_bytes = size - SESSION_HEADER_BYTES - SESSION_FOOTER_BYTES
    if data_bytes % EEG_FRAME_V1_BYTES:
        raise SessionIntegrityError("session data is truncated in the middle of an EegFrameV1")
    record_count = data_bytes // EEG_FRAME_V1_BYTES
    if footer.get("frame_count") != record_count:
        raise SessionIntegrityError("footer frame count does not match the data region")
    if header.get("format") != "eeg-sustained-session-v1":
        raise SessionIntegrityError("unsupported session format")
    if header.get("frame_record_bytes") != EEG_FRAME_V1_BYTES:
        raise SessionIntegrityError("session EegFrameV1 size is incompatible")
    if header.get("channel_count") != EEG_CHANNELS:
        raise SessionIntegrityError("session channel count is incompatible")
    id_gate = header.get("id_gate")
    if not isinstance(id_gate, dict) or id_gate.get("passed") is not True:
        raise SessionIntegrityError("session does not contain a passing ID gate")
    header_hash = hashlib.sha256(header_raw).hexdigest().upper()
    if footer.get("header_sha256") != header_hash:
        raise SessionIntegrityError("session header SHA-256 does not match footer")

    data_hash = hashlib.sha256()
    first_sequence = last_sequence = None
    previous_sequence = previous_timestamp = None
    gaps = missing = duplicates = out_of_order = timestamp_errors = 0
    sample_rate = header.get("sample_rate_millihz")
    if not isinstance(sample_rate, int) or sample_rate <= 0:
        raise SessionIntegrityError("invalid session sample rate")
    period_ns = 1_000_000_000_000 // sample_rate
    try:
        with value.open("rb") as stream:
            stream.seek(SESSION_HEADER_BYTES)
            for index in range(record_count):
                raw = stream.read(EEG_FRAME_V1_BYTES)
                data_hash.update(raw)
                try:
                    frame = EegFrameV1.unpack(raw)
                except ValueError as exc:
                    raise SessionIntegrityError(
                        f"invalid EegFrameV1 at data index {index}: {exc}"
                    ) from exc
                if frame.source is not SourceType.SIMULATED:
                    raise SessionIntegrityError("4G data region must contain SIMULATED frames")
                if frame.sample_rate_millihz != sample_rate:
                    raise SessionIntegrityError("sample rate changes inside session")
                if frame.sequence > SEQUENCE_MASK:
                    raise SessionIntegrityError("producer sequence exceeds 32 bits")
                if first_sequence is None:
                    first_sequence = frame.sequence
                if previous_sequence is not None:
                    relation, missing_count = _sequence_relation(previous_sequence,
                                                                  frame.sequence)
                    if relation == "GAP":
                        gaps += 1
                        missing += missing_count
                    elif relation == "DUPLICATE":
                        duplicates += 1
                    elif relation == "OUT_OF_ORDER":
                        out_of_order += 1
                    if previous_timestamp is not None:
                        if frame.timestamp_ns <= previous_timestamp:
                            timestamp_errors += 1
                        elif relation in {"CONTIGUOUS", "GAP"}:
                            delta = (frame.sequence - previous_sequence) & SEQUENCE_MASK
                            if frame.timestamp_ns != previous_timestamp + delta * period_ns:
                                timestamp_errors += 1
                previous_sequence = last_sequence = frame.sequence
                previous_timestamp = frame.timestamp_ns
    except OSError as exc:
        raise SessionIntegrityError(f"cannot scan session data: {exc}") from exc
    if first_sequence is None or last_sequence is None:
        raise SessionIntegrityError("session contains no records")
    data_hash_value = data_hash.hexdigest().upper()
    if footer.get("data_sha256") != data_hash_value:
        raise SessionIntegrityError("session data SHA-256 does not match footer")
    checks = {
        "first_sequence": first_sequence,
        "last_sequence": last_sequence,
        "recorder_gap_events": gaps,
        "missing_sequence_count": missing,
        "duplicate_count": duplicates,
        "out_of_order_count": out_of_order,
        "timestamp_error_count": timestamp_errors,
    }
    for key, expected in checks.items():
        if footer.get(key) != expected:
            raise SessionIntegrityError(f"footer {key} does not match scanned records")
    required_ints = ("overflow_count", "dropped_count", "partial_pair_count",
                     "sequence_gap_count")
    for key in required_ints:
        if not isinstance(footer.get(key), int) or int(footer[key]) < 0:
            raise SessionIntegrityError(f"footer {key} is invalid")
    session_complete = bool(footer.get("session_complete"))
    calculated_complete = bool(
        footer.get("complete_end") is True
        and footer.get("shutdown_reason") == "SHUTDOWN_SAFE"
        and not any(int(footer[key]) for key in required_ints)
        and not gaps and not duplicates and not out_of_order and not timestamp_errors
    )
    if session_complete != calculated_complete:
        raise SessionIntegrityError("footer session_complete is inconsistent")
    if header.get("first_sequence") != first_sequence:
        raise SessionIntegrityError("header first_sequence does not match data")
    return SessionInfo(
        path=value, session_id=str(header.get("session_id")), frame_count=record_count,
        first_sequence=first_sequence, last_sequence=last_sequence,
        sample_rate_millihz=sample_rate, time_source=str(header.get("time_source")),
        overflow_count=int(footer["overflow_count"]),
        dropped_count=int(footer["dropped_count"]),
        partial_pair_count=int(footer["partial_pair_count"]),
        sequence_gap_count=int(footer["sequence_gap_count"]),
        recorder_gap_events=gaps, missing_sequence_count=missing,
        duplicate_count=duplicates, out_of_order_count=out_of_order,
        timestamp_error_count=timestamp_errors,
        shutdown_reason=str(footer.get("shutdown_reason")),
        complete_end=bool(footer.get("complete_end")),
        session_complete=session_complete, data_sha256=data_hash_value,
        header_sha256=header_hash, file_sha256=_sha256_file(value), file_size=size,
    )


@dataclass(frozen=True, slots=True)
class ReplayProgress:
    current_frame: int
    total_frames: int
    mode: ReplayMode
    current_sequence: int | None

    @property
    def finished(self) -> bool:
        return self.current_frame >= self.total_frames


class SustainedSessionReplay:
    """Validated deterministic replay preserving sequence, time, flags, and samples."""

    def __init__(self, path: str | Path, *, mode: ReplayMode = ReplayMode.ONE_X) -> None:
        self.info = inspect_session(path)
        self.mode = ReplayMode(mode)
        self._stream = self.info.path.open("rb")
        self._stream.seek(SESSION_HEADER_BYTES)
        self._index = 0
        self._current_sequence: int | None = None
        self._closed = False

    @property
    def progress(self) -> ReplayProgress:
        return ReplayProgress(self._index, self.info.frame_count, self.mode,
                              self._current_sequence)

    def set_mode(self, mode: ReplayMode) -> None:
        self.mode = ReplayMode(mode)

    def read_frames(self, max_frames: int) -> tuple[EegFrameV1, ...]:
        if self._closed:
            raise SessionError("replay is closed")
        if not isinstance(max_frames, int) or isinstance(max_frames, bool):
            raise TypeError("max_frames must be an integer")
        if max_frames < 0:
            raise ValueError("max_frames cannot be negative")
        count = min(max_frames, self.info.frame_count - self._index)
        frames: list[EegFrameV1] = []
        for _ in range(count):
            raw = self._stream.read(EEG_FRAME_V1_BYTES)
            if len(raw) != EEG_FRAME_V1_BYTES:
                raise SessionIntegrityError("session changed or was truncated during replay")
            original = EegFrameV1.unpack(raw)
            replay = replace(original, source=SourceType.FILE_REPLAY)
            frames.append(replay)
            self._index += 1
            self._current_sequence = replay.sequence
        return tuple(frames)

    def read_records(self, max_records: int) -> tuple[bytes, ...]:
        return tuple(frame.pack() for frame in self.read_frames(max_records))

    def reset(self) -> None:
        if self._closed:
            raise SessionError("replay is closed")
        self._stream.seek(SESSION_HEADER_BYTES)
        self._index = 0
        self._current_sequence = None

    def seek_sequence(self, sequence: int) -> int:
        if not 0 <= sequence <= SEQUENCE_MASK:
            raise ValueError("sequence must fit uint32")
        self.reset()
        for index in range(self.info.frame_count):
            position = SESSION_HEADER_BYTES + index * EEG_FRAME_V1_BYTES
            raw = self._stream.read(EEG_FRAME_V1_BYTES)
            if EegFrameV1.unpack(raw).sequence == sequence:
                self._stream.seek(position)
                self._index = index
                self._current_sequence = None
                return index
        self.reset()
        raise SessionError(f"sequence {sequence} is not present in the session")

    def close(self) -> None:
        if not self._closed:
            self._stream.close()
            self._closed = True

    def __enter__(self) -> "SustainedSessionReplay":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()


class DeterministicPairSource:
    """Transaction-level deterministic paired ADS source, not a bit-level SPI model."""

    @staticmethod
    def _sample(sequence: int, channel: int, adc: int) -> int:
        raw = (sequence * (17 + adc) + channel * 0x101 + adc * 0x2345) & 0xFF_FFFF
        return raw - (1 << 24) if raw & (1 << 23) else raw

    def pair(self, sequence: int) -> FramePair:
        value = sequence & SEQUENCE_MASK
        return FramePair(
            sequence=value,
            ads1_status=0xC1_0000 | (value & 0xFFFF),
            ads2_status=0xC2_0000 | (value & 0xFFFF),
            ads1_channels=tuple(self._sample(value, channel, 1) for channel in range(8)),
            ads2_channels=tuple(self._sample(value, channel, 2) for channel in range(8)),
        )


class Milestone4GFrameBufferMMIO(FrameBufferFakeMMIO):
    """Thin compatibility name for the unified 4F snapshot contract."""

    pass


class FrameBufferMmioConsumer:
    """Consume the 4F relative-register snapshot/ack protocol through fake-MMIO."""

    def __init__(self, mmio: FrameBufferFakeMMIO, *, sample_rate_millihz: int) -> None:
        self.mmio = mmio
        self.sample_rate_millihz = sample_rate_millihz
        self._last_sequence: int | None = None
        self._last_timestamp: int | None = None

    @staticmethod
    def _signed(value: int) -> int:
        return value - (1 << 32) if value & 0x8000_0000 else value

    def consume_one(self) -> EegFrameV1 | None:
        if self.mmio.controller.status.empty:
            return None
        self.mmio.write32(FrameBufferRegister.CONTROL,
                          int(FrameBufferControl.SNAPSHOT), 0x1)
        sequence = self.mmio.read32(FrameBufferRegister.FRAME_METADATA)
        ads1_status = self.mmio.read32(FrameBufferRegister.FRAME_ADS1_STATUS)
        ads2_status = self.mmio.read32(FrameBufferRegister.FRAME_ADS2_STATUS)
        channels: list[int] = []
        for base in (FrameBufferRegister.FRAME_ADS1_WORD0,
                     FrameBufferRegister.FRAME_ADS2_WORD0):
            channels.extend(
                self._signed(self.mmio.read32(int(base) + 4 * index))
                for index in range(8)
            )
        period_ns = 1_000_000_000_000 // self.sample_rate_millihz
        if self._last_sequence is None:
            timestamp_ns = 0
        else:
            relation, _ = _sequence_relation(self._last_sequence, sequence)
            if relation in {"CONTIGUOUS", "GAP"}:
                delta = (sequence - self._last_sequence) & SEQUENCE_MASK
                timestamp_ns = int(self._last_timestamp or 0) + delta * period_ns
            else:
                timestamp_ns = int(self._last_timestamp or 0) + period_ns
        frame = EegFrameV1(
            sequence=sequence, timestamp_ns=timestamp_ns,
            sample_rate_millihz=self.sample_rate_millihz,
            source=SourceType.SIMULATED, adc_present_mask=0x03,
            flags=ErrorFlag.NONE, adc1_status=ads1_status,
            adc2_status=ads2_status, samples=tuple(channels),
        )
        self.mmio.write32(FrameBufferRegister.CONTROL,
                          int(FrameBufferControl.CONSUME_ACK), 0x1)
        self._last_sequence = sequence
        self._last_timestamp = timestamp_ns
        return frame


@dataclass(frozen=True, slots=True)
class StreamSimulationResult:
    produced_frames: int
    consumed_frames: int
    written_frames: int
    replayed_frames: int
    overflow_count: int
    dropped_count: int
    partial_pair_count: int
    sequence_gap_count: int
    file_size: int
    file_sha256: str
    session_complete: bool
    shutdown_safe: bool


def run_transaction_stream(
    path: str | Path, *, frame_count: int, consumer_delay_pattern: tuple[int, ...] = (0,),
    pause_start: int | None = None, pause_frames: int = 0,
    start_sequence: int = 0,
    ads1_ids: tuple[int, int, int] = (0x3E, 0x3E, 0x3E),
    ads2_ids: tuple[int, int, int] = (0x3E, 0x3E, 0x3E),
    session_id: str = "M4G-DETERMINISTIC-SESSION",
) -> StreamSimulationResult:
    if frame_count <= 0:
        raise ValueError("frame_count must be positive")
    if not consumer_delay_pattern or any(value < 0 for value in consumer_delay_pattern):
        raise ValueError("consumer delay pattern must contain non-negative integers")
    if not id_triplets_pass(ads1_ids, ads2_ids):
        raise SessionWriteError("ID gate failed; FIFO and normal session remain blocked")
    fifo = FrameBufferController(capacity=DEFAULT_FIFO_DEPTH)
    mmio = Milestone4GFrameBufferMMIO(fifo)
    consumer = FrameBufferMmioConsumer(mmio,
                                       sample_rate_millihz=DEFAULT_SAMPLE_RATE_MILLIHZ)
    source = DeterministicPairSource()
    consumed = written = 0
    delay_budget = 0
    writer = SustainedSessionWriter(
        path, session_id=session_id, ads1_ids=ads1_ids, ads2_ids=ads2_ids
    )
    try:
        for index in range(frame_count):
            sequence = (start_sequence + index) & SEQUENCE_MASK
            fifo.push_pair(source.pair(sequence), id_gate_passed=True)
            paused = (pause_start is not None and pause_start <= index
                      < pause_start + pause_frames)
            delay_budget += consumer_delay_pattern[index % len(consumer_delay_pattern)]
            budget = 0 if paused or delay_budget else 1
            if not paused and delay_budget:
                delay_budget -= 1
            if not paused and not delay_budget and fifo.status.occupancy > 1:
                budget = 2
            for _ in range(budget):
                frame = consumer.consume_one()
                if frame is None:
                    break
                consumed += 1
                writer.append_frame(frame)
                written += 1
        fifo.request_shutdown()
        while not fifo.status.empty:
            frame = consumer.consume_one()
            if frame is None:
                break
            consumed += 1
            writer.append_frame(frame)
            written += 1
        info = writer.close(
            overflow_count=fifo.status.overflow_count,
            dropped_count=fifo.status.dropped_count,
            partial_pair_count=fifo.status.partial_pair_count,
            sequence_gap_count=fifo.status.sequence_gap_count,
        )
    except Exception:
        if not writer._closed and writer._stream is not None:
            writer._stream.close()
            writer._closed = True
        raise
    replayed = 0
    with SustainedSessionReplay(info.path, mode=ReplayMode.ACCELERATED) as replay:
        while batch := replay.read_frames(4096):
            replayed += len(batch)
    return StreamSimulationResult(
        produced_frames=frame_count, consumed_frames=consumed,
        written_frames=written, replayed_frames=replayed,
        overflow_count=fifo.status.overflow_count,
        dropped_count=fifo.status.dropped_count,
        partial_pair_count=fifo.status.partial_pair_count,
        sequence_gap_count=fifo.status.sequence_gap_count,
        file_size=info.file_size, file_sha256=info.file_sha256,
        session_complete=info.session_complete,
        shutdown_safe=fifo.status.shutdown_safe,
    )


class ReplayDspAdapter:
    """Run replay through existing DSP while treating uint32 wrap as contiguous."""

    def __init__(self, processor: DspProcessor | None = None) -> None:
        self.processor = processor or DspProcessor()
        self._last_sequence: int | None = None
        self._epoch = 0

    def process(self, frame: EegFrameV1) -> FilteredFrame:
        sequence = frame.sequence & SEQUENCE_MASK
        if self._last_sequence == SEQUENCE_MASK and sequence == 0:
            self._epoch += 1
        extended = (self._epoch << 32) | sequence
        result = self.processor.process(replace(frame, sequence=extended))
        self._last_sequence = sequence
        return replace(result, sequence=sequence)


def replay_through_dsp(path: str | Path) -> tuple[FilteredFrame, ...]:
    adapter = ReplayDspAdapter()
    output: list[FilteredFrame] = []
    with SustainedSessionReplay(path, mode=ReplayMode.ACCELERATED) as replay:
        while frames := replay.read_frames(1024):
            output.extend(adapter.process(frame) for frame in frames)
    return tuple(output)
