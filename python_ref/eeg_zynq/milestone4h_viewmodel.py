"""Pure offline Qt-compatible view-model for milestone 4H."""

from __future__ import annotations

from dataclasses import dataclass

from .frame import EegFrameV1
from .milestone4h import AdcScale, ChannelQuality, counts_to_microvolts


@dataclass(frozen=True, slots=True)
class Milestone4HViewModel:
    banner: str
    raw_counts: tuple[int, ...]
    microvolts: tuple[float, ...]
    dsp_output: tuple[float | None, ...]
    channel_quality_flags: tuple[tuple[str, ...], ...]
    sequence: int
    sequence_gap: bool
    settling: bool
    file_corrupt: bool
    sample_rate_hz: float
    reference_voltage: float
    pga_gain: int
    session_frame_count: int
    completion: str
    hardware_start_enabled: bool
    ignore_id_enabled: bool
    human_connection_enabled: bool
    clinical_interpretation_enabled: bool

    @classmethod
    def from_analysis(
        cls, frame: EegFrameV1, dsp_output: tuple[float | None, ...],
        quality: tuple[ChannelQuality, ...], *, scale: AdcScale,
        session_frame_count: int, sequence_gap: bool = False,
        settling: bool = False, file_corrupt: bool = False,
        normal_end: bool = True,
    ) -> "Milestone4HViewModel":
        if len(dsp_output) != 16 or len(quality) != 16:
            raise ValueError("4H view-model requires 16 channels")
        return cls(
            banner="OFFLINE / SYNTHETIC / REPLAY / DO_NOT DOWNLOAD",
            raw_counts=frame.samples,
            microvolts=tuple(counts_to_microvolts(value, scale) for value in frame.samples),
            dsp_output=dsp_output,
            channel_quality_flags=tuple(item.quality_flags for item in quality),
            sequence=frame.sequence,
            sequence_gap=sequence_gap,
            settling=settling,
            file_corrupt=file_corrupt,
            sample_rate_hz=frame.sample_rate_millihz / 1000.0,
            reference_voltage=scale.reference_voltage,
            pga_gain=scale.pga_gain,
            session_frame_count=session_frame_count,
            completion="NORMAL_END" if normal_end else "ABNORMAL_END",
            hardware_start_enabled=False,
            ignore_id_enabled=False,
            human_connection_enabled=False,
            clinical_interpretation_enabled=False,
        )

