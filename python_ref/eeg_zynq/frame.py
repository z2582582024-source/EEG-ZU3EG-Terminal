"""Versioned 16-channel project record format.

This little-endian record is a software/file/DMA contract.  It must not be
confused with the ADS1299's native 27-byte MSB-first wire frame.
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass
from enum import IntEnum, IntFlag

from .ads1299 import Ads1299Frame

EEG_CHANNELS = 16
EEG_FRAME_MAGIC = b"EEG1"
EEG_FRAME_VERSION = 1

# Offsets are documented in docs/data_format_v1.md.
_PAYLOAD_STRUCT = struct.Struct("<4sHHQQIBBHIII16i")
_CRC_STRUCT = struct.Struct("<I")
EEG_FRAME_V1_BYTES = _PAYLOAD_STRUCT.size + _CRC_STRUCT.size


class SourceType(IntEnum):
    """Source provenance shown verbatim by later UI/data consumers."""

    SIMULATED = 1
    FILE_REPLAY = 2
    ADS1299_DMA = 3


class ErrorFlag(IntFlag):
    NONE = 0
    ADC1_STATUS_HEADER = 1 << 0
    ADC2_STATUS_HEADER = 1 << 1
    RAW_LENGTH = 1 << 2
    RECORD_CRC = 1 << 3
    SEQUENCE_GAP = 1 << 4
    DUPLICATE = 1 << 5
    OUT_OF_ORDER = 1 << 6
    ADC1_TIMEOUT = 1 << 8
    ADC2_TIMEOUT = 1 << 9
    SYNC_SKEW = 1 << 10
    SIMULATED_FAULT = 1 << 31


class RecordFormatError(ValueError):
    """Base class for malformed EegFrameV1 records."""


class RecordLengthError(RecordFormatError):
    """The record size is not exactly 112 bytes."""


class RecordCrcError(RecordFormatError):
    """The record-level CRC32 does not match."""


@dataclass(frozen=True, slots=True)
class EegFrameV1:
    """One paired 16-channel sample and its acquisition metadata."""

    sequence: int
    timestamp_ns: int
    sample_rate_millihz: int
    source: SourceType
    adc_present_mask: int
    flags: ErrorFlag
    adc1_status: int
    adc2_status: int
    samples: tuple[int, ...]

    def __post_init__(self) -> None:
        if not 0 <= self.sequence <= 0xFFFFFFFFFFFFFFFF:
            raise ValueError("sequence must fit uint64")
        if not 0 <= self.timestamp_ns <= 0xFFFFFFFFFFFFFFFF:
            raise ValueError("timestamp_ns must fit uint64")
        if not 1 <= self.sample_rate_millihz <= 0xFFFFFFFF:
            raise ValueError("sample_rate_millihz must be a positive uint32")
        if not isinstance(self.source, SourceType):
            raise TypeError("source must be a SourceType")
        if not 0 <= self.adc_present_mask <= 0x03:
            raise ValueError("adc_present_mask only defines bit 0 (ADC1) and bit 1 (ADC2)")
        if not isinstance(self.flags, ErrorFlag):
            raise TypeError("flags must be an ErrorFlag")
        if not 0 <= self.adc1_status <= 0xFFFFFF:
            raise ValueError("adc1_status must fit 24 bits")
        if not 0 <= self.adc2_status <= 0xFFFFFF:
            raise ValueError("adc2_status must fit 24 bits")
        if len(self.samples) != EEG_CHANNELS:
            raise ValueError(f"expected {EEG_CHANNELS} samples, got {len(self.samples)}")
        for sample in self.samples:
            if not -(1 << 31) <= sample <= (1 << 31) - 1:
                raise ValueError(f"sample does not fit int32: {sample}")
        if not self.adc_present_mask & 0x01 and any(self.samples[0:8]):
            raise ValueError("ADC1-absent records must zero-fill CH01..CH08")
        if not self.adc_present_mask & 0x02 and any(self.samples[8:16]):
            raise ValueError("ADC2-absent records must zero-fill CH09..CH16")

    def pack(self) -> bytes:
        payload = _PAYLOAD_STRUCT.pack(
            EEG_FRAME_MAGIC,
            EEG_FRAME_VERSION,
            EEG_FRAME_V1_BYTES,
            self.sequence,
            self.timestamp_ns,
            self.sample_rate_millihz,
            int(self.source),
            self.adc_present_mask,
            EEG_CHANNELS,
            int(self.flags),
            self.adc1_status,
            self.adc2_status,
            *self.samples,
        )
        return payload + _CRC_STRUCT.pack(zlib.crc32(payload) & 0xFFFFFFFF)

    @classmethod
    def unpack(cls, record: bytes | bytearray | memoryview) -> "EegFrameV1":
        data = bytes(record)
        if len(data) != EEG_FRAME_V1_BYTES:
            raise RecordLengthError(
                f"EegFrameV1 must be {EEG_FRAME_V1_BYTES} bytes, got {len(data)}"
            )

        payload = data[:-_CRC_STRUCT.size]
        stored_crc = _CRC_STRUCT.unpack(data[-_CRC_STRUCT.size :])[0]
        calculated_crc = zlib.crc32(payload) & 0xFFFFFFFF
        if stored_crc != calculated_crc:
            raise RecordCrcError(
                f"CRC32 mismatch: stored=0x{stored_crc:08X}, calculated=0x{calculated_crc:08X}"
            )

        unpacked = _PAYLOAD_STRUCT.unpack(payload)
        magic, version, record_size = unpacked[0:3]
        if magic != EEG_FRAME_MAGIC:
            raise RecordFormatError(f"invalid magic: {magic!r}")
        if version != EEG_FRAME_VERSION:
            raise RecordFormatError(f"unsupported record version: {version}")
        if record_size != EEG_FRAME_V1_BYTES:
            raise RecordFormatError(f"record_size field is {record_size}, expected {EEG_FRAME_V1_BYTES}")

        channel_count = unpacked[8]
        if channel_count != EEG_CHANNELS:
            raise RecordFormatError(f"channel_count is {channel_count}, expected {EEG_CHANNELS}")
        try:
            source = SourceType(unpacked[6])
        except ValueError as exc:
            raise RecordFormatError(f"unknown source type: {unpacked[6]}") from exc

        try:
            flags = ErrorFlag(unpacked[9])
            return cls(
                sequence=unpacked[3],
                timestamp_ns=unpacked[4],
                sample_rate_millihz=unpacked[5],
                source=source,
                adc_present_mask=unpacked[7],
                flags=flags,
                adc1_status=unpacked[10],
                adc2_status=unpacked[11],
                samples=tuple(unpacked[12:]),
            )
        except (TypeError, ValueError) as exc:
            raise RecordFormatError(str(exc)) from exc


def combine_ads1299_frames(
    adc1: Ads1299Frame | None,
    adc2: Ads1299Frame | None,
    *,
    sequence: int,
    timestamp_ns: int,
    sample_rate_millihz: int,
    source: SourceType,
    extra_flags: ErrorFlag = ErrorFlag.NONE,
) -> EegFrameV1:
    """Pair two ADC frames using the fixed logical-channel mapping.

    ADC1 CH1..CH8 map to logical CH01..CH08 and ADC2 CH1..CH8 map to
    logical CH09..CH16.  Missing ADC payloads are explicitly zero-filled and
    marked by both ``adc_present_mask`` and a timeout flag.
    """

    flags = extra_flags
    present_mask = 0

    if adc1 is None:
        adc1_samples = (0,) * 8
        adc1_status = 0
        flags |= ErrorFlag.ADC1_TIMEOUT
    else:
        present_mask |= 0x01
        adc1_samples = adc1.channels
        adc1_status = adc1.status
        if not adc1.status_header_valid:
            flags |= ErrorFlag.ADC1_STATUS_HEADER

    if adc2 is None:
        adc2_samples = (0,) * 8
        adc2_status = 0
        flags |= ErrorFlag.ADC2_TIMEOUT
    else:
        present_mask |= 0x02
        adc2_samples = adc2.channels
        adc2_status = adc2.status
        if not adc2.status_header_valid:
            flags |= ErrorFlag.ADC2_STATUS_HEADER

    return EegFrameV1(
        sequence=sequence,
        timestamp_ns=timestamp_ns,
        sample_rate_millihz=sample_rate_millihz,
        source=source,
        adc_present_mask=present_mask,
        flags=flags,
        adc1_status=adc1_status,
        adc2_status=adc2_status,
        samples=tuple(adc1_samples + adc2_samples),
    )
