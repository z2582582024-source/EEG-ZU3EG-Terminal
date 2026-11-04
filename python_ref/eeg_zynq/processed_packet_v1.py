"""Strict EDP1 processed-data packet and offline replay adapter.

EDP1 carries byte-identical EMD1 raw records, Q2.30-derived filtered samples,
and an optional 129-bin selected-channel FFT result. It is a replay source,
never a live DMA claim. The format is a candidate ABI, not a deployed driver.
"""
from __future__ import annotations

import math
import os
import struct
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from .dsp import Spectrum, SpectrumBatch
from .frame import EEG_CHANNELS, EegFrameV1, ErrorFlag, SourceType
from .recording import ReplayLoadError

MAGIC = b"EDP1"
VERSION = 1
HEADER = struct.Struct("<4sHH14I")
RAW_RECORD = struct.Struct("<6I16i2I")
FILTER_RECORD = struct.Struct("<IHH16i")
SPECTRUM_BIN = struct.Struct("<iiQ")
HEADER_BYTES = 64
CHUNK_FRAMES = 32
SAMPLE_INTERVAL_NS = 4_096_000
FFT_SIZE = 256
FFT_BINS = FFT_SIZE // 2 + 1
RAW_BYTES = CHUNK_FRAMES * RAW_RECORD.size
FILTERED_BYTES = CHUNK_FRAMES * FILTER_RECORD.size
SPECTRUM_BYTES = FFT_BINS * SPECTRUM_BIN.size
PACKET_BYTES = HEADER_BYTES + RAW_BYTES + FILTERED_BYTES + SPECTRUM_BYTES + 4

FLAG_RAW_PRESENT = 1 << 0
FLAG_FILTERED_PRESENT = 1 << 1
FLAG_SPECTRUM_VALID = 1 << 2
FLAG_STATE_RESET = 1 << 3
REQUIRED_FLAGS = FLAG_RAW_PRESENT | FLAG_FILTERED_PRESENT
ALGORITHM_ID = 0x00010001
NO_SEQUENCE = 0xFFFFFFFF
_MASK32 = 0xFFFFFFFF


class ProcessedPacketError(ReplayLoadError):
    """Malformed, incomplete, or inconsistent EDP1 packet."""


@dataclass(frozen=True, slots=True)
class ProcessedChannelFrame:
    sequence: int
    valid_mask: int
    clipped_mask: int
    samples: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class DspPacket:
    chunk_index: int
    first_sequence: int
    raw_records: tuple[bytes, ...]
    filtered_records: tuple[ProcessedChannelFrame, ...]
    spectrum_channel: int | None
    spectrum_start_sequence: int | None
    spectrum_end_sequence: int | None
    spectrum_bins: tuple[tuple[int, int, int], ...] | None
    state_reset: bool


def _u32(value: int) -> int:
    return value & _MASK32


def _parse_raw_records(data: bytes, first_sequence: int) -> tuple[bytes, ...]:
    records = []
    for index in range(CHUNK_FRAMES):
        raw = data[index * RAW_RECORD.size:(index + 1) * RAW_RECORD.size]
        fields = RAW_RECORD.unpack(raw)
        sequence = _u32(first_sequence + index)
        if fields[:4] != (0x31444D45, 0x00600001, sequence, sequence):
            raise ProcessedPacketError(f"EDP1 raw record identity/order at frame {index}")
        if fields[4] >> 20 != 0xC or fields[5] >> 20 != 0xC:
            raise ProcessedPacketError(f"EDP1 ADS status header at frame {index}")
        if fields[22:] != (SAMPLE_INTERVAL_NS, 0x103):
            raise ProcessedPacketError(f"EDP1 raw timing/flags at frame {index}")
        if any(sample < -(1 << 23) or sample >= (1 << 23) for sample in fields[6:22]):
            raise ProcessedPacketError(f"EDP1 raw int24 extension at frame {index}")
        records.append(raw)
    return tuple(records)


