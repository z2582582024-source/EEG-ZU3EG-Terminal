"""Validated continuous recording metadata and non-blocking file replay."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO

from .electrodes import BIAS_ELECTRODE, ELECTRODE_CHANNELS, REFERENCE_ELECTRODE
from .frame import EEG_FRAME_V1_BYTES, EEG_FRAME_VERSION, EegFrameV1, SourceType
from . import dma_event08_import
from .internal_test_import import (
    DESIGN_SAMPLE_RATE_HZ,
    EVENT_ID,
    IMPORT_SCHEMA,
    ImportValidationError,
    validate_import_metadata,
)

RECORDING_METADATA_SCHEMA = "eeg-recording-metadata-v1"
ELECTRODE_MAPPING_SCHEMA = "eeg-electrode-mapping-v1"
RECORDING_CLASSIFICATION = "software recording; not hardware capture or validation"
FRAME_BYTE_ORDER = "little-endian project record"
INTEGRITY_SCOPE = "entire eegbin file in byte order"


class RecordingError(ValueError):
    """Base class for recording creation, validation, or I/O failures."""


class RecordingCreateError(RecordingError):
    """A new recording file could not be created safely."""


class RecordingWriteError(RecordingError):
    """A validated record could not be written or finalized."""


class ReplayLoadError(RecordingError):
    """A recording cannot be safely loaded for replay."""


def metadata_path_for(data_path: str | Path) -> Path:
    path = Path(data_path)
    return path.with_name(path.name + ".json")


def _channel_order_document() -> list[dict[str, int | str]]:
    return [
        {
            "logical_index": channel.logical_index,
            "logical_channel": channel.logical_channel,
            "electrode": channel.electrode,
            "adc_index": channel.adc_index,
            "adc_input_index": channel.adc_input_index,
            "adc_input": channel.adc_input,
            "cap_lead": channel.cap_lead,
            "board_input": channel.board_input,
        }
        for channel in ELECTRODE_CHANNELS
    ]


def _electrode_mapping_document() -> dict[str, object]:
    return {
        "schema": ELECTRODE_MAPPING_SCHEMA,
        "status": "frozen-user-confirmed",
        "channels": _channel_order_document(),
        "reference": {
            "electrode": REFERENCE_ELECTRODE.electrode,
            "role": REFERENCE_ELECTRODE.role,
        },
        "bias": {
            "electrode": BIAS_ELECTRODE.electrode,
            "role": BIAS_ELECTRODE.role,
            "system_ground_connection": BIAS_ELECTRODE.system_ground_connection,
        },
    }


@dataclass(frozen=True, slots=True)
class RecordingInfo:
    data_path: Path
    metadata_path: Path | None
    record_count: int
    sample_rate_millihz: int
    original_sources: tuple[SourceType, ...]
    data_sha256: str
    metadata_present: bool
    import_event_id: str | None = None
    design_sample_rate_hz: float | None = None

    @property
    def is_board_internal_test(self) -> bool:
        return self.import_event_id in (EVENT_ID, dma_event08_import.EVENT_ID)

    @property
    def is_dma_internal_test(self) -> bool:
        return self.import_event_id == dma_event08_import.EVENT_ID

    @property
    def sample_rate_hz(self) -> float:
        return self.design_sample_rate_hz or self.sample_rate_millihz / 1000.0

    @property
    def duration_seconds(self) -> float:
        return self.record_count / self.sample_rate_hz

    @property
    def original_source_names(self) -> tuple[str, ...]:
        return tuple(source.name for source in self.original_sources)


@dataclass(frozen=True, slots=True)
class RecordingStatus:
    active: bool
    data_path: Path
    record_count: int
    sample_rate_millihz: int | None
    write_error: str | None

    @property
    def duration_seconds(self) -> float:
        if self.sample_rate_millihz is None:
            return 0.0
        return self.record_count / (self.sample_rate_millihz / 1000.0)


class ContinuousRecordWriter:
    """Append fully validated EegFrameV1 records and finalize a JSON sidecar.

    Files are opened in exclusive-create mode so existing user recordings are
    never overwritten.  Milestone 3B only authorizes recording SIMULATED data.
    """

    def __init__(
        self,
        data_path: str | Path,
        *,
        stream_factory: Callable[[Path], BinaryIO] | None = None,
    ) -> None:
        self.data_path = Path(data_path).expanduser().resolve()
        self.metadata_path = metadata_path_for(self.data_path)
        if self.data_path == self.metadata_path:
            raise RecordingCreateError("data and metadata paths must be different")
        if self.metadata_path.exists():
            raise RecordingCreateError(f"metadata file already exists: {self.metadata_path}")
        self._stream_factory = stream_factory or (lambda path: path.open("xb"))
        try:
            self._stream = self._stream_factory(self.data_path)
        except (OSError, ValueError) as exc:
            raise RecordingCreateError(f"cannot create recording file {self.data_path}: {exc}") from exc
        self._hash = hashlib.sha256()
        self._record_count = 0
        self._sample_rate_millihz: int | None = None
        self._sources: set[SourceType] = set()
        self._created_utc = datetime.now(timezone.utc)
        self._closed = False
        self._write_error: str | None = None

    @property
    def status(self) -> RecordingStatus:
        return RecordingStatus(
            active=not self._closed,
            data_path=self.data_path,
            record_count=self._record_count,
            sample_rate_millihz=self._sample_rate_millihz,
            write_error=self._write_error,
        )

    def append_record(self, record: bytes | bytearray | memoryview) -> None:
        if self._closed:
            raise RecordingWriteError("recording is already closed")
        data = bytes(record)
        try:
            frame = EegFrameV1.unpack(data)
        except ValueError as exc:
            self._fail(f"record {self._record_count} is not a valid EegFrameV1: {exc}")
        if frame.source is not SourceType.SIMULATED:
            self._fail(
                "milestone 3B recording accepts SIMULATED frames only; "
                f"received {frame.source.name}"
            )
        if self._sample_rate_millihz is None:
            self._sample_rate_millihz = frame.sample_rate_millihz
        elif frame.sample_rate_millihz != self._sample_rate_millihz:
            self._fail(
                "sample rate changed during recording: "
                f"{frame.sample_rate_millihz} != {self._sample_rate_millihz} mHz"
            )
        try:
            written = self._stream.write(data)
            if written != len(data):
                raise OSError(f"short write: {written!r} of {len(data)} bytes")
        except (OSError, ValueError) as exc:
            self._fail(f"write failed at record {self._record_count}: {exc}")
        self._hash.update(data)
        self._sources.add(frame.source)
        self._record_count += 1

    def _fail(self, message: str) -> None:
        self._write_error = message
        try:
            self._stream.close()
        finally:
            self._closed = True
        raise RecordingWriteError(message)

    def close(self) -> RecordingInfo:
        if self._closed:
            if self._write_error is not None:
                raise RecordingWriteError(self._write_error)
            raise RecordingWriteError("recording is already closed")
        try:
            self._stream.flush()
            self._stream.close()
        except (OSError, ValueError) as exc:
            self._closed = True
            self._write_error = f"cannot close recording file: {exc}"
            raise RecordingWriteError(self._write_error) from exc
        self._closed = True
        if self._record_count == 0 or self._sample_rate_millihz is None:
            self._write_error = "recording contains no frames and cannot be finalized"
            raise RecordingWriteError(self._write_error)

        metadata = self._metadata_document()
        try:
            with self.metadata_path.open("x", encoding="utf-8", newline="\n") as stream:
                json.dump(metadata, stream, ensure_ascii=False, indent=2, sort_keys=True)
                stream.write("\n")
        except (OSError, ValueError) as exc:
            self._write_error = f"cannot write metadata file {self.metadata_path}: {exc}"
            raise RecordingWriteError(self._write_error) from exc
        return RecordingInfo(
            data_path=self.data_path,
            metadata_path=self.metadata_path,
            record_count=self._record_count,
            sample_rate_millihz=self._sample_rate_millihz,
            original_sources=tuple(sorted(self._sources, key=int)),
            data_sha256=self._hash.hexdigest(),
            metadata_present=True,
        )

    def _metadata_document(self) -> dict[str, object]:
        assert self._sample_rate_millihz is not None
        source_names = [source.name for source in sorted(self._sources, key=int)]
        return {
            "schema": RECORDING_METADATA_SCHEMA,
            "classification": RECORDING_CLASSIFICATION,
            "frame_format": {
                "name": "EegFrameV1",
                "version": EEG_FRAME_VERSION,
                "record_bytes": EEG_FRAME_V1_BYTES,
                "byte_order": FRAME_BYTE_ORDER,
            },
            "sample_rate_millihz": self._sample_rate_millihz,
            "record_count": self._record_count,
            "duration_seconds": self._record_count
            / (self._sample_rate_millihz / 1000.0),
            "original_sources": source_names,
            "current_replay_source": SourceType.FILE_REPLAY.name,
            "electrode_mapping": _electrode_mapping_document(),
            "integrity": {
                "algorithm": "SHA-256",
                "scope": INTEGRITY_SCOPE,
                "data_sha256": self._hash.hexdigest(),
            },
            "created_utc": self._created_utc.isoformat(),
            "finalized_utc": datetime.now(timezone.utc).isoformat(),
        }

    def __enter__(self) -> "ContinuousRecordWriter":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if not self._closed:
            if exc_type is None:
                self.close()
            else:
                self._stream.close()
                self._closed = True


def _load_sidecar(path: Path) -> dict[str, object] | None:
    metadata_path = metadata_path_for(path)
    if not metadata_path.exists():
        return None
    try:
        document = json.loads(
            metadata_path.read_text(encoding="utf-8"), object_pairs_hook=_unique_json_object
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReplayLoadError(f"invalid metadata file {metadata_path}: {exc}") from exc
    if not isinstance(document, dict):
        raise ReplayLoadError("recording metadata root must be a JSON object")
    return document


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    document: dict[str, object] = {}
    for key, value in pairs:
        if key in document:
            raise ReplayLoadError(f"duplicate metadata key: {key}")
        document[key] = value
    return document


def inspect_recording(data_path: str | Path) -> RecordingInfo:
    """Fully validate a raw fixed-record file and any matching metadata sidecar."""

    path = Path(data_path).expanduser().resolve()
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ReplayLoadError(f"cannot open recording file {path}: {exc}") from exc
    if size == 0:
        raise ReplayLoadError("recording file is empty")
    if size % EEG_FRAME_V1_BYTES:
        full_records, remainder = divmod(size, EEG_FRAME_V1_BYTES)
        raise ReplayLoadError(
            f"recording length {size} is not a multiple of {EEG_FRAME_V1_BYTES}; "
            f"{full_records} complete records and {remainder} trailing bytes"
        )

    metadata = _load_sidecar(path)
    dma_imported = metadata is not None and metadata.get("schema") == dma_event08_import.IMPORT_SCHEMA
    imported = metadata is not None and metadata.get("schema") in (IMPORT_SCHEMA, dma_event08_import.IMPORT_SCHEMA)
    import_frames: list[EegFrameV1] = []
    digest = hashlib.sha256()
    sources: set[SourceType] = set()
    sample_rate_millihz: int | None = None
    record_count = 0
    try:
        with path.open("rb") as stream:
            while record := stream.read(EEG_FRAME_V1_BYTES):
                digest.update(record)
                try:
                    frame = EegFrameV1.unpack(record)
                except ValueError as exc:
                    raise ReplayLoadError(f"invalid EegFrameV1 at record {record_count}: {exc}") from exc
                if frame.source is SourceType.FILE_REPLAY and not imported:
                    raise ReplayLoadError("stored FILE_REPLAY frames require validated import provenance metadata")
                if imported:
                    import_frames.append(frame)
                if sample_rate_millihz is None:
                    sample_rate_millihz = frame.sample_rate_millihz
                elif frame.sample_rate_millihz != sample_rate_millihz:
                    raise ReplayLoadError(
                        f"sample rate changes at record {record_count}: "
                        f"{frame.sample_rate_millihz} != {sample_rate_millihz} mHz"
                    )
                sources.add(frame.source)
                record_count += 1
    except OSError as exc:
        raise ReplayLoadError(f"cannot read recording file {path}: {exc}") from exc
    assert sample_rate_millihz is not None

    if imported:
        try:
            validator = dma_event08_import.validate_import_metadata if dma_imported else validate_import_metadata
            validator(
                metadata, import_frames, digest.hexdigest(), _electrode_mapping_document()
            )
        except (ImportValidationError, TypeError, ValueError) as exc:
            raise ReplayLoadError(f"invalid board-test import: {exc}") from exc
    elif metadata is not None:
        _validate_metadata(
            metadata,
            record_count=record_count,
            sample_rate_millihz=sample_rate_millihz,
            sources=sources,
            data_sha256=digest.hexdigest(),
        )
    return RecordingInfo(
        data_path=path,
        metadata_path=metadata_path_for(path) if metadata is not None else None,
        record_count=record_count,
        sample_rate_millihz=sample_rate_millihz,
        original_sources=tuple(sorted(sources, key=int)),
        data_sha256=digest.hexdigest(),
        metadata_present=metadata is not None,
        import_event_id=(dma_event08_import.EVENT_ID if dma_imported else EVENT_ID) if imported else None,
        design_sample_rate_hz=DESIGN_SAMPLE_RATE_HZ if imported else None,
    )


def _validate_metadata(
    document: dict[str, object],
    *,
    record_count: int,
    sample_rate_millihz: int,
    sources: set[SourceType],
    data_sha256: str,
) -> None:
    try:
        if document["schema"] != RECORDING_METADATA_SCHEMA:
            raise ReplayLoadError(f"unsupported metadata schema: {document['schema']!r}")
        if document["classification"] != RECORDING_CLASSIFICATION:
            raise ReplayLoadError(
                "metadata classification must identify a software recording, "
                "not hardware validation"
            )
        frame_format = document["frame_format"]
        if not isinstance(frame_format, dict):
            raise ReplayLoadError("frame_format metadata must be an object")
        expected_frame_format = {
            "name": "EegFrameV1",
            "version": EEG_FRAME_VERSION,
            "record_bytes": EEG_FRAME_V1_BYTES,
        }
        for key, expected in expected_frame_format.items():
            if frame_format.get(key) != expected:
                raise ReplayLoadError(
                    f"metadata frame_format.{key} is {frame_format.get(key)!r}, expected {expected!r}"
                )
        if frame_format.get("byte_order") != FRAME_BYTE_ORDER:
            raise ReplayLoadError(
                f"metadata frame_format.byte_order is {frame_format.get('byte_order')!r}, "
                f"expected {FRAME_BYTE_ORDER!r}"
            )
        metadata_record_count = document["record_count"]
        if not isinstance(metadata_record_count, int) or isinstance(metadata_record_count, bool):
            raise ReplayLoadError("metadata record_count must be an integer")
        if metadata_record_count != record_count:
            raise ReplayLoadError(
                f"metadata record_count is {metadata_record_count!r}, actual is {record_count}"
            )
        metadata_sample_rate = document["sample_rate_millihz"]
        if not isinstance(metadata_sample_rate, int) or isinstance(metadata_sample_rate, bool):
            raise ReplayLoadError("metadata sample_rate_millihz must be an integer")
        if metadata_sample_rate != sample_rate_millihz:
            raise ReplayLoadError(
                "metadata sample_rate_millihz does not match the records"
            )
        duration = document["duration_seconds"]
        expected_duration = record_count / (sample_rate_millihz / 1000.0)
        if (
            not isinstance(duration, (int, float))
            or isinstance(duration, bool)
            or not math.isfinite(duration)
            or not math.isclose(duration, expected_duration, rel_tol=1e-12, abs_tol=1e-12)
        ):
            raise ReplayLoadError("metadata duration_seconds does not match record count and rate")
        source_names = document["original_sources"]
        expected_sources = [source.name for source in sorted(sources, key=int)]
        if not isinstance(source_names, list) or any(not isinstance(name, str) for name in source_names):
            raise ReplayLoadError("metadata original_sources must be a string list")
        if source_names != expected_sources:
            raise ReplayLoadError(
                f"metadata original_sources is {source_names!r}, expected {expected_sources!r}"
            )
        if document.get("current_replay_source") != SourceType.FILE_REPLAY.name:
            raise ReplayLoadError("metadata current_replay_source must be FILE_REPLAY")
        mapping = document["electrode_mapping"]
        if not isinstance(mapping, dict):
            raise ReplayLoadError("electrode_mapping metadata must be an object")
        if mapping != _electrode_mapping_document():
            raise ReplayLoadError("fixed 16-channel electrode/REF/BIAS mapping does not match the project")
        integrity = document["integrity"]
        if not isinstance(integrity, dict) or integrity.get("algorithm") != "SHA-256":
            raise ReplayLoadError("metadata integrity algorithm must be SHA-256")
        if integrity.get("scope") != INTEGRITY_SCOPE:
            raise ReplayLoadError("metadata integrity scope does not cover the entire eegbin file")
        if integrity.get("data_sha256") != data_sha256:
            raise ReplayLoadError("recording SHA-256 does not match metadata")
    except KeyError as exc:
        raise ReplayLoadError(f"recording metadata is missing required field: {exc.args[0]}") from exc


@dataclass(frozen=True, slots=True)
class ReplayProgress:
    current_record: int
    total_records: int
    sample_rate_hz: float
    finished: bool

    @property
    def elapsed_seconds(self) -> float:
        return self.current_record / self.sample_rate_hz

    @property
    def total_seconds(self) -> float:
        return self.total_records / self.sample_rate_hz

    @property
    def percent(self) -> float:
        return 100.0 * self.current_record / self.total_records


class FileReplaySource:
    """Non-blocking fixed-record source that always emits FILE_REPLAY frames."""

    def __init__(self, data_path: str | Path) -> None:
        self.info = inspect_recording(data_path)
        try:
            self._stream = self.info.data_path.open("rb")
        except OSError as exc:
            raise ReplayLoadError(f"cannot open recording for replay: {exc}") from exc
        self._current_record = 0
        self._closed = False

    @property
    def source_type(self) -> SourceType:
        return SourceType.FILE_REPLAY

    @property
    def display_name(self) -> str:
        if self.info.is_dma_internal_test:
            return "真实DMA板测内部测试信号 · 非实时、非真人脑电"
        if self.info.is_board_internal_test:
            return "真实板测内部测试信号 · 非实时、非真人脑电"
        return f"文件回放：{self.info.data_path.name}"

    @property
    def is_real_hardware(self) -> bool:
        return False

    @property
    def sample_rate_hz(self) -> float:
        return self.info.sample_rate_hz

    @property
    def progress(self) -> ReplayProgress:
        return ReplayProgress(
            current_record=self._current_record,
            total_records=self.info.record_count,
            sample_rate_hz=self.info.sample_rate_hz,
            finished=self._current_record >= self.info.record_count,
        )

    def read_records(self, max_records: int) -> tuple[bytes, ...]:
        if self._closed:
            raise ReplayLoadError("replay source is closed")
        if not isinstance(max_records, int) or isinstance(max_records, bool):
            raise TypeError("max_records must be an integer")
        if max_records < 0:
            raise ValueError("max_records cannot be negative")
        remaining = self.info.record_count - self._current_record
        count = min(max_records, remaining)
        records: list[bytes] = []
        for _ in range(count):
            try:
                raw = self._stream.read(EEG_FRAME_V1_BYTES)
            except OSError as exc:
                raise ReplayLoadError(f"cannot read record {self._current_record}: {exc}") from exc
            if len(raw) != EEG_FRAME_V1_BYTES:
                raise ReplayLoadError(
                    f"recording changed or became truncated at record {self._current_record}"
                )
            try:
                original = EegFrameV1.unpack(raw)
            except ValueError as exc:
                raise ReplayLoadError(f"invalid record {self._current_record} during replay: {exc}") from exc
            replay = EegFrameV1(
                sequence=original.sequence,
                timestamp_ns=original.timestamp_ns,
                sample_rate_millihz=original.sample_rate_millihz,
                source=SourceType.FILE_REPLAY,
                adc_present_mask=original.adc_present_mask,
                flags=original.flags,
                adc1_status=original.adc1_status,
                adc2_status=original.adc2_status,
                samples=original.samples,
            )
            records.append(replay.pack())
            self._current_record += 1
        return tuple(records)

    def reset(self) -> None:
        if self._closed:
            raise ReplayLoadError("replay source is closed")
        self._stream.seek(0)
        self._current_record = 0

    def close(self) -> None:
        if not self._closed:
            self._stream.close()
            self._closed = True

    def __enter__(self) -> "FileReplaySource":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()
