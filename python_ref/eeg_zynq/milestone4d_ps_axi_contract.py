from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Collection, Protocol, runtime_checkable

from .milestone4_common import SYSTEM_AXI_BASE_ADDRESS

HARDWARE_EXECUTION_ALLOWED = False

@runtime_checkable
class RelativeMmio32(Protocol):
    """Peripheral-relative 32-bit MMIO contract; no physical mapping."""

    def read32(self, offset: int) -> int:
        ...

    def write32(self, offset: int, value: int, wstrb: int = 0xF) -> None:
        ...

def validate_relative_offset(offset: int, valid_offsets: Collection[int]) -> int:
    if not isinstance(offset, int):
        raise TypeError("offset must be an integer")
    if offset < 0 or offset & 0x3:
        raise ValueError("offset must be non-negative and 32-bit aligned")
    if offset not in valid_offsets:
        raise KeyError(f"unimplemented relative offset 0x{offset:02X}")
    return offset

def validate_wstrb(wstrb: int) -> int:
    if not isinstance(wstrb, int):
        raise TypeError("wstrb must be an integer")
    if not 0 <= wstrb <= 0xF:
        raise ValueError("wstrb must be a four-bit value")
    return wstrb

@dataclass(frozen=True)
class FuturePsPlatformBinding:
    xsa_path: Path | None = None
    system_base_address: int | None = None
    pl_clock_hz: int | None = None
    reset_source: str | None = None
    ps_axi_master_port: str | None = None
    interrupt_id: int | None = None

    def require_offline_unbound_state(self) -> None:
        if HARDWARE_EXECUTION_ALLOWED:
            raise RuntimeError("hardware execution must remain disabled")
        if self.system_base_address is not None:
            raise RuntimeError("base address requires tool-assigned real platform integration")