def parse_dsp_packet(data: bytes | bytearray | memoryview) -> DspPacket:
    packet = bytes(data)
    if len(packet) != PACKET_BYTES:
        raise ProcessedPacketError(f"EDP1 length {len(packet)} != {PACKET_BYTES}")
    stored_crc, = struct.unpack_from("<I", packet, len(packet) - 4)
    if zlib.crc32(packet[:-4]) & _MASK32 != stored_crc:
        raise ProcessedPacketError("EDP1 packet CRC mismatch")
    h = HEADER.unpack_from(packet)
    (magic, version, header_bytes, packet_bytes, flags, chunk_index, first_sequence,
     frame_count, interval_ns, raw_bytes, filtered_bytes, spectrum_bins, fft_size,
     fft_start, fft_end, spectrum_channel, algorithm_id) = h
    if (magic, version, header_bytes, packet_bytes) != (MAGIC, VERSION, HEADER_BYTES, PACKET_BYTES):
        raise ProcessedPacketError("EDP1 magic/version/header/packet length")
    if flags & ~0x0F or flags & REQUIRED_FLAGS != REQUIRED_FLAGS:
        raise ProcessedPacketError("EDP1 flags must identify both raw and filtered payloads")
    if (frame_count, interval_ns, raw_bytes, filtered_bytes, spectrum_bins, fft_size,
            algorithm_id) != (CHUNK_FRAMES, SAMPLE_INTERVAL_NS, RAW_BYTES, FILTERED_BYTES,
                              FFT_BINS, FFT_SIZE, ALGORITHM_ID):
        raise ProcessedPacketError("EDP1 frame/interval/payload/algorithm contract")
    if first_sequence % CHUNK_FRAMES:
        raise ProcessedPacketError("EDP1 first sequence is not chunk aligned")

    offset = HEADER_BYTES
    raw_data = packet[offset:offset + RAW_BYTES]
    offset += RAW_BYTES
    raw_records = _parse_raw_records(raw_data, first_sequence)
    filtered = []
    for index in range(CHUNK_FRAMES):
        values = FILTER_RECORD.unpack_from(packet, offset + index * FILTER_RECORD.size)
        sequence, valid_mask, clipped_mask, *samples = values
        expected_sequence = _u32(first_sequence + index)
        if sequence != expected_sequence or valid_mask != 0xFFFF:
            raise ProcessedPacketError(f"EDP1 filtered record identity/valid mask at frame {index}")
        if clipped_mask & ~0xFFFF:
            raise ProcessedPacketError(f"EDP1 clipped mask width at frame {index}")
        if any(sample < -(1 << 23) or sample >= (1 << 23) for sample in samples):
            raise ProcessedPacketError(f"EDP1 filtered sample range at frame {index}")
        filtered.append(ProcessedChannelFrame(sequence, valid_mask, clipped_mask, tuple(samples)))
    offset += FILTERED_BYTES
    bins = tuple(SPECTRUM_BIN.unpack_from(packet, offset + i * SPECTRUM_BIN.size)
                 for i in range(FFT_BINS))
    spectrum_valid = bool(flags & FLAG_SPECTRUM_VALID)
    if spectrum_valid:
        if spectrum_channel >= EEG_CHANNELS or _u32(fft_end - fft_start) != FFT_SIZE - 1:
            raise ProcessedPacketError("EDP1 FFT sequence/channel identity")
        packet_end = _u32(first_sequence + CHUNK_FRAMES - 1)
        future_distance = _u32(fft_end - packet_end)
        if future_distance not in (0, 0xFFFFFFFF) and future_distance < 0x80000000:
            raise ProcessedPacketError("EDP1 FFT window ends after packet data")
    elif (fft_start, fft_end, spectrum_channel) != (NO_SEQUENCE, NO_SEQUENCE, NO_SEQUENCE):
        raise ProcessedPacketError("EDP1 absent spectrum must use sentinel identities")
    return DspPacket(
        chunk_index, first_sequence, raw_records, tuple(filtered),
        spectrum_channel if spectrum_valid else None,
        fft_start if spectrum_valid else None, fft_end if spectrum_valid else None,
        bins if spectrum_valid else None, bool(flags & FLAG_STATE_RESET),
    )


