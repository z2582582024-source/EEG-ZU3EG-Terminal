"""Standard-library CPU/Python filtering and FFT golden reference.

The defaults are centralized design assumptions for software development, not
measured hardware characteristics.  Processing remains in raw ADC counts until
PGA and VREF are frozen and verified.  A missing ADC produces ``None`` rather
than a numeric zero so consumers cannot mistake padding for a real 0 V sample.
"""

from __future__ import annotations

import cmath
import math
from collections.abc import Sequence
from dataclasses import dataclass

from .frame import EEG_CHANNELS, EegFrameV1, ErrorFlag, SourceType


@dataclass(frozen=True, slots=True)
class DspConfig:
    """Central DSP parameters; every default below is a design assumption."""

    sample_rate_hz: float = 250.0
    highpass_hz: float = 0.5
    lowpass_hz: float = 45.0
    notch_hz: float = 50.0
    notch_q: float = 30.0
    filter_q: float = math.sqrt(0.5)
    fft_size: int = 256
    fft_window: str = "hann_periodic"

    def __post_init__(self) -> None:
        numeric = {
            "sample_rate_hz": self.sample_rate_hz,
            "highpass_hz": self.highpass_hz,
            "lowpass_hz": self.lowpass_hz,
            "notch_hz": self.notch_hz,
            "notch_q": self.notch_q,
            "filter_q": self.filter_q,
        }
        for name, value in numeric.items():
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise TypeError(f"{name} must be a number")
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        nyquist_hz = self.sample_rate_hz / 2.0
        if not self.highpass_hz < self.lowpass_hz:
            raise ValueError("highpass_hz must be lower than lowpass_hz")
        for name in ("highpass_hz", "lowpass_hz", "notch_hz"):
            if getattr(self, name) >= nyquist_hz:
                raise ValueError(f"{name} must be below the Nyquist frequency")
        if not isinstance(self.fft_size, int) or isinstance(self.fft_size, bool):
            raise TypeError("fft_size must be an integer")
        if self.fft_size < 2 or self.fft_size & (self.fft_size - 1):
            raise ValueError("fft_size must be a power of two and at least 2")
        if self.fft_window != "hann_periodic":
            raise ValueError("fft_window must be 'hann_periodic'")
        if not 1 <= self.sample_rate_millihz <= 0xFFFFFFFF:
            raise ValueError("sample_rate_hz cannot be represented by EegFrameV1 in mHz")

    @property
    def sample_rate_millihz(self) -> int:
        return int(round(self.sample_rate_hz * 1000.0))

    def assumptions_dict(self) -> dict[str, int | float | str]:
        return {
            "sample_rate_hz": self.sample_rate_hz,
            "highpass_hz": self.highpass_hz,
            "lowpass_hz": self.lowpass_hz,
            "notch_hz": self.notch_hz,
            "notch_q": self.notch_q,
            "filter_q": self.filter_q,
            "fft_size": self.fft_size,
            "fft_window": self.fft_window,
            "classification": "design assumptions; not hardware-verified",
            "input_unit": "raw ADC counts",
        }


@dataclass(frozen=True, slots=True)
class BiquadCoefficients:
    b0: float
    b1: float
    b2: float
    a1: float
    a2: float


def _normalized_biquad(
    b0: float,
    b1: float,
    b2: float,
    a0: float,
    a1: float,
    a2: float,
) -> BiquadCoefficients:
    return BiquadCoefficients(b0 / a0, b1 / a0, b2 / a0, a1 / a0, a2 / a0)


def design_highpass(sample_rate_hz: float, cutoff_hz: float, q: float) -> BiquadCoefficients:
    omega = 2.0 * math.pi * cutoff_hz / sample_rate_hz
    cosine = math.cos(omega)
    alpha = math.sin(omega) / (2.0 * q)
    return _normalized_biquad(
        (1.0 + cosine) / 2.0,
        -(1.0 + cosine),
        (1.0 + cosine) / 2.0,
        1.0 + alpha,
        -2.0 * cosine,
        1.0 - alpha,
    )


def design_lowpass(sample_rate_hz: float, cutoff_hz: float, q: float) -> BiquadCoefficients:
    omega = 2.0 * math.pi * cutoff_hz / sample_rate_hz
    cosine = math.cos(omega)
    alpha = math.sin(omega) / (2.0 * q)
    return _normalized_biquad(
        (1.0 - cosine) / 2.0,
        1.0 - cosine,
        (1.0 - cosine) / 2.0,
        1.0 + alpha,
        -2.0 * cosine,
        1.0 - alpha,
    )


def design_notch(sample_rate_hz: float, notch_hz: float, q: float) -> BiquadCoefficients:
    omega = 2.0 * math.pi * notch_hz / sample_rate_hz
    cosine = math.cos(omega)
    alpha = math.sin(omega) / (2.0 * q)
    return _normalized_biquad(
        1.0,
        -2.0 * cosine,
        1.0,
        1.0 + alpha,
        -2.0 * cosine,
        1.0 - alpha,
    )


