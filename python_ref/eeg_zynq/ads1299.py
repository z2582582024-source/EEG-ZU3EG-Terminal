"""ADS1299 216-bit data-frame codec used as the CPU golden reference.

The wire frame is big-endian/MSB-first: one 24-bit status word followed by
eight signed 24-bit channel samples.  It does not contain a sequence number
or CRC; those are project transport-layer fields defined in ``frame.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from .milestone4_common import SIGNED24_MAX, SIGNED24_MIN, decode_signed24, encode_signed24_bytes

ADS1299_CHANNELS = 8
ADS1299_WORD_BYTES = 3
ADS1299_FRAME_BYTES = (1 + ADS1299_CHANNELS) * ADS1299_WORD_BYTES
STATUS_HEADER_NIBBLE = 0xC


class Ads1299FrameError(ValueError):
    """Base class for malformed ADS1299 frames."""


class FrameLengthError(Ads1299FrameError):
    """The ADS1299 frame is not exactly 27 bytes."""


class StatusHeaderError(Ads1299FrameError):
    """The status word does not start with the required 0b1100 nibble."""


def signed24_to_int(raw_value: int) -> int:
    """Sign-extend an unsigned 24-bit value to a Python integer."""

    return decode_signed24(raw_value)


def int_to_signed24_bytes(value: int) -> bytes:
    """Encode a signed integer as one big-endian 24-bit two's-complement word."""

    return encode_signed24_bytes(value)


@dataclass(frozen=True, slots=True)
class Ads1299Frame:
    """A lossless parsed representation of one ADS1299 data frame."""

    raw: bytes
    status: int
    channels: tuple[int, ...]

    def __post_init__(self) -> None:
        if len(self.raw) != ADS1299_FRAME_BYTES:
            raise FrameLengthError(
                f"ADS1299 frame must be {ADS1299_FRAME_BYTES} bytes, got {len(self.raw)}"
            )
        if not 0 <= self.status <= 0xFFFFFF:
            raise ValueError("status must be an unsigned 24-bit value")
        if len(self.channels) != ADS1299_CHANNELS:
            raise ValueError(f"expected {ADS1299_CHANNELS} channels")
        for sample in self.channels:
            if not SIGNED24_MIN <= sample <= SIGNED24_MAX:
                raise ValueError(f"channel sample out of signed 24-bit range: {sample}")

    @property
    def status_header_valid(self) -> bool:
        return (self.status >> 20) == STATUS_HEADER_NIBBLE

    @property
    def loff_statp(self) -> int:
        return (self.status >> 12) & 0xFF

    @property
    def loff_statn(self) -> int:
        return (self.status >> 4) & 0xFF

    @property
    def gpio(self) -> int:
        return self.status & 0x0F


def parse_ads1299_frame(
    raw: bytes | bytearray | memoryview, *, validate_status_header: bool = True
) -> Ads1299Frame:
    """Parse one 27-byte ADS1299 frame.

    ``validate_status_header=False`` is intended only for fault-injection and
    diagnostics, where the invalid status still needs to be preserved.
    """

    data = bytes(raw)
    if len(data) != ADS1299_FRAME_BYTES:
        raise FrameLengthError(
            f"ADS1299 frame must be {ADS1299_FRAME_BYTES} bytes, got {len(data)}"
        )

    status = int.from_bytes(data[0:3], "big")
    if validate_status_header and (status >> 20) != STATUS_HEADER_NIBBLE:
        raise StatusHeaderError(
            f"status header must be 0xC, got 0x{status >> 20:X} (status=0x{status:06X})"
        )

    channels = tuple(
        signed24_to_int(int.from_bytes(data[offset : offset + 3], "big"))
        for offset in range(3, ADS1299_FRAME_BYTES, 3)
    )
    return Ads1299Frame(raw=data, status=status, channels=channels)


def build_ads1299_frame(channels: Sequence[int], *, status: int = 0xC00000) -> bytes:
    """Build one raw ADS1299 frame from a status word and eight samples."""

    if len(channels) != ADS1299_CHANNELS:
        raise ValueError(f"expected {ADS1299_CHANNELS} channels, got {len(channels)}")
    if not 0 <= status <= 0xFFFFFF:
        raise ValueError("status must be an unsigned 24-bit value")

    payload = bytearray(status.to_bytes(ADS1299_WORD_BYTES, "big"))
    for sample in channels:
        payload.extend(int_to_signed24_bytes(sample))
    return bytes(payload)
