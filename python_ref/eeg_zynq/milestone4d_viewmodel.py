"""Qt-consumable offline view-model with no hardware side effects."""

from dataclasses import dataclass

from .milestone4d_production import Snapshot, decode_error_names


@dataclass(frozen=True)
class Milestone4DOfflineViewModel:
    banner: str
    hardware_start_enabled: bool
    state: str
    ads1_ids: str
    ads2_ids: str
    id_gate: str
    configuration: str
    frame_counts: str
    errors: str
    shutdown_request: str
    terminal_state: str

    @classmethod
    def from_snapshot(cls, value: Snapshot) -> "Milestone4DOfflineViewModel":
        format_ids = lambda values: "/".join(f"0x{item:02X}" for item in values)
        return cls(
            banner="OFFLINE / DO_NOT DOWNLOAD",
            hardware_start_enabled=False,
            state=f"{int(value.state):02d}/{value.state.name}",
            ads1_ids=format_ids(value.ads1_ids),
            ads2_ids=format_ids(value.ads2_ids),
            id_gate="PASSED" if value.id_gate_passed else "FAILED",
            configuration="PASSED" if value.config_passed else "NOT_PASSED",
            frame_counts=(f"valid={value.complete_pairs} discarded={value.discarded_frames} "
                          f"partial={value.partial_frames} drdy_errors={value.drdy_errors}"),
            errors="NONE" if not value.errors else ",".join(decode_error_names(value.errors)),
            shutdown_request="SEEN" if value.shutdown_request_seen else "NOT_SEEN",
            terminal_state=("SHUTDOWN_SAFE" if value.shutdown_safe else
                            "FAULT_SAFE" if value.fault_safe else "ACTIVE"),
        )
