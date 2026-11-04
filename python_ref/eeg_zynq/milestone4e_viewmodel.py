"""Qt-consumable simulation-only display state; no hardware controls."""

from dataclasses import dataclass

from .milestone4d_viewmodel import Milestone4DOfflineViewModel
from .milestone4e import BridgeResult


@dataclass(frozen=True)
class Milestone4EViewModel:
    banner: str
    hardware_start_enabled: bool
    channel_labels: tuple[str, ...]
    channel_values: tuple[int | None, ...]
    frame_counts: str
    id_gate: str
    configuration: str
    state: str
    errors: str
    shutdown_request: str
    terminal_state: str
    frame_reason: str

    @classmethod
    def from_result(cls, result: BridgeResult) -> "Milestone4EViewModel":
        base = Milestone4DOfflineViewModel.from_snapshot(result.snapshot)
        return cls(
            banner="OFFLINE / SIMULATION / DO_NOT DOWNLOAD",
            hardware_start_enabled=False,
            channel_labels=tuple(f"CH{i:02d}" for i in range(1, 17)),
            channel_values=result.frame.samples if result.frame else (None,) * 16,
            frame_counts=base.frame_counts,
            id_gate="PASSED" if (result.snapshot.id_gate_passed and
                                 result.snapshot.software_id_gate) else "FAILED",
            configuration=base.configuration, state=base.state,
            errors=base.errors, shutdown_request=base.shutdown_request,
            terminal_state=base.terminal_state, frame_reason=result.reason,
        )
