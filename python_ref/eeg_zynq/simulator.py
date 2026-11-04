"""Deterministic dual-ADS1299 simulation source and fault injection."""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Iterable, Mapping

from .ads1299 import (
    SIGNED24_MAX,
    SIGNED24_MIN,
    build_ads1299_frame,
    parse_ads1299_frame,
)
from .frame import EegFrameV1, ErrorFlag, SourceType, combine_ads1299_frames

_CHANNEL_FREQUENCIES_HZ = (
    1.0,
    2.0,
    4.0,
    6.0,
    8.0,
    10.0,
    12.0,
    15.0,
    18.0,
    20.0,
    25.0,
    30.0,
    35.0,
    40.0,
    45.0,
    50.0,
)


@dataclass(frozen=True, slots=True)
class SimulationConfig:
    sample_rate_hz: float = 250.0
    seed: int = 0x1299
    start_timestamp_ns: int = 0
    signal_amplitude_counts: int = 350_000
    drift_amplitude_counts: int = 45_000
    mains_amplitude_counts: int = 12_000
    noise_std_counts: float = 2_000.0

    def __post_init__(self) -> None:
        if not math.isfinite(self.sample_rate_hz) or self.sample_rate_hz <= 0:
            raise ValueError("sample_rate_hz must be finite and positive")
        if not 0 <= self.start_timestamp_ns <= 0xFFFFFFFFFFFFFFFF:
            raise ValueError("start_timestamp_ns must fit uint64")
        if self.sample_rate_millihz < 1:
            raise ValueError("sample_rate_hz must round to at least 1 mHz")
        if self.sample_rate_millihz > 0xFFFFFFFF:
            raise ValueError("sample_rate_hz is too large for EegFrameV1")
        if not math.isfinite(self.noise_std_counts) or self.noise_std_counts < 0:
            raise ValueError("noise_std_counts must be finite and non-negative")

    @property
    def sample_rate_millihz(self) -> int:
        return int(round(self.sample_rate_hz * 1000.0))


