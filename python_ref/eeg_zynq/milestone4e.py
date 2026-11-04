"""Offline, loss-aware adapter from the frozen 4D snapshot to EegFrameV1.

The 4D peripheral exposes the last pair, not a FIFO or hardware timestamp.
Only one newly completed pair per snapshot can be transported losslessly.
"""

from __future__ import annotations

from dataclasses import dataclass

from .ads1299 import build_ads1299_frame, parse_ads1299_frame
from .frame import EegFrameV1, SourceType, combine_ads1299_frames
from .milestone4d_production import Error, OfflineController, Snapshot, State

HARDWARE_EXECUTION_ALLOWED = False


@dataclass(frozen=True)
class BridgeResult:
    frame: EegFrameV1 | None
    reason: str
    snapshot: Snapshot


class OfflineFrameBridge:
    def __init__(self, controller: OfflineController, *, sample_rate_millihz: int = 250_000,
                 origin_ns: int = 0):
        if not 1 <= sample_rate_millihz <= 0xFFFFFFFF or origin_ns < 0:
            raise ValueError("invalid simulated timing")
        self.controller = controller
        self.sample_rate_millihz = sample_rate_millihz
        self.origin_ns = origin_ns
        self.last_complete: int | None = None
        self.last_discarded = 0
        self.last_partial = 0

    def poll(self) -> BridgeResult:
        value = self.controller.snapshot()
        previous = self.last_complete
        self.last_complete = value.complete_pairs
        reason = ""
        if not value.software_id_gate or not value.id_gate_passed:
            reason = "ID_GATE"
        elif not value.config_passed or value.ads1_config_pass != 0x7FF or value.ads2_config_pass != 0x7FF:
            reason = "CONFIG"
        elif value.errors or value.fault_safe:
            reason = "ERROR:" + str(int(value.errors))
        elif value.state not in (State.FRAME_RECEIVE, State.TEST_DATA_WINDOW,
                                 State.EXIT_STOP, State.EXIT_SDATAC_ADS1,
                                 State.EXIT_SDATAC_ADS2, State.SHUTDOWN_SAFE):
            reason = "NOT_IN_DATA_PATH"
        elif previous is not None and value.complete_pairs < previous:
            reason = "COUNTER_RESET"
        elif value.discarded_frames < 3 or (value.complete_pairs == 0 and
                                             value.discarded_frames == 3):
            reason = "SETTLING"
        elif value.partial_frames != self.last_partial:
            reason = "PARTIAL_FRAME"
        elif value.complete_pairs > (previous if previous is not None else 0) + 1:
            reason = "SNAPSHOT_GAP"
        elif previous is not None and value.complete_pairs == previous:
            reason = "NO_NEW_PAIR"
        elif value.complete_pairs == 0 or value.valid_channel_positions != 0xFFFF:
            reason = "INCOMPLETE_PAIR"
        elif value.ads1_status >> 20 != 0xC or value.ads2_status >> 20 != 0xC:
            reason = "STATUS_HEADER"
        elif value.complete_pairs > 8:
            reason = "WINDOW_BOUND"
        self.last_partial = value.partial_frames
        self.last_discarded = value.discarded_frames
        if reason:
            return BridgeResult(None, reason, value)

        # Round-trip via the existing MSB-first codec, preserving its signed-24 rules.
        ads1 = parse_ads1299_frame(build_ads1299_frame(value.ads1_channels,
                                                        status=value.ads1_status))
        ads2 = parse_ads1299_frame(build_ads1299_frame(value.ads2_channels,
                                                        status=value.ads2_status))
        sequence = value.complete_pairs - 1
        timestamp = self.origin_ns + sequence * 1_000_000_000_000 // self.sample_rate_millihz
        frame = combine_ads1299_frames(
            ads1, ads2, sequence=sequence, timestamp_ns=timestamp,
            sample_rate_millihz=self.sample_rate_millihz, source=SourceType.SIMULATED)
        return BridgeResult(EegFrameV1.unpack(frame.pack()), "VALID_PAIR", value)
