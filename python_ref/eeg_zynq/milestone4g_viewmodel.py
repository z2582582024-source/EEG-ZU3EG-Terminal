"""Qt-compatible offline replay view-model for milestone 4G."""

from __future__ import annotations

from dataclasses import dataclass

from .milestone4g import ReplayProgress, SessionInfo


@dataclass(frozen=True, slots=True)
class Milestone4GReplayViewModel:
    banner: str
    session_identity: str
    progress: str
    channels: int
    current_sequence: str
    time_source: str
    integrity: str
    file_corrupt: bool
    completion: str
    terminal_state: str
    hardware_start_enabled: bool
    human_connection_enabled: bool

    @classmethod
    def from_session(
        cls, info: SessionInfo, progress: ReplayProgress, *, file_corrupt: bool = False,
    ) -> "Milestone4GReplayViewModel":
        completion = "NORMAL_END" if info.complete_end else "ABNORMAL_ABORT"
        if info.shutdown_reason == "FAULT_SAFE":
            terminal = "FAULT_SAFE"
        elif info.shutdown_reason == "SHUTDOWN_SAFE":
            terminal = "SHUTDOWN_SAFE"
        else:
            terminal = info.shutdown_reason
        return cls(
            banner="OFFLINE / REPLAY / DO_NOT DOWNLOAD",
            session_identity=f"{info.session_id} {info.file_sha256}",
            progress=f"{progress.current_frame}/{progress.total_frames} {progress.mode.value}",
            channels=16,
            current_sequence=("-" if progress.current_sequence is None
                              else str(progress.current_sequence)),
            time_source=info.time_source,
            integrity=(f"overflow={info.overflow_count} dropped={info.dropped_count} "
                       f"partial={info.partial_pair_count} "
                       f"gaps={info.sequence_gap_count + info.recorder_gap_events}"),
            file_corrupt=file_corrupt,
            completion=completion,
            terminal_state=terminal,
            hardware_start_enabled=False,
            human_connection_enabled=False,
        )