def encode_dsp_packet(
    *,
    chunk_index: int,
    first_sequence: int,
    raw_records: tuple[bytes, ...],
    filtered_records: tuple[ProcessedChannelFrame, ...],
    spectrum_channel: int | None = None,
    spectrum_start_sequence: int | None = None,
    spectrum_end_sequence: int | None = None,
    spectrum_bins: tuple[tuple[int, int, int], ...] | None = None,
    state_reset: bool = False,
) -> bytes:
    if len(raw_records) != CHUNK_FRAMES or len(filtered_records) != CHUNK_FRAMES:
        raise ValueError("EDP1 packet requires exactly 32 raw and filtered frames")
    raw_data = b"".join(raw_records)
    _parse_raw_records(raw_data, first_sequence)
    filter_data = bytearray()
    for i, row in enumerate(filtered_records):
        if row.sequence != _u32(first_sequence + i) or len(row.samples) != EEG_CHANNELS:
            raise ValueError("EDP1 filtered sequence/channel count")
        filter_data.extend(FILTER_RECORD.pack(row.sequence, row.valid_mask, row.clipped_mask, *row.samples))
    valid_spectrum = spectrum_bins is not None
    if valid_spectrum:
        if (len(spectrum_bins) != FFT_BINS or spectrum_channel is None or
                spectrum_start_sequence is None or spectrum_end_sequence is None):
            raise ValueError("EDP1 FFT result must include 129 bins and window identity")
        fft_start, fft_end, channel = spectrum_start_sequence, spectrum_end_sequence, spectrum_channel
        bins_data = b"".join(SPECTRUM_BIN.pack(*row) for row in spectrum_bins)
        flags = REQUIRED_FLAGS | FLAG_SPECTRUM_VALID
    else:
        if any(value is not None for value in (spectrum_channel, spectrum_start_sequence, spectrum_end_sequence)):
            raise ValueError("EDP1 absent FFT must not carry window identity")
        fft_start = fft_end = channel = NO_SEQUENCE
        bins_data = b"".join(SPECTRUM_BIN.pack(0, 0, 0) for _ in range(FFT_BINS))
        flags = REQUIRED_FLAGS
    if state_reset:
        flags |= FLAG_STATE_RESET
    header = HEADER.pack(MAGIC, VERSION, HEADER_BYTES, PACKET_BYTES, flags,
        chunk_index & _MASK32, first_sequence & _MASK32, CHUNK_FRAMES,
        SAMPLE_INTERVAL_NS, RAW_BYTES, FILTERED_BYTES, FFT_BINS, FFT_SIZE,
        fft_start & _MASK32, fft_end & _MASK32, channel & _MASK32, ALGORITHM_ID)
    content = header + raw_data + bytes(filter_data) + bins_data
    return content + struct.pack("<I", zlib.crc32(content) & _MASK32)