@dataclass(frozen=True, slots=True)
class FaultPlan:
    """Frame-sequence keyed software fault plan.

    ``reorder_adjacent_at={5}`` swaps sequence 5 with sequence 6 after drops
    are applied.  ``saturate_channels`` uses zero-based logical channels.
    """

    dropped_sequences: frozenset[int] = frozenset()
    duplicate_sequences: frozenset[int] = frozenset()
    reorder_adjacent_at: frozenset[int] = frozenset()
    corrupted_crc_sequences: frozenset[int] = frozenset()
    missing_adc1_sequences: frozenset[int] = frozenset()
    missing_adc2_sequences: frozenset[int] = frozenset()
    bad_status_adc1_sequences: frozenset[int] = frozenset()
    bad_status_adc2_sequences: frozenset[int] = frozenset()
    sync_skew_sequences: frozenset[int] = frozenset()
    saturate_channels: Mapping[int, tuple[int, ...]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        sequence_sets = (
            self.dropped_sequences,
            self.duplicate_sequences,
            self.reorder_adjacent_at,
            self.corrupted_crc_sequences,
            self.missing_adc1_sequences,
            self.missing_adc2_sequences,
            self.bad_status_adc1_sequences,
            self.bad_status_adc2_sequences,
            self.sync_skew_sequences,
        )
        if any(sequence < 0 for values in sequence_sets for sequence in values):
            raise ValueError("fault sequence numbers cannot be negative")
        for sequence, channels in self.saturate_channels.items():
            if sequence < 0:
                raise ValueError("fault sequence numbers cannot be negative")
            if any(not 0 <= channel < 16 for channel in channels):
                raise ValueError("saturation channels must be in range 0..15")


class SimulatedAds1299Source:
    """Repeatable 16-channel source explicitly marked as non-hardware data."""

    def __init__(
        self,
        config: SimulationConfig | None = None,
        faults: FaultPlan | None = None,
    ) -> None:
        self.config = config or SimulationConfig()
        self.faults = faults or FaultPlan()

    @property
    def source_type(self) -> SourceType:
        return SourceType.SIMULATED

    @property
    def display_name(self) -> str:
        return "模拟数据（非真实采集）"

    @property
    def is_real_hardware(self) -> bool:
        return False

    def channel_samples(self, sequence: int) -> tuple[int, ...]:
        if sequence < 0:
            raise ValueError("sequence cannot be negative")
        cfg = self.config
        time_s = sequence / cfg.sample_rate_hz
        rng = random.Random(cfg.seed * 1_000_003 + sequence)
        values: list[int] = []
        saturated = frozenset(self.faults.saturate_channels.get(sequence, ()))

        for channel, frequency_hz in enumerate(_CHANNEL_FREQUENCIES_HZ):
            if channel in saturated:
                values.append(SIGNED24_MAX if channel % 2 == 0 else SIGNED24_MIN)
                continue
            phase = channel * math.pi / 16.0
            amplitude = cfg.signal_amplitude_counts + channel * 8_000
            value = (
                amplitude * math.sin(2.0 * math.pi * frequency_hz * time_s + phase)
                + cfg.drift_amplitude_counts
                * math.sin(2.0 * math.pi * 0.25 * time_s + phase / 3.0)
                + cfg.mains_amplitude_counts
                * math.sin(2.0 * math.pi * 50.0 * time_s + phase / 5.0)
                + rng.gauss(0.0, cfg.noise_std_counts)
            )
            values.append(max(SIGNED24_MIN, min(SIGNED24_MAX, int(round(value)))))
        return tuple(values)

    def raw_pair_for_sequence(self, sequence: int) -> tuple[bytes | None, bytes | None]:
        samples = self.channel_samples(sequence)
        adc1_status = 0 if sequence in self.faults.bad_status_adc1_sequences else 0xC00000
        adc2_status = 0 if sequence in self.faults.bad_status_adc2_sequences else 0xC00000
        adc1_raw = build_ads1299_frame(samples[:8], status=adc1_status)
        adc2_raw = build_ads1299_frame(samples[8:], status=adc2_status)
        if sequence in self.faults.missing_adc1_sequences:
            adc1_raw = None
        if sequence in self.faults.missing_adc2_sequences:
            adc2_raw = None
        return adc1_raw, adc2_raw

    def frame_for_sequence(self, sequence: int) -> EegFrameV1:
        adc1_raw, adc2_raw = self.raw_pair_for_sequence(sequence)
        adc1 = (
            parse_ads1299_frame(adc1_raw, validate_status_header=False)
            if adc1_raw is not None
            else None
        )
        adc2 = (
            parse_ads1299_frame(adc2_raw, validate_status_header=False)
            if adc2_raw is not None
            else None
        )
        extra_flags = ErrorFlag.NONE
        if sequence in self.faults.sync_skew_sequences:
            extra_flags |= ErrorFlag.SYNC_SKEW
        has_injected_fault = any(
            sequence in values
            for values in (
                self.faults.missing_adc1_sequences,
                self.faults.missing_adc2_sequences,
                self.faults.bad_status_adc1_sequences,
                self.faults.bad_status_adc2_sequences,
                self.faults.sync_skew_sequences,
            )
        ) or sequence in self.faults.saturate_channels
        if has_injected_fault:
            extra_flags |= ErrorFlag.SIMULATED_FAULT

        timestamp_ns = self.config.start_timestamp_ns + (
            sequence * 1_000_000_000_000 // self.config.sample_rate_millihz
        )
        return combine_ads1299_frames(
            adc1,
            adc2,
            sequence=sequence,
            timestamp_ns=timestamp_ns,
            sample_rate_millihz=self.config.sample_rate_millihz,
            source=self.source_type,
            extra_flags=extra_flags,
        )

    def records(self, frame_count: int) -> Iterable[bytes]:
        """Yield a finite stream after applying transport-level faults."""

        if frame_count < 0:
            raise ValueError("frame_count cannot be negative")
        entries = [
            (sequence, self.frame_for_sequence(sequence).pack())
            for sequence in range(frame_count)
            if sequence not in self.faults.dropped_sequences
        ]

        index = 0
        while index + 1 < len(entries):
            sequence, _ = entries[index]
            next_sequence, _ = entries[index + 1]
            if sequence in self.faults.reorder_adjacent_at and next_sequence == sequence + 1:
                entries[index], entries[index + 1] = entries[index + 1], entries[index]
                index += 2
            else:
                index += 1

        for sequence, record in entries:
            if sequence in self.faults.corrupted_crc_sequences:
                damaged = bytearray(record)
                damaged[44] ^= 0x01
                record = bytes(damaged)
            yield record
            if sequence in self.faults.duplicate_sequences:
                yield record


class RealtimeSimulatedSource:
    """Stateful, non-blocking simulated source for desktop playback.

    The adapter intentionally generates the normal deterministic signal only.
    Fault-injection regression remains available through
    ``SimulatedAds1299Source.records`` and scripted realtime sources in tests.
    """

    def __init__(self, source: SimulatedAds1299Source | None = None) -> None:
        self.source = source or SimulatedAds1299Source()
        self._next_sequence = 0

    @property
    def source_type(self) -> SourceType:
        return self.source.source_type

    @property
    def display_name(self) -> str:
        return self.source.display_name

    @property
    def is_real_hardware(self) -> bool:
        return self.source.is_real_hardware

    @property
    def sample_rate_hz(self) -> float:
        return self.source.config.sample_rate_hz

    @property
    def next_sequence(self) -> int:
        return self._next_sequence

    def read_records(self, max_records: int) -> tuple[bytes, ...]:
        if not isinstance(max_records, int) or isinstance(max_records, bool):
            raise TypeError("max_records must be an integer")
        if max_records < 0:
            raise ValueError("max_records cannot be negative")
        start = self._next_sequence
        self._next_sequence += max_records
        return tuple(
            self.source.frame_for_sequence(sequence).pack()
            for sequence in range(start, self._next_sequence)
        )

    def reset(self) -> None:
        self._next_sequence = 0
