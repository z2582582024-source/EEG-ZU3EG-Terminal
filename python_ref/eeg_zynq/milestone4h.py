"""Milestone 4H offline synthetic-signal, DSP, and quality analysis.

This module is deliberately software-only. It keeps EegFrameV1 in raw
ADS1299 counts and applies voltage scaling only in an explicitly parameterized
analysis layer. None of the quality flags are clinical, lead-off, impedance,
or human-EEG conclusions.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Iterator, Sequence

from .dsp import DspConfig, DspProcessor, FilteredFrame
from .frame import EEG_CHANNELS, EegFrameV1, ErrorFlag, SourceType
from .milestone4_common import ADS1299_ID_EXPECTED as ADS1299_8CH_ID, ADS1299_ID_MASK, SIGNED24_MASK as ADC_RAW_MASK, SIGNED24_MAX as ADC_MAX_COUNT, SIGNED24_MIN as ADC_MIN_COUNT, decode_signed24, dual_id_gate_passed, encode_signed24
from .milestone4g import (
    ReplayDspAdapter,
    ReplayMode,
    SessionIntegrityError,
    SustainedSessionReplay,
    SustainedSessionWriter,
    inspect_session,
)

ADC_BITS = 24
LEGAL_PGA_GAINS = frozenset({1, 2, 4, 6, 8, 12, 24})
INTERNAL_TEST_REFERENCE_VOLTAGE = 4.5
INTERNAL_TEST_PGA_GAIN = 1
SAMPLE_RATE_HZ = 250.0
SAMPLE_PERIOD_NS = 4_000_000
UINT32_MASK = 0xFFFF_FFFF


@dataclass(frozen=True, slots=True)
class AdcScale:
    """Explicit ADS1299 code scaling used only by offline analysis."""

    reference_voltage: float = INTERNAL_TEST_REFERENCE_VOLTAGE
    pga_gain: int = INTERNAL_TEST_PGA_GAIN

    def __post_init__(self) -> None:
        if not isinstance(self.reference_voltage, (int, float)) or isinstance(
            self.reference_voltage, bool
        ):
            raise TypeError("reference_voltage must be numeric")
        if not math.isfinite(float(self.reference_voltage)) or self.reference_voltage <= 0:
            raise ValueError("reference_voltage must be finite and positive")
        if self.pga_gain not in LEGAL_PGA_GAINS:
            raise ValueError(f"unsupported ADS1299 PGA gain: {self.pga_gain}")

    @property
    def lsb_volts(self) -> float:
        return float(self.reference_voltage) / (self.pga_gain * ADC_MAX_COUNT)

    def metadata(self) -> dict[str, object]:
        return {
            "reference_voltage": float(self.reference_voltage),
            "pga_gain": self.pga_gain,
            "lsb_volts": self.lsb_volts,
            "classification": "OFFLINE_PARAMETERIZED_SCALING_NOT_HARDWARE_CALIBRATION",
        }


def counts_to_volts(count: int, scale: AdcScale) -> float:
    if not isinstance(count, int) or isinstance(count, bool):
        raise TypeError("count must be an integer")
    if not ADC_MIN_COUNT <= count <= ADC_MAX_COUNT:
        raise ValueError("count is outside the signed 24-bit range")
    return count * scale.lsb_volts


def counts_to_microvolts(count: int, scale: AdcScale) -> float:
    return counts_to_volts(count, scale) * 1_000_000.0


def _round_half_away_from_zero(value: float) -> int:
    magnitude = math.floor(abs(value) + 0.5)
    return magnitude if value >= 0 else -magnitude


def volts_to_counts(volts: float, scale: AdcScale, *, saturate: bool = True) -> int:
    if not isinstance(volts, (int, float)) or isinstance(volts, bool):
        raise TypeError("volts must be numeric")
    if not math.isfinite(float(volts)):
        raise ValueError("volts must be finite")
    count = _round_half_away_from_zero(float(volts) / scale.lsb_volts)
    if ADC_MIN_COUNT <= count <= ADC_MAX_COUNT:
        return count
    if not saturate:
        raise OverflowError("voltage is outside the signed 24-bit range")
    return max(ADC_MIN_COUNT, min(ADC_MAX_COUNT, count))


def microvolts_to_counts(microvolts: float, scale: AdcScale, *, saturate: bool = True) -> int:
    if not isinstance(microvolts, (int, float)) or isinstance(microvolts, bool):
        raise TypeError("microvolts must be numeric")
    return volts_to_counts(float(microvolts) / 1_000_000.0, scale, saturate=saturate)


def id_gate_passed(ads1_ids: Sequence[int], ads2_ids: Sequence[int]) -> bool:
    return dual_id_gate_passed(ads1_ids, ads2_ids)


class SyntheticScenario(str, Enum):
    ZERO = "ZERO"
    POSITIVE_DC = "POSITIVE_DC"
    NEGATIVE_DC = "NEGATIVE_DC"
    SINE = "SINE"
    MULTISINE = "MULTISINE"
    LOW_DRIFT = "LOW_DRIFT"
    LINE_50 = "LINE_50"
    LINE_60 = "LINE_60"
    WHITE_NOISE = "WHITE_NOISE"
    COLORED_NOISE = "COLORED_NOISE"
    STEP = "STEP"
    IMPULSE = "IMPULSE"
    CLIPPED = "CLIPPED"
    POSITIVE_RAIL = "POSITIVE_RAIL"
    NEGATIVE_RAIL = "NEGATIVE_RAIL"
    FLATLINE = "FLATLINE"


SYNTHETIC_CHANNEL_PLAN: tuple[SyntheticScenario, ...] = (
    SyntheticScenario.ZERO,
    SyntheticScenario.POSITIVE_DC,
    SyntheticScenario.NEGATIVE_DC,
    SyntheticScenario.SINE,
    SyntheticScenario.MULTISINE,
    SyntheticScenario.LOW_DRIFT,
    SyntheticScenario.LINE_50,
    SyntheticScenario.LINE_60,
    SyntheticScenario.WHITE_NOISE,
    SyntheticScenario.COLORED_NOISE,
    SyntheticScenario.STEP,
    SyntheticScenario.IMPULSE,
    SyntheticScenario.CLIPPED,
    SyntheticScenario.POSITIVE_RAIL,
    SyntheticScenario.NEGATIVE_RAIL,
    SyntheticScenario.FLATLINE,
)


@dataclass(frozen=True, slots=True)
class SyntheticEnvelope:
    frame: EegFrameV1
    tags: tuple[str, ...] = ()
    invalid_channels: tuple[int, ...] = ()


class SyntheticSignalGenerator:
    """Fixed-seed streaming 16-channel synthetic source."""

    def __init__(
        self, *, scale: AdcScale | None = None, seed: int = 0x4E454748,
        sample_rate_hz: float = SAMPLE_RATE_HZ,
    ) -> None:
        if sample_rate_hz != SAMPLE_RATE_HZ:
            raise ValueError("4H reuses the frozen 250 SPS DSP configuration")
        self.scale = scale or AdcScale()
        self.seed = int(seed)
        self.sample_rate_hz = sample_rate_hz

    def frames(
        self, frame_count: int, *, start_sequence: int = 0,
        channel_plan: Sequence[SyntheticScenario] = SYNTHETIC_CHANNEL_PLAN,
        channel_swap: tuple[int, int] | None = None,
        channel_gain_factors: Sequence[float] | None = None,
        missing_channels: Sequence[int] = (),
        shutdown_after: int | None = None,
    ) -> Iterator[SyntheticEnvelope]:
        if frame_count < 0:
            raise ValueError("frame_count cannot be negative")
        if len(channel_plan) != EEG_CHANNELS:
            raise ValueError("channel_plan must contain exactly 16 scenarios")
        gains = tuple(channel_gain_factors or (1.0,) * EEG_CHANNELS)
        if len(gains) != EEG_CHANNELS or any(
            not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0
            for value in gains
        ):
            raise ValueError("channel_gain_factors must contain 16 finite non-negative values")
        invalid = tuple(sorted(set(int(index) for index in missing_channels)))
        if any(not 0 <= index < EEG_CHANNELS for index in invalid):
            raise ValueError("missing channel index is outside 0..15")
        rng = random.Random(self.seed)
        colored = [0.0] * EEG_CHANNELS
        for offset in range(frame_count):
            sequence = (start_sequence + offset) & UINT32_MASK
            timestamp_ns = offset * SAMPLE_PERIOD_NS
            values: list[int] = []
            tags: list[str] = ["SYNTHETIC"]
            for channel, scenario in enumerate(channel_plan):
                uv, colored[channel] = self._sample_uv(
                    scenario, offset, channel, rng, colored[channel], frame_count
                )
                values.append(microvolts_to_counts(uv * gains[channel], self.scale))
            if channel_swap is not None:
                left, right = channel_swap
                values[left], values[right] = values[right], values[left]
                tags.append("CHANNEL_SWAP")
            for channel in invalid:
                values[channel] = 0
            flags = ErrorFlag.SIMULATED_FAULT if invalid else ErrorFlag.NONE
            if invalid:
                tags.append("MISSING_CHANNEL")
            if shutdown_after is not None and offset >= shutdown_after:
                break
            yield SyntheticEnvelope(
                EegFrameV1(
                    sequence=sequence,
                    timestamp_ns=timestamp_ns,
                    sample_rate_millihz=250_000,
                    source=SourceType.SIMULATED,
                    adc_present_mask=3,
                    flags=flags,
                    adc1_status=0xC1_0000 | (sequence & 0xFFFF),
                    adc2_status=0xC2_0000 | (sequence & 0xFFFF),
                    samples=tuple(values),
                ),
                tuple(tags),
                invalid,
            )

    def _sample_uv(
        self, scenario: SyntheticScenario, index: int, channel: int,
        rng: random.Random, previous_colored: float, frame_count: int,
    ) -> tuple[float, float]:
        t = index / self.sample_rate_hz
        phase = channel * math.pi / 19.0
        if scenario is SyntheticScenario.ZERO:
            value = 0.0
        elif scenario is SyntheticScenario.POSITIVE_DC:
            value = 250.0
        elif scenario is SyntheticScenario.NEGATIVE_DC:
            value = -400.0
        elif scenario is SyntheticScenario.SINE:
            value = 100.0 * math.sin(2.0 * math.pi * 10.0 * t + phase)
        elif scenario is SyntheticScenario.MULTISINE:
            value = (80.0 * math.sin(2.0 * math.pi * 7.0 * t + phase)
                     + 30.0 * math.sin(2.0 * math.pi * 22.0 * t))
        elif scenario is SyntheticScenario.LOW_DRIFT:
            value = 800.0 * math.sin(2.0 * math.pi * 0.2 * t)
        elif scenario is SyntheticScenario.LINE_50:
            value = 200.0 * math.sin(2.0 * math.pi * 50.0 * t)
        elif scenario is SyntheticScenario.LINE_60:
            value = 200.0 * math.sin(2.0 * math.pi * 60.0 * t)
        elif scenario is SyntheticScenario.WHITE_NOISE:
            value = rng.gauss(0.0, 35.0)
        elif scenario is SyntheticScenario.COLORED_NOISE:
            value = 0.92 * previous_colored + rng.gauss(0.0, 12.0)
        elif scenario is SyntheticScenario.STEP:
            value = -50.0 if index < max(1, frame_count // 2) else 1_500.0
        elif scenario is SyntheticScenario.IMPULSE:
            value = 2_500.0 if index == max(1, frame_count // 3) else 0.0
        elif scenario is SyntheticScenario.CLIPPED:
            value = max(-1_000.0, min(1_000.0, 1_500.0 * math.sin(2.0 * math.pi * 8.0 * t)))
        elif scenario is SyntheticScenario.POSITIVE_RAIL:
            return counts_to_microvolts(ADC_MAX_COUNT, self.scale), previous_colored
        elif scenario is SyntheticScenario.NEGATIVE_RAIL:
            return counts_to_microvolts(ADC_MIN_COUNT, self.scale), previous_colored
        elif scenario is SyntheticScenario.FLATLINE:
            value = 17.0
        else:
            raise AssertionError(scenario)
        return value, value if scenario is SyntheticScenario.COLORED_NOISE else previous_colored


class QualityFlag(str, Enum):
    VALID = "VALID"
    SATURATED = "SATURATED"
    CLIPPED = "CLIPPED"
    FLATLINE = "FLATLINE"
    LARGE_DC = "LARGE_DC"
    LINE_NOISE_50 = "LINE_NOISE_50"
    LINE_NOISE_60 = "LINE_NOISE_60"
    DATA_GAP = "DATA_GAP"
    FRAME_INVALID = "FRAME_INVALID"
    SETTLING = "SETTLING"
    FILE_CORRUPT = "FILE_CORRUPT"


@dataclass(frozen=True, slots=True)
class QualityThresholds:
    clipping_fraction: float = 0.98
    flatline_delta_uv: float = 0.5
    flatline_run_samples: int = 250
    abrupt_step_uv: float = 1_000.0
    large_dc_uv: float = 1_000.0
    line_noise_ratio: float = 0.20

    def __post_init__(self) -> None:
        if not 0.0 < self.clipping_fraction <= 1.0:
            raise ValueError("clipping_fraction must be in (0, 1]")
        if self.flatline_run_samples < 2:
            raise ValueError("flatline_run_samples must be at least 2")
        for name in ("flatline_delta_uv", "abrupt_step_uv", "large_dc_uv",
                     "line_noise_ratio"):
            value = getattr(self, name)
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be finite and non-negative")


@dataclass(slots=True)
class _ChannelStats:
    valid: int = 0
    invalid: int = 0
    minimum: float = math.inf
    maximum: float = -math.inf
    total: float = 0.0
    total_sq: float = 0.0
    saturation: int = 0
    clipping: int = 0
    flatline: int = 0
    abrupt: int = 0
    gap: int = 0
    partial: int = 0
    corruption: int = 0
    settling: int = 0
    previous: float | None = None
    flat_run: int = 0
    cos50: float = 0.0
    sin50: float = 0.0
    cos60: float = 0.0
    sin60: float = 0.0


@dataclass(frozen=True, slots=True)
class ChannelQuality:
    channel: int
    valid_sample_count: int
    invalid_sample_count: int
    minimum_uv: float | None
    maximum_uv: float | None
    mean_uv: float | None
    rms_uv: float | None
    peak_to_peak_uv: float | None
    dc_offset_uv: float | None
    rail_saturation_count: int
    clipping_count: int
    flatline_count: int
    abrupt_step_count: int
    line_noise_50_ratio: float
    line_noise_60_ratio: float
    sequence_gap_impact: int
    partial_frame_impact: int
    file_corruption_impact: int
    settling_count: int
    quality_flags: tuple[str, ...]


class SignalQualityAnalyzer:
    """Bounded-memory per-channel digital quality accumulator."""

    def __init__(
        self, *, scale: AdcScale | None = None,
        thresholds: QualityThresholds | None = None,
        sample_rate_hz: float = SAMPLE_RATE_HZ,
    ) -> None:
        self.scale = scale or AdcScale()
        self.thresholds = thresholds or QualityThresholds()
        self.sample_rate_hz = sample_rate_hz
        self._channels = [_ChannelStats() for _ in range(EEG_CHANNELS)]
        self._sample_index = 0

    def update(
        self, frame: EegFrameV1, *, invalid_channels: Sequence[int] = (),
        data_gap: bool = False, partial_frame: bool = False,
        file_corrupt: bool = False, settling: bool = False,
    ) -> None:
        invalid = set(invalid_channels)
        frame_invalid = frame.flags != ErrorFlag.NONE or frame.adc_present_mask != 3
        clip_count = int(ADC_MAX_COUNT * self.thresholds.clipping_fraction)
        flat_delta = self.thresholds.flatline_delta_uv
        for channel, count in enumerate(frame.samples):
            stats = self._channels[channel]
            affected = channel in invalid or frame_invalid or partial_frame or file_corrupt
            if affected:
                stats.invalid += 1
                stats.partial += int(partial_frame)
                stats.corruption += int(file_corrupt)
                stats.gap += int(data_gap)
                stats.settling += int(settling)
                continue
            uv = counts_to_microvolts(count, self.scale)
            stats.valid += 1
            stats.minimum = min(stats.minimum, uv)
            stats.maximum = max(stats.maximum, uv)
            stats.total += uv
            stats.total_sq += uv * uv
            stats.saturation += int(count in (ADC_MIN_COUNT, ADC_MAX_COUNT))
            stats.clipping += int(abs(count) >= clip_count)
            if stats.previous is not None:
                delta = abs(uv - stats.previous)
                stats.abrupt += int(delta >= self.thresholds.abrupt_step_uv)
                stats.flat_run = stats.flat_run + 1 if delta <= flat_delta else 0
                if stats.flat_run == self.thresholds.flatline_run_samples:
                    stats.flatline += 1
            stats.previous = uv
            angle50 = 2.0 * math.pi * 50.0 * self._sample_index / self.sample_rate_hz
            angle60 = 2.0 * math.pi * 60.0 * self._sample_index / self.sample_rate_hz
            stats.cos50 += uv * math.cos(angle50)
            stats.sin50 += uv * math.sin(angle50)
            stats.cos60 += uv * math.cos(angle60)
            stats.sin60 += uv * math.sin(angle60)
            stats.gap += int(data_gap)
            stats.settling += int(settling)
        self._sample_index += 1

    def finalize(self) -> tuple[ChannelQuality, ...]:
        reports: list[ChannelQuality] = []
        for channel, stats in enumerate(self._channels):
            if stats.valid:
                mean = stats.total / stats.valid
                rms = math.sqrt(stats.total_sq / stats.valid)
                peak = stats.maximum - stats.minimum
                denominator = max(stats.total_sq, 1e-30)
                ratio50 = (2.0 / stats.valid) * (
                    stats.cos50 * stats.cos50 + stats.sin50 * stats.sin50
                ) / denominator
                ratio60 = (2.0 / stats.valid) * (
                    stats.cos60 * stats.cos60 + stats.sin60 * stats.sin60
                ) / denominator
                flags: set[QualityFlag] = {QualityFlag.VALID}
                if stats.saturation:
                    flags.add(QualityFlag.SATURATED)
                if stats.clipping:
                    flags.add(QualityFlag.CLIPPED)
                if stats.flatline:
                    flags.add(QualityFlag.FLATLINE)
                if abs(mean) >= self.thresholds.large_dc_uv:
                    flags.add(QualityFlag.LARGE_DC)
                if ratio50 >= self.thresholds.line_noise_ratio:
                    flags.add(QualityFlag.LINE_NOISE_50)
                if ratio60 >= self.thresholds.line_noise_ratio:
                    flags.add(QualityFlag.LINE_NOISE_60)
            else:
                mean = rms = peak = None
                ratio50 = ratio60 = 0.0
                flags = {QualityFlag.FRAME_INVALID}
            if stats.invalid:
                flags.add(QualityFlag.FRAME_INVALID)
            if stats.gap:
                flags.add(QualityFlag.DATA_GAP)
            if stats.settling:
                flags.add(QualityFlag.SETTLING)
            if stats.corruption:
                flags.add(QualityFlag.FILE_CORRUPT)
            reports.append(ChannelQuality(
                channel=channel + 1,
                valid_sample_count=stats.valid,
                invalid_sample_count=stats.invalid,
                minimum_uv=None if not stats.valid else stats.minimum,
                maximum_uv=None if not stats.valid else stats.maximum,
                mean_uv=mean,
                rms_uv=rms,
                peak_to_peak_uv=peak,
                dc_offset_uv=mean,
                rail_saturation_count=stats.saturation,
                clipping_count=stats.clipping,
                flatline_count=stats.flatline,
                abrupt_step_count=stats.abrupt,
                line_noise_50_ratio=ratio50,
                line_noise_60_ratio=ratio60,
                sequence_gap_impact=stats.gap,
                partial_frame_impact=stats.partial,
                file_corruption_impact=stats.corruption,
                settling_count=stats.settling,
                quality_flags=tuple(sorted(flag.value for flag in flags)),
            ))
        return tuple(reports)


@dataclass(frozen=True, slots=True)
class GapAwareResult:
    filtered: FilteredFrame
    relation: str
    missing_frames: int
    settling: bool
    valid_after_gap: bool
    timestamp_error: bool


class GapAwareDsp:
    """Wrap the frozen DSP with explicit discontinuity and settling metadata."""

    def __init__(self, *, settling_samples: int = 8, config: DspConfig | None = None) -> None:
        if settling_samples < 0:
            raise ValueError("settling_samples cannot be negative")
        self.settling_samples = settling_samples
        self.config = config or DspConfig()
        self.reset_session()

    def reset_session(self) -> None:
        self._adapter = ReplayDspAdapter(DspProcessor(self.config))
        self._last_sequence: int | None = None
        self._last_timestamp_ns: int | None = None
        self._settling_remaining = 0

    def process(self, frame: EegFrameV1) -> GapAwareResult:
        relation = "FIRST"
        missing = 0
        timestamp_error = False
        if self._last_sequence is not None:
            delta = ((frame.sequence & UINT32_MASK) - self._last_sequence) & UINT32_MASK
            if delta == 1:
                relation = "CONTIGUOUS"
            elif delta == 0:
                relation = "DUPLICATE"
            elif delta < 0x8000_0000:
                relation = "GAP"
                missing = delta - 1
            else:
                relation = "OUT_OF_ORDER"
            if self._last_timestamp_ns is not None:
                expected_delta = max(1, delta if delta < 0x8000_0000 else 1) * SAMPLE_PERIOD_NS
                timestamp_error = frame.timestamp_ns != self._last_timestamp_ns + expected_delta
        discontinuity = relation in {"GAP", "DUPLICATE", "OUT_OF_ORDER"} or timestamp_error
        if timestamp_error and relation == "CONTIGUOUS":
            self._adapter.processor.reset()
        filtered = self._adapter.process(frame)
        if discontinuity:
            self._settling_remaining = self.settling_samples
        settling = self._settling_remaining > 0
        if settling:
            self._settling_remaining -= 1
        self._last_sequence = frame.sequence & UINT32_MASK
        self._last_timestamp_ns = frame.timestamp_ns
        return GapAwareResult(
            filtered=filtered,
            relation=relation,
            missing_frames=missing,
            settling=settling,
            valid_after_gap=not settling and not discontinuity,
            timestamp_error=timestamp_error,
        )


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest().upper()


def analyze_session(
    session_path: str | Path, result_path: str | Path, *,
    scale: AdcScale | None = None, thresholds: QualityThresholds | None = None,
    batch_size: int = 1024,
) -> dict[str, object]:
    """Stream a validated 4G session through DSP and quality analysis."""

    source = Path(session_path)
    destination = Path(result_path)
    active_scale = scale or AdcScale()
    active_thresholds = thresholds or QualityThresholds()
    info = inspect_session(source)
    gap_dsp = GapAwareDsp()
    quality = SignalQualityAnalyzer(scale=active_scale, thresholds=active_thresholds)
    relation_counts: dict[str, int] = {}
    processed = 0
    filtered_digest = hashlib.sha256()
    with SustainedSessionReplay(source, mode=ReplayMode.ACCELERATED) as replay:
        while frames := replay.read_frames(batch_size):
            for frame in frames:
                result = gap_dsp.process(frame)
                relation_counts[result.relation] = relation_counts.get(result.relation, 0) + 1
                quality.update(
                    frame,
                    data_gap=result.relation in {"GAP", "DUPLICATE", "OUT_OF_ORDER"},
                    settling=result.settling,
                )
                filtered_digest.update(json.dumps(
                    [None if item is None else float(item) for item in result.filtered.samples],
                    separators=(",", ":"), allow_nan=False,
                ).encode("ascii"))
                processed += 1
    document: dict[str, object] = {
        "milestone": "4H-SIGNAL-QUALITY-DSP-SIMULATION",
        "classification": "OFFLINE_SYNTHETIC_NON_CLINICAL",
        "source_session": str(source.resolve()),
        "source_session_sha256": sha256_file(source),
        "source_session_frames": info.frame_count,
        "processed_frames": processed,
        "scale": active_scale.metadata(),
        "dsp": DspConfig().assumptions_dict(),
        "gap_policy": {
            "interpolation": "DISABLED",
            "filter_reset_on_discontinuity": True,
            "settling_samples": gap_dsp.settling_samples,
            "relations": relation_counts,
        },
        "quality_thresholds": asdict(active_thresholds),
        "channels": [asdict(item) for item in quality.finalize()],
        "filtered_stream_sha256": filtered_digest.hexdigest().upper(),
        "hardware_execution": "NOT_EXECUTED",
        "real_ps_axi": "NOT_INTEGRATED",
        "claims_excluded": [
            "real ADC analog performance", "electrode impedance", "lead-off",
            "common-mode rejection", "human EEG", "clinical interpretation",
        ],
    }
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":"), allow_nan=False)
    document["analysis_payload_sha256"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest().upper()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(document, indent=2, sort_keys=True, allow_nan=False) + "\n",
                           encoding="utf-8", newline="\n")
    return document


def create_synthetic_session(
    session_path: str | Path, *, frame_count: int, seed: int = 0x4E454748,
    ads1_ids: tuple[int, int, int] = (0x3E, 0x3E, 0x3E),
    ads2_ids: tuple[int, int, int] = (0x3E, 0x3E, 0x3E),
) -> object:
    if not id_gate_passed(ads1_ids, ads2_ids):
        raise PermissionError("ID gate failed; normal synthetic acquisition session is prohibited")
    path = Path(session_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    writer = SustainedSessionWriter(
        path,
        session_id=f"M4H-SYNTHETIC-{frame_count}",
        ads1_ids=ads1_ids,
        ads2_ids=ads2_ids,
        config_summary="4H_SYNTHETIC_INTERNAL_TEST_SCALE_4V5_GAIN1",
    )
    generator = SyntheticSignalGenerator(seed=seed)
    for envelope in generator.frames(frame_count):
        writer.append_frame(envelope.frame)
    return writer.close(shutdown_reason="SHUTDOWN_SAFE")


def safe_analyze_session(
    session_path: str | Path, result_path: str | Path, **kwargs: object
) -> dict[str, object]:
    try:
        return analyze_session(session_path, result_path, **kwargs)
    except SessionIntegrityError as exc:
        document = {
            "milestone": "4H-SIGNAL-QUALITY-DSP-SIMULATION",
            "classification": "OFFLINE_SYNTHETIC_NON_CLINICAL",
            "file_corrupt": True,
            "normal_analysis": False,
            "error": str(exc),
            "hardware_execution": "NOT_EXECUTED",
        }
        destination = Path(result_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n",
                               encoding="utf-8", newline="\n")
        return document
