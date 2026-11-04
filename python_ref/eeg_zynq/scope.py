"""Qt-independent realtime oscilloscope session and display state."""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType

from .datasource import RealtimeFrameSource
from .dsp import DspConfig, DspProcessor, FilteredFrame, SpectrumAccumulator, SpectrumBatch
from .electrodes import ELECTRODE_CHANNELS
from .frame import EEG_CHANNELS, EegFrameV1, ErrorFlag, SourceType
from .validation import ErrorCounters, StreamValidator


class WaveformMode(Enum):
    RAW = "raw"
    FILTERED = "filtered"


TIME_WINDOWS_SECONDS = (1.0, 2.0, 4.0, 8.0)
AMPLITUDE_SCALES = (0.25, 0.5, 1.0, 2.0, 4.0, 8.0)


@dataclass(frozen=True, slots=True)
class ScopeSnapshot:
    playing: bool
    visible_channel_count: int
    waveform_mode: WaveformMode
    time_window_seconds: float
    amplitude_scale: float
    sample_rate_hz: float
    last_sequence: int | None
    source_type: SourceType
    source_display_name: str
    is_real_hardware: bool
    raw_waveforms: tuple[tuple[float | None, ...], ...]
    filtered_waveforms: tuple[tuple[float | None, ...], ...]
    latest_spectrum: SpectrumBatch | None
    error_counters: Mapping[str, int]

    @property
    def visible_channels(self) -> tuple[str, ...]:
        return tuple(
            f"{channel.logical_channel} {channel.electrode}"
            for channel in ELECTRODE_CHANNELS[: self.visible_channel_count]
        )

    @property
    def displayed_waveforms(self) -> tuple[tuple[float | None, ...], ...]:
        source = self.raw_waveforms if self.waveform_mode is WaveformMode.RAW else self.filtered_waveforms
        sample_count = max(1, int(round(self.time_window_seconds * self.sample_rate_hz)))
        return tuple(channel[-sample_count:] for channel in source[: self.visible_channel_count])