def filter_coefficients(config: DspConfig) -> tuple[BiquadCoefficients, ...]:
    """Return the fixed high-pass, notch, low-pass cascade in processing order."""

    return (
        design_highpass(config.sample_rate_hz, config.highpass_hz, config.filter_q),
        design_notch(config.sample_rate_hz, config.notch_hz, config.notch_q),
        design_lowpass(config.sample_rate_hz, config.lowpass_hz, config.filter_q),
    )


class Biquad:
    """Transposed direct-form-II floating-point biquad."""

    def __init__(self, coefficients: BiquadCoefficients) -> None:
        self.coefficients = coefficients
        self.reset()

    def reset(self) -> None:
        self._z1 = 0.0
        self._z2 = 0.0

    def process(self, sample: float) -> float:
        coefficients = self.coefficients
        output = coefficients.b0 * sample + self._z1
        self._z1 = coefficients.b1 * sample - coefficients.a1 * output + self._z2
        self._z2 = coefficients.b2 * sample - coefficients.a2 * output
        return output


def _new_filter_chain(config: DspConfig) -> tuple[Biquad, ...]:
    return tuple(Biquad(coefficients) for coefficients in filter_coefficients(config))


def filter_samples(samples: Sequence[int | float], config: DspConfig | None = None) -> tuple[float, ...]:
    """Filter one contiguous channel; intended for tests and golden-vector use."""

    active_config = config or DspConfig()
    chain = _new_filter_chain(active_config)
    output: list[float] = []
    for sample in samples:
        value = float(sample)
        if not math.isfinite(value):
            raise ValueError("filter input samples must be finite")
        for section in chain:
            value = section.process(value)
        output.append(value)
    return tuple(output)


@dataclass(frozen=True, slots=True)
class FilteredFrame:
    sequence: int
    timestamp_ns: int
    sample_rate_millihz: int
    source: SourceType
    flags: ErrorFlag
    samples: tuple[float | None, ...]
    state_reset: bool


class DspProcessor:
    """Stateful 16-channel filter with explicit missing-data handling."""

    def __init__(self, config: DspConfig | None = None) -> None:
        self.config = config or DspConfig()
        self._channels = tuple(_new_filter_chain(self.config) for _ in range(EEG_CHANNELS))
        self._last_sequence: int | None = None

    def reset(self) -> None:
        for channel in self._channels:
            for section in channel:
                section.reset()
        self._last_sequence = None

    def _reset_channel(self, channel_index: int) -> None:
        for section in self._channels[channel_index]:
            section.reset()

    def process(self, frame: EegFrameV1) -> FilteredFrame:
        if frame.sample_rate_millihz != self.config.sample_rate_millihz:
            raise ValueError(
                "frame sample rate does not match DspConfig: "
                f"{frame.sample_rate_millihz} != {self.config.sample_rate_millihz} mHz"
            )

        discontinuity = self._last_sequence is not None and frame.sequence != self._last_sequence + 1
        if discontinuity:
            for channel_index in range(EEG_CHANNELS):
                self._reset_channel(channel_index)

        output: list[float | None] = []
        for channel_index, sample in enumerate(frame.samples):
            adc_bit = 0x01 if channel_index < 8 else 0x02
            if not frame.adc_present_mask & adc_bit:
                self._reset_channel(channel_index)
                output.append(None)
                continue
            value = float(sample)
            for section in self._channels[channel_index]:
                value = section.process(value)
            output.append(value)

        self._last_sequence = frame.sequence
        return FilteredFrame(
            sequence=frame.sequence,
            timestamp_ns=frame.timestamp_ns,
            sample_rate_millihz=frame.sample_rate_millihz,
            source=frame.source,
            flags=frame.flags,
            samples=tuple(output),
            state_reset=discontinuity,
        )


def radix2_fft(samples: Sequence[complex | float | int]) -> tuple[complex, ...]:
    """Unscaled forward radix-2 FFT using the ``exp(-j*2*pi*k/N)`` convention."""

    size = len(samples)
    if size < 2 or size & (size - 1):
        raise ValueError("FFT input length must be a power of two and at least 2")
    values = [complex(sample) for sample in samples]
    if any(not math.isfinite(value.real) or not math.isfinite(value.imag) for value in values):
        raise ValueError("FFT input samples must be finite")

    target = 0
    for index in range(1, size):
        bit = size >> 1
        while target & bit:
            target ^= bit
            bit >>= 1
        target ^= bit
        if index < target:
            values[index], values[target] = values[target], values[index]

    width = 2
    while width <= size:
        phase_step = cmath.exp(-2j * math.pi / width)
        half = width // 2
        for start in range(0, size, width):
            phase = 1.0 + 0.0j
            for offset in range(half):
                even = values[start + offset]
                odd = phase * values[start + offset + half]
                values[start + offset] = even + odd
                values[start + offset + half] = even - odd
                phase *= phase_step
        width *= 2
    return tuple(values)


