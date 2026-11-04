"""Simple fixed-record file I/O reserved for replay and later Qt adapters."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from pathlib import Path

from .frame import EEG_FRAME_V1_BYTES, EegFrameV1, SourceType


def write_record_file(path: str | Path, records: Iterable[bytes]) -> int:
    """Write concatenated EegFrameV1 records and return the record count."""

    count = 0
    with Path(path).open("wb") as stream:
        for record in records:
            if len(record) != EEG_FRAME_V1_BYTES:
                raise ValueError(f"record {count} has invalid length {len(record)}")
            data = bytes(record)
            try:
                EegFrameV1.unpack(data)
            except ValueError as exc:
                raise ValueError(f"record {count} is not a valid EegFrameV1: {exc}") from exc
            stream.write(data)
            count += 1
    return count


def read_record_file(path: str | Path, *, mark_as_replay: bool = True) -> Iterator[EegFrameV1]:
    """Read a fixed-record file, rejecting a truncated final record.

    By default provenance is changed to FILE_REPLAY and the CRC is regenerated
    if the caller packs the returned object.  The original source remains in
    the on-disk record until this explicit read conversion.
    """

    with Path(path).open("rb") as stream:
        record_index = 0
        while True:
            record = stream.read(EEG_FRAME_V1_BYTES)
            if not record:
                return
            if len(record) != EEG_FRAME_V1_BYTES:
                raise ValueError(
                    f"truncated record {record_index}: expected {EEG_FRAME_V1_BYTES} bytes, got {len(record)}"
                )
            frame = EegFrameV1.unpack(record)
            if mark_as_replay:
                frame = EegFrameV1(
                    sequence=frame.sequence,
                    timestamp_ns=frame.timestamp_ns,
                    sample_rate_millihz=frame.sample_rate_millihz,
                    source=SourceType.FILE_REPLAY,
                    adc_present_mask=frame.adc_present_mask,
                    flags=frame.flags,
                    adc1_status=frame.adc1_status,
                    adc2_status=frame.adc2_status,
                    samples=frame.samples,
                )
            yield frame
            record_index += 1
