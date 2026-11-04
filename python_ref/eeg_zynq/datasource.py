"""Replaceable data-source interface shared by simulation, replay, and DMA."""

from __future__ import annotations

from typing import Iterable, Protocol, runtime_checkable

from .frame import SourceType


@runtime_checkable
class FrameSource(Protocol):
    """Minimal byte-record source contract for later DSP and Qt adapters."""

    @property
    def source_type(self) -> SourceType: ...

    @property
    def display_name(self) -> str: ...

    @property
    def is_real_hardware(self) -> bool: ...

    def records(self, frame_count: int) -> Iterable[bytes]: ...


@runtime_checkable
class RealtimeFrameSource(Protocol):
    """Non-blocking batch boundary shared by Qt simulation and future DMA.

    ``read_records`` must return promptly with at most ``max_records`` complete
    EegFrameV1 byte records.  It may return fewer records, including none.  A
    future DMA adapter belongs behind this protocol and is solely responsible
    for assigning verified ``ADS1299_DMA`` provenance.
    """

    @property
    def source_type(self) -> SourceType: ...

    @property
    def display_name(self) -> str: ...

    @property
    def is_real_hardware(self) -> bool: ...

    def read_records(self, max_records: int) -> Iterable[bytes]: ...

    def reset(self) -> None: ...
