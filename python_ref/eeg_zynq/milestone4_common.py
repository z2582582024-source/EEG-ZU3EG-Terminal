"""Shared Milestone 4D-4I digital data definitions.

This module contains no analog scaling and no physical MMIO address. It is
the unique software implementation of the ADS1299 ID predicate, signed 24-bit
codec, and logical 16-channel order used by the offline integration chain.
"""

from __future__ import annotations

from typing import Sequence

SIGNED24_BITS = 24
SIGNED24_MASK = (1 << SIGNED24_BITS) - 1
SIGNED24_MIN = -(1 << (SIGNED24_BITS - 1))
SIGNED24_MAX = (1 << (SIGNED24_BITS - 1)) - 1
ADS1299_ID_MASK = 0x1F
ADS1299_ID_EXPECTED = 0x1E
SYSTEM_AXI_BASE_ADDRESS = None
ADS_CHANNEL_ORDER = tuple(
    [f"ADS1_CH{index}" for index in range(1, 9)]
    + [f"ADS2_CH{index}" for index in range(1, 9)]
)


def decode_signed24(value: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError("signed24 value must be an integer")
    if not 0 <= value <= SIGNED24_MASK:
        raise ValueError("signed24 value must fit 24 bits")
    return value - (1 << SIGNED24_BITS) if value & (1 << 23) else value


def encode_signed24(value: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError("signed24 value must be an integer")
    if not SIGNED24_MIN <= value <= SIGNED24_MAX:
        raise ValueError("signed24 value is outside the representable range")
    return value & SIGNED24_MASK


def encode_signed24_bytes(value: int) -> bytes:
    return encode_signed24(value).to_bytes(3, "big")


def ads1299_id_is_8ch(value: int) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and 0 <= value <= 0xFF
        and (value & ADS1299_ID_MASK) == ADS1299_ID_EXPECTED
    )


def triple_id_gate_passed(values: Sequence[int]) -> bool:
    group = tuple(values)
    return len(group) == 3 and len(set(group)) == 1 and all(
        ads1299_id_is_8ch(value) for value in group
    )


def dual_id_gate_passed(ads1_ids: Sequence[int], ads2_ids: Sequence[int]) -> bool:
    return triple_id_gate_passed(ads1_ids) and triple_id_gate_passed(ads2_ids)