class ProcessedDspReplaySource:
    """Validated EDP1 FILE_REPLAY source for the existing Qt oscilloscope."""

    design_sample_rate_hz = 1_000_000_000 / SAMPLE_INTERVAL_NS

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path).resolve()
        self._stream: BinaryIO = self.path.open("rb")
        self._pending: list[tuple[bytes, ProcessedChannelFrame]] = []
        self._pending_index = 0
        self._processed: dict[int, ProcessedChannelFrame] = {}
        self._chunk_index: int | None = None
        self._expected_sequence: int | None = None
        self._latest_spectrum: SpectrumBatch | None = None
        self._closed = False
        self._reset_next = True

    @property
    def source_type(self) -> SourceType:
        return SourceType.FILE_REPLAY

    @property
    def display_name(self) -> str:
        return f"PL处理候选回放：{self.path.name}（FILE_REPLAY，非实时）"

    @property
    def is_real_hardware(self) -> bool:
        return False

    @property
    def sample_rate_hz(self) -> float:
        return self.design_sample_rate_hz

    @property
    def latest_spectrum(self) -> SpectrumBatch | None:
        return self._latest_spectrum

    def _read_packet(self) -> None:
        raw = self._stream.read(PACKET_BYTES)
        if not raw:
            return
        if len(raw) != PACKET_BYTES:
            raise ProcessedPacketError("truncated EDP1 file packet")
        packet = parse_dsp_packet(raw)
        if self._chunk_index is not None and packet.chunk_index != _u32(self._chunk_index + 1):
            raise ProcessedPacketError("EDP1 chunk index gap")
        if self._expected_sequence is not None and packet.first_sequence != self._expected_sequence:
            if not packet.state_reset:
                raise ProcessedPacketError("EDP1 sequence gap without DSP reset flag")
            self._reset_next = True
        self._chunk_index = packet.chunk_index
        self._expected_sequence = _u32(packet.first_sequence + CHUNK_FRAMES)
        self._pending = []
        self._pending_index = 0
        for raw_record, filtered in zip(packet.raw_records, packet.filtered_records):
            fields = RAW_RECORD.unpack(raw_record)
            frame = EegFrameV1(
                sequence=filtered.sequence,
                timestamp_ns=filtered.sequence * SAMPLE_INTERVAL_NS,
                sample_rate_millihz=244_141,
                source=SourceType.FILE_REPLAY,
                adc_present_mask=0x03,
                flags=ErrorFlag.NONE,
                adc1_status=fields[4], adc2_status=fields[5],
                samples=tuple(fields[6:22]),
            )
            self._pending.append((frame.pack(), filtered))
        if packet.spectrum_bins is not None:
            frequencies = tuple(i * self.design_sample_rate_hz / FFT_SIZE for i in range(FFT_BINS))
            amplitudes = tuple(math.sqrt(real * real + imag * imag)
                               for real, imag, _ in packet.spectrum_bins)
            spectra = [None] * EEG_CHANNELS
            spectra[packet.spectrum_channel] = Spectrum(
                frequencies, amplitudes, "PL Q15 Hann /256 magnitude counts"
            )
            self._latest_spectrum = SpectrumBatch(
                packet.spectrum_start_sequence, packet.spectrum_end_sequence,
                244_141, tuple(spectra),
            )

    def read_records(self, max_records: int) -> tuple[bytes, ...]:
        if self._closed:
            raise ProcessedPacketError("EDP1 replay is closed")
        if not isinstance(max_records, int) or isinstance(max_records, bool) or max_records < 0:
            raise ValueError("max_records must be a non-negative integer")
        result = []
        while len(result) < max_records:
            if self._pending_index >= len(self._pending):
                self._read_packet()
                if not self._pending:
                    break
            record, processed = self._pending[self._pending_index]
            self._pending_index += 1
            result.append(record)
            self._processed[processed.sequence] = processed
        return tuple(result)

    def take_processed_frame(self, sequence: int) -> ProcessedChannelFrame | None:
        return self._processed.pop(sequence, None)

    def take_state_reset(self) -> bool:
        value, self._reset_next = self._reset_next, False
        return value

    def reset(self) -> None:
        if self._closed:
            raise ProcessedPacketError("EDP1 replay is closed")
        self._stream.seek(0)
        self._pending = []
        self._pending_index = 0
        self._processed.clear()
        self._chunk_index = self._expected_sequence = None
        self._latest_spectrum = None
        self._reset_next = True

    def close(self) -> None:
        if not self._closed:
            self._stream.close()
            self._closed = True