@dataclass(frozen=True, slots=True)
class Spectrum:
    frequencies_hz: tuple[float, ...]
    amplitudes: tuple[float, ...]
    input_unit: str = "raw ADC counts"


@dataclass(frozen=True, slots=True)
class SpectrumBatch:
    """One globally aligned FFT block for all logical channels."""

    start_sequence: int
    end_sequence: int
    sample_rate_millihz: int
    spectra: tuple[Spectrum | None, ...]


def periodic_hann(size: int) -> tuple[float, ...]:
    if size < 2:
        raise ValueError("Hann window size must be at least 2")
    return tuple(0.5 - 0.5 * math.cos(2.0 * math.pi * index / size) for index in range(size))


def one_sided_spectrum(
    samples: Sequence[int | float], config: DspConfig | None = None
) -> Spectrum:
    """Return Hann coherent-gain-corrected one-sided peak amplitudes."""

    active_config = config or DspConfig()
    if len(samples) != active_config.fft_size:
        raise ValueError(f"expected exactly {active_config.fft_size} FFT samples, got {len(samples)}")
    window = periodic_hann(active_config.fft_size)
    weighted = tuple(float(sample) * weight for sample, weight in zip(samples, window))
    transformed = radix2_fft(weighted)
    coherent_sum = sum(window)
    last_bin = active_config.fft_size // 2
    amplitudes: list[float] = []
    for bin_index in range(last_bin + 1):
        scale = 1.0 / coherent_sum if bin_index in (0, last_bin) else 2.0 / coherent_sum
        amplitudes.append(abs(transformed[bin_index]) * scale)
    frequencies = tuple(
        bin_index * active_config.sample_rate_hz / active_config.fft_size
        for bin_index in range(last_bin + 1)
    )
    return Spectrum(frequencies, tuple(amplitudes))


class SpectrumAccumulator:
    """Build non-overlapping, sequence-aligned 16-channel FFT blocks.

    A missing sample invalidates only that channel for the current block.  A
    sequence discontinuity or upstream filter-state reset discards the entire
    unfinished block, so no spectrum spans a data gap.
    """

    def __init__(self, config: DspConfig | None = None) -> None:
        self.config = config or DspConfig()
        self._buffers = tuple([] for _ in range(EEG_CHANNELS))
        self._valid = [True] * EEG_CHANNELS
        self._start_sequence: int | None = None
        self._last_sequence: int | None = None

    @property
    def buffered_frame_count(self) -> int:
        return len(self._buffers[0])

    def reset(self) -> None:
        for buffer in self._buffers:
            buffer.clear()
        self._valid = [True] * EEG_CHANNELS
        self._start_sequence = None
        self._last_sequence = None

    def _discard_block(self) -> None:
        for buffer in self._buffers:
            buffer.clear()
        self._valid = [True] * EEG_CHANNELS
        self._start_sequence = None

    def push(self, frame: FilteredFrame) -> SpectrumBatch | None:
        if frame.sample_rate_millihz != self.config.sample_rate_millihz:
            raise ValueError(
                "filtered-frame sample rate does not match DspConfig: "
                f"{frame.sample_rate_millihz} != {self.config.sample_rate_millihz} mHz"
            )
        if len(frame.samples) != EEG_CHANNELS:
            raise ValueError(f"expected {EEG_CHANNELS} filtered channels, got {len(frame.samples)}")

        discontinuity = self._last_sequence is not None and frame.sequence != self._last_sequence + 1
        if frame.state_reset or discontinuity:
            self._discard_block()
        if self._start_sequence is None:
            self._start_sequence = frame.sequence

        for channel_index, sample in enumerate(frame.samples):
            if sample is None:
                self._valid[channel_index] = False
                self._buffers[channel_index].append(0.0)
            else:
                value = float(sample)
                if not math.isfinite(value):
                    raise ValueError("filtered samples must be finite or None")
                self._buffers[channel_index].append(value)
        self._last_sequence = frame.sequence

        if self.buffered_frame_count < self.config.fft_size:
            return None
        if self.buffered_frame_count != self.config.fft_size:
            raise AssertionError("FFT accumulator exceeded its configured window")

        spectra = tuple(
            one_sided_spectrum(buffer, self.config) if self._valid[index] else None
            for index, buffer in enumerate(self._buffers)
        )
        result = SpectrumBatch(
            start_sequence=self._start_sequence,
            end_sequence=frame.sequence,
            sample_rate_millihz=frame.sample_rate_millihz,
            spectra=spectra,
        )
        self._discard_block()
        return result
