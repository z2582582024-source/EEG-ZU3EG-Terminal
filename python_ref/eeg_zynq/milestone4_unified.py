"""Unified offline MMIO/FIFO entry for the Milestone 4D-4I data path."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .frame import EegFrameV1, ErrorFlag, SourceType
from .milestone4_common import SYSTEM_AXI_BASE_ADDRESS, dual_id_gate_passed
from .milestone4_registers import BaseControl, BaseRegister, FrameBufferControl, FrameBufferRegister
from .milestone4f import FrameBufferController, FrameBufferFakeMMIO, FramePair

HARDWARE_EXECUTION_ALLOWED = False


class RelativeMMIO(Protocol):
    is_fake: bool
    def read32(self, offset: int) -> int: ...
    def write32(self, offset: int, value: int, wstrb: int = 0xF) -> None: ...


class UnifiedFakeMMIO(FrameBufferFakeMMIO):
    """Combined 4D/4F relative map; never maps a physical address."""

    def __init__(self, controller: FrameBufferController | None = None):
        super().__init__(controller)
        self.base_registers: dict[int, int] = {}
        self.ads1_ids=(0,0,0); self.ads2_ids=(0,0,0)

    @property
    def id_gate_passed(self) -> bool:
        return dual_id_gate_passed(self.ads1_ids, self.ads2_ids)

    def set_ids(self, ads1: tuple[int,int,int], ads2: tuple[int,int,int]) -> None:
        self.ads1_ids=tuple(ads1); self.ads2_ids=tuple(ads2)
        self.base_registers[int(BaseRegister.ADS1_ID_READS)] = ads1[0]|ads1[1]<<8|ads1[2]<<16
        self.base_registers[int(BaseRegister.ADS2_ID_READS)] = ads2[0]|ads2[1]<<8|ads2[2]<<16
        status=self.base_registers.get(int(BaseRegister.STATUS),0)
        self.base_registers[int(BaseRegister.STATUS)] = (status | (1<<6)) if self.id_gate_passed else (status & ~(1<<6))

    def push_pair(self, pair: FramePair) -> bool:
        return self.controller.push_pair(pair, id_gate_passed=self.id_gate_passed)

    def read32(self, offset: int) -> int:
        value=int(offset)
        if value < int(FrameBufferRegister.FIFO_STATUS):
            if value not in {int(x) for x in BaseRegister} and not (
                int(BaseRegister.ADS1_CHANNEL_WORD0) <= value <= 0x94 or
                int(BaseRegister.ADS2_CHANNEL_WORD0) <= value <= 0xAC
            ):
                raise KeyError(f"illegal unified offset 0x{value:03X}")
            return self.base_registers.get(value,0)
        return super().read32(value)

    def write32(self, offset: int, value: int, wstrb: int = 0xF) -> None:
        off=int(offset); value=int(value)&0xFFFF_FFFF
        if off == int(BaseRegister.CONTROL):
            if not 0 <= wstrb <= 0xF: raise ValueError("WSTRB must be a four-bit mask")
            self.writes.append((off,value,wstrb))
            if not (wstrb & 1): return
            command=BaseControl(value & 0xF)
            if command & BaseControl.SOFTWARE_SHUTDOWN: self.controller.request_shutdown()
            if command & BaseControl.CLEAR_STICKY and self.controller.status.empty and not self.controller.status.stopped:
                self.controller._overflow=False
            return
        super().write32(off,value,wstrb)


@dataclass(frozen=True, slots=True)
class UnifiedFrameSnapshot:
    token: int
    frame: EegFrameV1
    overflow_count: int
    dropped_count: int
    partial_pair_count: int
    sequence_gap_count: int


class UnifiedFrameConsumer:
    def __init__(self, mmio: RelativeMMIO, *, sample_rate_millihz: int=250_000):
        if not getattr(mmio,"is_fake",False):
            raise RuntimeError("real MMIO remains prohibited")
        self.mmio=mmio; self.sample_rate_millihz=sample_rate_millihz
        self._last_sequence: int|None=None; self._last_timestamp=0

    @staticmethod
    def _signed32(value: int) -> int:
        return value-(1<<32) if value & 0x8000_0000 else value

    def consume_one(self) -> UnifiedFrameSnapshot|None:
        if self.mmio.read32(FrameBufferRegister.FIFO_STATUS) & 1: return None
        self.mmio.write32(FrameBufferRegister.CONTROL,int(FrameBufferControl.SNAPSHOT),1)
        token=self.mmio.read32(FrameBufferRegister.SNAPSHOT_TOKEN)
        sequence=self.mmio.read32(FrameBufferRegister.FRAME_METADATA)
        a1=self.mmio.read32(FrameBufferRegister.FRAME_ADS1_STATUS)&0xFF_FFFF
        a2=self.mmio.read32(FrameBufferRegister.FRAME_ADS2_STATUS)&0xFF_FFFF
        samples=[]
        for base in (FrameBufferRegister.FRAME_ADS1_WORD0,FrameBufferRegister.FRAME_ADS2_WORD0):
            samples.extend(self._signed32(self.mmio.read32(int(base)+4*i)) for i in range(8))
        gaps=self.mmio.read32(FrameBufferRegister.SEQUENCE_GAP_COUNT)
        flags=ErrorFlag.SEQUENCE_GAP if gaps else ErrorFlag.NONE
        period=1_000_000_000_000//self.sample_rate_millihz
        if self._last_sequence is None: timestamp=0
        else: timestamp=self._last_timestamp+max(1,(sequence-self._last_sequence)&0xFFFF_FFFF)*period
        frame=EegFrameV1(sequence=sequence,timestamp_ns=timestamp,
            sample_rate_millihz=self.sample_rate_millihz,source=SourceType.SIMULATED,
            adc_present_mask=3,flags=flags,adc1_status=a1,adc2_status=a2,samples=tuple(samples))
        if self.mmio.read32(FrameBufferRegister.SNAPSHOT_TOKEN)!=token:
            raise RuntimeError("atomic snapshot token changed during read")
        self.mmio.write32(FrameBufferRegister.CONTROL,int(FrameBufferControl.CONSUME_ACK),1)
        self._last_sequence=sequence; self._last_timestamp=timestamp
        return UnifiedFrameSnapshot(token,frame,
            self.mmio.read32(FrameBufferRegister.OVERFLOW_COUNT),
            self.mmio.read32(FrameBufferRegister.DROPPED_COUNT),
            self.mmio.read32(FrameBufferRegister.PARTIAL_PAIR_COUNT),gaps)
