"""Offline paired-frame FIFO and atomic relative-MMIO contract for 4F.

This is a deterministic software model of the bounded RTL policy.  It never
maps a physical address and intentionally exposes no /dev/mem, UIO or PYNQ
backend.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from enum import IntEnum, IntFlag
from typing import NewType

from .frame import EegFrameV1, ErrorFlag, SourceType
from .milestone4_common import SYSTEM_AXI_BASE_ADDRESS
from .milestone4_registers import FrameBufferControl, FrameBufferRegister, FrameBufferStatusBits

HARDWARE_EXECUTION_ALLOWED = False
FIFO_DEPTH = 4
FrameSequence = NewType("FrameSequence", int)


class FrameBufferIntegrityError(ValueError):
    """The entry cannot be represented as a valid paired EegFrameV1."""


@dataclass(frozen=True, slots=True)
class FramePair:
    sequence: int
    ads1_status: int
    ads2_status: int
    ads1_channels: tuple[int, ...]
    ads2_channels: tuple[int, ...]

    def __post_init__(self) -> None:
        if not 0 <= self.sequence <= 0xFFFFFFFF:
            raise ValueError("frame sequence must fit uint32")
        if not 0 <= self.ads1_status <= 0xFFFFFF or not 0 <= self.ads2_status <= 0xFFFFFF:
            raise ValueError("status words must fit 24 bits")
        if len(self.ads1_channels) != 8 or len(self.ads2_channels) != 8:
            raise ValueError("a paired frame requires 8 channels per ADC")
        for sample in self.ads1_channels + self.ads2_channels:
            if not -(1 << 31) <= sample < (1 << 31):
                raise ValueError("channel value must fit signed int32")


@dataclass(frozen=True, slots=True)
class FrameBufferStatus:
    occupancy: int
    capacity: int
    producer_sequence: int
    consumer_sequence: int | None
    overflow_count: int
    dropped_count: int
    partial_pair_count: int
    sequence_gap_count: int
    last_accepted_sequence: int | None
    last_consumed_sequence: int | None
    overflow: bool
    stopped: bool
    shutdown_safe: bool
    fault_safe: bool
    snapshot_token: int
    snapshot_valid: bool

    @property
    def empty(self) -> bool:
        return self.occupancy == 0

    @property
    def full(self) -> bool:
        return self.occupancy >= self.capacity

    @property
    def almost_full(self) -> bool:
        return self.occupancy >= max(1, self.capacity - 1)


class FrameBufferController:
    """Bounded paired FIFO with atomic head snapshot and one-shot consume ack."""

    def __init__(self, *, capacity: int = FIFO_DEPTH):
        if not isinstance(capacity, int) or capacity < 1:
            raise ValueError("capacity must be a positive integer")
        self.capacity = capacity
        self._queue: deque[FramePair] = deque()
        self.producer_sequence = 0
        self.consumer_sequence: int | None = None
        self.overflow_count = self.dropped_count = 0
        self.partial_pair_count = self.sequence_gap_count = 0
        self.last_accepted_sequence: int | None = None
        self.last_consumed_sequence: int | None = None
        self._overflow = False
        self._stopped = False
        self._shutdown_safe = False
        self._fault_safe = False
        self._snapshot_token = 0
        self._snapshot: tuple[int, FramePair] | None = None

    @property
    def status(self) -> FrameBufferStatus:
        return FrameBufferStatus(
            occupancy=len(self._queue), capacity=self.capacity,
            producer_sequence=self.producer_sequence,
            consumer_sequence=self.consumer_sequence,
            overflow_count=self.overflow_count, dropped_count=self.dropped_count,
            partial_pair_count=self.partial_pair_count,
            sequence_gap_count=self.sequence_gap_count,
            last_accepted_sequence=self.last_accepted_sequence,
            last_consumed_sequence=self.last_consumed_sequence,
            overflow=self._overflow, stopped=self._stopped,
            shutdown_safe=self._shutdown_safe, fault_safe=self._fault_safe,
            snapshot_token=self._snapshot_token,
            snapshot_valid=self._snapshot is not None,
        )

    def reset(self) -> None:
        self.__init__(capacity=self.capacity)

    def push_pair(self, pair: FramePair, *, id_gate_passed: bool = True) -> bool:
        self.producer_sequence = pair.sequence
        if not id_gate_passed or self._stopped or self._fault_safe:
            self.dropped_count += 1
            return False
        if len(self._queue) >= self.capacity:
            self._overflow = True
            self.overflow_count += 1
            self.dropped_count += 1
            return False
        if (self.last_accepted_sequence is not None and
                pair.sequence != (self.last_accepted_sequence + 1) & 0xFFFFFFFF):
            self.sequence_gap_count += 1
        self._queue.append(pair)
        self.last_accepted_sequence = pair.sequence
        return True

    def push_partial(self, *, ads1_present: bool, ads2_present: bool) -> bool:
        if ads1_present == ads2_present:
            raise ValueError("push_partial requires exactly one device present")
        self.partial_pair_count += 1
        self.dropped_count += 1
        return False

    def request_shutdown(self) -> None:
        self._stopped = True
        self._finish_shutdown_if_empty()

    def request_fault_safe(self) -> None:
        self._stopped = True
        self._fault_safe = True
        self._snapshot = None

    def _finish_shutdown_if_empty(self) -> None:
        if self._stopped and not self._fault_safe and not self._queue:
            self._shutdown_safe = True

    def snapshot(self) -> tuple[int, FramePair | None]:
        """Capture one immutable head/token pair; later writes cannot tear it."""
        self._snapshot_token = (self._snapshot_token + 1) & 0xFFFFFFFF
        self._snapshot = (self._snapshot_token, self._queue[0]) if self._queue else None
        return self._snapshot if self._snapshot else (self._snapshot_token, None)

    def snapshot_frame(self, token: int | None = None) -> FramePair | None:
        if self._snapshot is None or (token is not None and token != self._snapshot[0]):
            return None
        return self._snapshot[1]

    def consume_ack(self, token: int) -> FramePair:
        if self._fault_safe:
            raise FrameBufferIntegrityError("FAULT_SAFE locks frame consumption")
        if self._snapshot is None or self._snapshot[0] != token:
            raise FrameBufferIntegrityError("stale or repeated consume token")
        if not self._queue or self._queue[0] is not self._snapshot[1]:
            raise FrameBufferIntegrityError("snapshot head changed before consume")
        pair = self._queue.popleft()
        self.consumer_sequence = pair.sequence
        self.last_consumed_sequence = pair.sequence
        self._snapshot = None
        self._finish_shutdown_if_empty()
        return pair

    def consume_eeg_frame(self, token: int, *, timestamp_ns: int,
                          sample_rate_millihz: int) -> EegFrameV1:
        pair = self.snapshot_frame(token)
        if pair is None:
            raise FrameBufferIntegrityError("no atomic frame snapshot")
        # The snapshot may intentionally lag the producer while the FIFO has
        # multiple valid entries. Only a recorded sequence gap invalidates it.
        if self.sequence_gap_count:
            raise FrameBufferIntegrityError("sequence integrity is not clean")
        return EegFrameV1(
            sequence=pair.sequence, timestamp_ns=timestamp_ns,
            sample_rate_millihz=sample_rate_millihz, source=SourceType.SIMULATED,
            adc_present_mask=0x03, flags=ErrorFlag.NONE,
            adc1_status=pair.ads1_status, adc2_status=pair.ads2_status,
            samples=pair.ads1_channels + pair.ads2_channels)


class FrameBufferFakeMMIO:
    """Relative-register adapter for the model; no physical address exists."""

    is_fake = True
    _READABLE = (
        {int(item) for item in FrameBufferRegister}
        | {int(FrameBufferRegister.FRAME_ADS1_WORD0) + 4 * index for index in range(8)}
        | {int(FrameBufferRegister.FRAME_ADS2_WORD0) + 4 * index for index in range(8)}
    )

    def __init__(self, controller: FrameBufferController | None = None):
        self.controller = controller or FrameBufferController()
        self.writes: list[tuple[int, int, int]] = []

    def read32(self, offset: int) -> int:
        offset = int(offset)
        if offset not in self._READABLE:
            raise KeyError(f"illegal frame-buffer offset 0x{offset:02X}")
        status = self.controller.status
        if offset == FrameBufferRegister.FIFO_STATUS:
            bits = FrameBufferStatusBits.EMPTY if status.empty else 0
            bits |= FrameBufferStatusBits.FULL if status.full else 0
            bits |= FrameBufferStatusBits.ALMOST_FULL if status.almost_full else 0
            bits |= FrameBufferStatusBits.OVERFLOW if status.overflow else 0
            bits |= FrameBufferStatusBits.STOPPED if status.stopped else 0
            bits |= FrameBufferStatusBits.SHUTDOWN_SAFE if status.shutdown_safe else 0
            bits |= FrameBufferStatusBits.FAULT_SAFE if status.fault_safe else 0
            bits |= FrameBufferStatusBits.SNAPSHOT_VALID if status.snapshot_valid else 0
            return int(bits)
        values = {
            FrameBufferRegister.FIFO_OCCUPANCY: status.occupancy,
            FrameBufferRegister.FIFO_CAPACITY: status.capacity,
            FrameBufferRegister.PRODUCER_SEQUENCE: status.producer_sequence,
            FrameBufferRegister.CONSUMER_SEQUENCE: status.consumer_sequence or 0,
            FrameBufferRegister.OVERFLOW_COUNT: status.overflow_count,
            FrameBufferRegister.DROPPED_COUNT: status.dropped_count,
            FrameBufferRegister.PARTIAL_PAIR_COUNT: status.partial_pair_count,
            FrameBufferRegister.SEQUENCE_GAP_COUNT: status.sequence_gap_count,
            FrameBufferRegister.LAST_ACCEPTED_SEQUENCE: status.last_accepted_sequence or 0,
            FrameBufferRegister.LAST_CONSUMED_SEQUENCE: status.last_consumed_sequence or 0,
            FrameBufferRegister.SNAPSHOT_TOKEN: status.snapshot_token,
        }
        if offset in values:
            return int(values[offset])
        pair = self.controller.snapshot_frame()
        if pair is None:
            return 0
        if offset == FrameBufferRegister.FRAME_METADATA:
            return pair.sequence
        if offset == FrameBufferRegister.FRAME_ADS1_STATUS:
            return pair.ads1_status
        if offset == FrameBufferRegister.FRAME_ADS2_STATUS:
            return pair.ads2_status
        for base, channels in ((FrameBufferRegister.FRAME_ADS1_WORD0, pair.ads1_channels),
                               (FrameBufferRegister.FRAME_ADS2_WORD0, pair.ads2_channels)):
            if base <= offset < base + 32 and (offset - base) % 4 == 0:
                index = (offset - base) // 4
                return channels[index] & 0xFFFFFFFF
        if offset == FrameBufferRegister.CONTROL:
            return 0
        raise KeyError(f"unsupported frame-buffer offset 0x{offset:02X}")

    def write32(self, offset: int, value: int, wstrb: int = 0xF) -> None:
        offset, value = int(offset), int(value) & 0xFFFFFFFF
        if offset != FrameBufferRegister.CONTROL:
            raise PermissionError(f"read-only frame-buffer offset 0x{offset:02X}")
        if not 0 <= wstrb <= 0xF:
            raise ValueError("WSTRB must be a four-bit mask")
        self.writes.append((offset, value, wstrb))
        if not (wstrb & 1):
            return
        command = FrameBufferControl(value & 0x1F)
        if command & FrameBufferControl.FAULT_SAFE:
            self.controller.request_fault_safe()
        if command & FrameBufferControl.SOFTWARE_SHUTDOWN:
            self.controller.request_shutdown()
        if command & FrameBufferControl.SNAPSHOT:
            self.controller.snapshot()
        if command & FrameBufferControl.CONSUME_ACK:
            token = self.controller.status.snapshot_token
            self.controller.consume_ack(token)
        if command & FrameBufferControl.CLEAR_STICKY and self.controller.status.empty and not self.controller.status.stopped:
            self.controller._overflow = False
