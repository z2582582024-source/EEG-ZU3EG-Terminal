"""Simulation-only frame-buffer status for a future Qt screen."""

from dataclasses import dataclass

from .milestone4f import FrameBufferStatus


@dataclass(frozen=True, slots=True)
class Milestone4FViewModel:
    banner: str
    hardware_start_enabled: bool
    fifo: str
    sequences: str
    integrity: str
    last_frame: str
    terminal_state: str

    @classmethod
    def from_status(cls, status: FrameBufferStatus) -> "Milestone4FViewModel":
        if status.fault_safe:
            terminal = "FAULT_SAFE"
        elif status.shutdown_safe:
            terminal = "SHUTDOWN_SAFE"
        else:
            terminal = "ACTIVE"
        integrity = (f"overflow={status.overflow_count} dropped={status.dropped_count} "
                     f"partial={status.partial_pair_count} gaps={status.sequence_gap_count}")
        return cls(
            banner="OFFLINE / SIMULATION / DO_NOT DOWNLOAD",
            hardware_start_enabled=False,
            fifo=(f"occupancy={status.occupancy}/{status.capacity} "
                  f"empty={int(status.empty)} full={int(status.full)} "
                  f"almost_full={int(status.almost_full)}"),
            sequences=(f"producer={status.producer_sequence} "
                       f"consumer={status.consumer_sequence if status.consumer_sequence is not None else '-'}"),
            integrity=integrity,
            last_frame=(str(status.last_accepted_sequence)
                        if status.last_accepted_sequence is not None else "-"),
            terminal_state=terminal,
        )