class OscilloscopeSession:
    """Consume EegFrameV1 batches and maintain GUI-ready display snapshots."""

    def __init__(
        self,
        source: RealtimeFrameSource,
        *,
        dsp_config: DspConfig | None = None,
        history_seconds: float = 8.0,
    ) -> None:
        if not isinstance(source, RealtimeFrameSource):
            raise TypeError("source must implement RealtimeFrameSource")
        if source.source_type is SourceType.ADS1299_DMA and not source.is_real_hardware:
            raise ValueError("ADS1299_DMA source must declare verified real-hardware provenance")
        if source.is_real_hardware and source.source_type is not SourceType.ADS1299_DMA:
            raise ValueError("real-hardware source must use ADS1299_DMA provenance")
        if not math.isfinite(history_seconds) or history_seconds < max(TIME_WINDOWS_SECONDS):
            raise ValueError(f"history_seconds must be at least {max(TIME_WINDOWS_SECONDS):g}")
        self.source = source
        source_rate = getattr(source, "sample_rate_hz", None)
        if source_rate is None:
            source_rate = getattr(source, "design_sample_rate_hz", None)
        if source_rate is None:
            source_info = getattr(source, "info", None)
            source_rate = getattr(source_info, "sample_rate_hz", None)
        self.dsp_config = dsp_config or DspConfig(
            sample_rate_hz=source_rate or DspConfig().sample_rate_hz
        )
        self._history_capacity = int(math.ceil(history_seconds * self.dsp_config.sample_rate_hz))
        self._raw = tuple(deque(maxlen=self._history_capacity) for _ in range(EEG_CHANNELS))
        self._filtered = tuple(deque(maxlen=self._history_capacity) for _ in range(EEG_CHANNELS))
        self._playing = True
        self._visible_channel_count = 8
        self._waveform_mode = WaveformMode.FILTERED
        self._time_window_seconds = 4.0
        self._amplitude_scale = 1.0
        self._record_sink: Callable[[bytes], None] | None = None
        self._diagnostic_active = False
        self._reset_processing()

    def _reset_processing(self) -> None:
        self._validator = StreamValidator()
        self._processor = DspProcessor(self.dsp_config)
        self._spectrum_accumulator = SpectrumAccumulator(self.dsp_config)
        self._latest_spectrum: SpectrumBatch | None = None
        self._last_frame: EegFrameV1 | None = None

    @property
    def playing(self) -> bool:
        return self._playing

    @property
    def history_capacity(self) -> int:
        return self._history_capacity

    @property
    def diagnostic_active(self) -> bool:
        return self._diagnostic_active

    def play(self) -> None:
        self._playing = True

    def pause(self) -> None:
        self._playing = False

    def toggle_playback(self) -> bool:
        self._playing = not self._playing
        return self._playing

    def restart(self) -> None:
        self.source.reset()
        self._clear_processing_state()

    def _clear_processing_state(self) -> None:
        self._reset_processing()
        for channel in self._raw:
            channel.clear()
        for channel in self._filtered:
            channel.clear()

    def begin_diagnostic_mode(self) -> None:
        """Pause EEG processing before an out-of-band hardware diagnostic."""

        if self._diagnostic_active:
            raise RuntimeError("diagnostic mode is already active")
        if self._record_sink is not None:
            raise RuntimeError("detach and close the EEG record sink before diagnostics")
        self._diagnostic_active = True
        self.pause()

    def end_diagnostic_mode(self) -> None:
        """Discard pre-diagnostic DSP/validator history before EEG resumes."""

        if not self._diagnostic_active:
            return
        self._diagnostic_active = False
        self._clear_processing_state()

    def set_record_sink(self, sink: Callable[[bytes], None] | None) -> None:
        """Attach one validated-record consumer, normally ContinuousRecordWriter."""

        if sink is not None and not callable(sink):
            raise TypeError("record sink must be callable or None")
        if sink is not None and self._diagnostic_active:
            raise RuntimeError("cannot attach an EEG record sink during diagnostics")
        self._record_sink = sink

    def set_visible_channel_count(self, channel_count: int) -> None:
        if channel_count not in (8, 16):
            raise ValueError("visible channel count must be 8 or 16")
        self._visible_channel_count = channel_count

    def set_waveform_mode(self, mode: WaveformMode | str) -> None:
        try:
            self._waveform_mode = mode if isinstance(mode, WaveformMode) else WaveformMode(mode)
        except ValueError as exc:
            raise ValueError("waveform mode must be 'raw' or 'filtered'") from exc

    def set_time_window_seconds(self, seconds: float) -> None:
        if seconds not in TIME_WINDOWS_SECONDS:
            raise ValueError(f"time window must be one of {TIME_WINDOWS_SECONDS}")
        self._time_window_seconds = seconds

    def set_amplitude_scale(self, scale: float) -> None:
        if scale not in AMPLITUDE_SCALES:
            raise ValueError(f"amplitude scale must be one of {AMPLITUDE_SCALES}")
        self._amplitude_scale = scale

    def advance(self, max_records: int) -> int:
        """Process up to ``max_records`` without blocking; return valid-frame count."""

        if not isinstance(max_records, int) or isinstance(max_records, bool):
            raise TypeError("max_records must be an integer")
        if max_records < 0:
            raise ValueError("max_records cannot be negative")
        if self._diagnostic_active or not self._playing or max_records == 0:
            return 0

        processed = 0
        records = iter(self.source.read_records(max_records))
        for record_index, record in enumerate(records):
            if record_index >= max_records:
                raise ValueError("realtime source returned more records than requested")
            data = bytes(record)
            if self._record_sink is not None:
                self._record_sink(data)
            frame = self._validator.observe_record(data)
            if frame is None:
                continue
            if frame.source is not self.source.source_type:
                raise ValueError(
                    "record/source provenance mismatch: "
                    f"record={frame.source.name}, adapter={self.source.source_type.name}"
                )
            processed_getter = getattr(self.source, "take_processed_frame", None)
            processed_row = processed_getter(frame.sequence) if processed_getter else None
            if processed_row is None:
                filtered = self._processor.process(frame)
                spectrum = self._spectrum_accumulator.push(filtered)
                if spectrum is not None:
                    self._latest_spectrum = spectrum
            else:
                reset_getter = getattr(self.source, "take_state_reset", None)
                filtered = FilteredFrame(
                    sequence=frame.sequence,
                    timestamp_ns=frame.timestamp_ns,
                    sample_rate_millihz=frame.sample_rate_millihz,
                    source=frame.source,
                    flags=frame.flags,
                    samples=tuple(float(value) for value in processed_row.samples),
                    state_reset=bool(reset_getter()) if reset_getter else False,
                )
                source_spectrum = getattr(self.source, "latest_spectrum", None)
                if source_spectrum is not None:
                    delta = (frame.sequence - source_spectrum.end_sequence) & 0xFFFFFFFF
                    spectrum_is_current = delta < 0x80000000
                    if spectrum_is_current and (
                        self._latest_spectrum is None or
                        source_spectrum.end_sequence != self._latest_spectrum.end_sequence
                    ):
                        self._latest_spectrum = source_spectrum

            for channel_index in range(EEG_CHANNELS):
                adc_bit = 0x01 if channel_index < 8 else 0x02
                raw_sample: float | None = (
                    float(frame.samples[channel_index]) if frame.adc_present_mask & adc_bit else None
                )
                self._raw[channel_index].append(raw_sample)
                self._filtered[channel_index].append(filtered.samples[channel_index])
            self._last_frame = frame
            processed += 1
        return processed

    def snapshot(self) -> ScopeSnapshot:
        counters: ErrorCounters = self._validator.counters
        error_counters = MappingProxyType(counters.to_dict())
        return ScopeSnapshot(
            playing=self._playing,
            visible_channel_count=self._visible_channel_count,
            waveform_mode=self._waveform_mode,
            time_window_seconds=self._time_window_seconds,
            amplitude_scale=self._amplitude_scale,
            sample_rate_hz=self.dsp_config.sample_rate_hz,
            last_sequence=self._last_frame.sequence if self._last_frame is not None else None,
            source_type=self.source.source_type,
            source_display_name=self.source.display_name,
            is_real_hardware=self.source.is_real_hardware,
            raw_waveforms=tuple(tuple(channel) for channel in self._raw),
            filtered_waveforms=tuple(tuple(channel) for channel in self._filtered),
            latest_spectrum=self._latest_spectrum,
            error_counters=error_counters,
        )
