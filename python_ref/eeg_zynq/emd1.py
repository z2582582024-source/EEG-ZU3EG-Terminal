"""Strict finite EMD1 v1 wire format shared by file import and Linux ingress.

EMD1 has no token/consume fields and no measured per-frame clock. Timestamps
below are relative, derived from the design interval carried by each record.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from .frame import EegFrameV1, ErrorFlag, SourceType

FRAME_COUNT = 256
RECORD_BYTES = 96
RAW_BYTES = FRAME_COUNT * RECORD_BYTES
INTERVAL_NS = 4_096_000
DESIGN_RATE_HZ = 244.140625
ENCODED_RATE_MILLIHZ = 244_141
_RECORD = struct.Struct("<6I16i2I")


@dataclass(frozen=True, slots=True)
class Emd1Record:
    sequence: int
    batch_index: int
    status1: int
    status2: int
    samples: tuple[int, ...]

    def pack(self) -> bytes:
        return _RECORD.pack(0x31444D45, 0x00600001, self.sequence,
                            self.batch_index, self.status1, self.status2,
                            *self.samples, INTERVAL_NS, 1)

    def to_frame(self, source: SourceType) -> EegFrameV1:
        if source not in (SourceType.FILE_REPLAY, SourceType.ADS1299_DMA):
            raise ValueError("EMD1 ingress requires an explicit replay or DMA source")
        return EegFrameV1(self.sequence, self.batch_index * INTERVAL_NS,
                          ENCODED_RATE_MILLIHZ, source, 3, ErrorFlag.NONE,
                          self.status1, self.status2, self.samples)


def parse_finite_emd1(data: bytes) -> tuple[Emd1Record, ...]:
    """Reject partial, corrupt, duplicated or out-of-order finite packets."""
    if len(data) != RAW_BYTES:
        raise ValueError(f"EMD1 length {len(data)} != {RAW_BYTES}")
    result = []
    for index, values in enumerate(_RECORD.iter_unpack(data)):
        if values[:2] != (0x31444D45, 0x00600001):
            raise ValueError(f"EMD1 record {index}: magic/version/length")
        if values[2:4] != (index, index):
            raise ValueError(f"EMD1 record {index}: sequence/batch_index {values[2:4]}")
        if any(not 0 <= v <= 0xFFFFFF or v >> 20 != 0xC for v in values[4:6]):
            raise ValueError(f"EMD1 record {index}: ADS1299 status header")
        if any(not -(1 << 23) <= v < (1 << 23) for v in values[6:22]):
            raise ValueError(f"EMD1 record {index}: invalid signed int24 extension")
        if values[22:] != (INTERVAL_NS, 1):
            raise ValueError(f"EMD1 record {index}: design interval/flags")
        result.append(Emd1Record(index, index, *values[4:6], tuple(values[6:22])))
    return tuple(result)
