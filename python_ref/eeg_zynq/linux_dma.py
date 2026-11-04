"""Explicit Linux character-device ingress behind RealtimeFrameSource.

Construction/import are inert. start_once() is a real hardware operation for a
future separately authorized Linux session; regular files and FILE_REPLAY are
never accepted here. No socket, debugger, /dev/mem, or bare-metal RX address.
"""

from __future__ import annotations

import os
import stat
import struct
import sys
import time
import zlib
from collections import deque
from pathlib import Path

from .emd1 import DESIGN_RATE_HZ, RAW_BYTES, parse_finite_emd1
from .frame import SourceType

INFO = struct.Struct("<8I")
STATUS = struct.Struct("<4Ii27I")
IOC_START = (ord("E") << 8) | 0x10
IOC_INFO = (2 << 30) | (INFO.size << 16) | (ord("E") << 8) | 0x11
IOC_STOP = (ord("E") << 8) | 0x12
IOC_STATUS = (2 << 30) | (STATUS.size << 16) | (ord("E") << 8) | 0x13
EXPECTED_INFO = (0x314C4445, 1, 32, 0x80000000, 0x80010000, 256, 96, 4096000)


class LinuxDmaError(RuntimeError):
    """Latched finite-session error, never retried or replaced with old data."""


def validate_terminal(status: tuple[int, ...], raw: bytes) -> None:
    if len(status) != 32 or status[:4] != (0x31534445, 1, 128, 2):
        raise LinuxDmaError("driver status identity/terminal state rejected")
    if (status[4] or status[6] != 31 or status[7:12] != (1, 0, 1, 1, RAW_BYTES)
            or status[13] != 256 or status[16] != RAW_BYTES or status[22] != 0):
        raise LinuxDmaError(f"driver integrity/ownership/safe terminal rejected: {status}")
    if (status[14] != 0x0b or not status[15] & 2 or status[15] & 0x770 or status[18] or status[20]
            or status[19] & 0x1fb != 0x31 or status[17] & 0xc0 != 0xc0
            or status[17] & 0x600 != 0x200):
        raise LinuxDmaError("driver DMA/core/FIFO state rejected")
    if len(raw) != RAW_BYTES or zlib.crc32(raw) & 0xFFFFFFFF != status[12]:
        raise LinuxDmaError("driver raw length/CRC disagrees with userspace")


class LinuxFiniteDmaSource:
    """Non-blocking, single finite batch. reset() never starts another batch.

    A kernel worker does the bounded capture and releases the DMA mapping only
    after completion/confirmed termination. This adapter publishes records only
    after the entire returned packet and terminal status pass validation.
    """

    design_sample_rate_hz = DESIGN_RATE_HZ

    def __init__(self, device: str | Path) -> None:
        self.device = os.fspath(device)
        self._fd: int | None = None
        self._attempted = False
        self._start_sent = False
        self._stop_sent = False
        self._verified_device = False
        self._closed = False
        self._published = False
        self._raw = bytearray()
        self._records: deque[bytes] = deque()
        self._deadline = 0.0
        self.failure: str | None = None
        self.cleanup_error: str | None = None
        self.last_status: tuple[int, ...] | None = None

    @property
    def source_type(self) -> SourceType:
        return SourceType.ADS1299_DMA

    @property
    def display_name(self) -> str:
        return "Linux有限DMA内部测试 · 设计时基 · 非真人脑电"

    @property
    def is_real_hardware(self) -> bool:
        return self._verified_device and self._start_sent and not self._closed

    @property
    def finished(self) -> bool:
        return self._published and not self._records

    def _ioctl(self, request: int, size: int = 0) -> bytes:
        import fcntl  # Linux only; not imported during default/import/help.
        assert self._fd is not None
        if size:
            buffer = bytearray(size)
            fcntl.ioctl(self._fd, request, buffer, True)
            return bytes(buffer)
        fcntl.ioctl(self._fd, request)
        return b""

    def start_once(self) -> None:
        if self._attempted or self._closed:
            raise LinuxDmaError("one start attempt per source; no retry/reopen")
        self._attempted = True
        try:
            if not sys.platform.startswith("linux") or sys.byteorder != "little":
                raise LinuxDmaError("requires actual little-endian Linux and the matched driver")
            self._fd = os.open(self.device, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC | os.O_NOFOLLOW)
            if not stat.S_ISCHR(os.fstat(self._fd).st_mode):
                raise LinuxDmaError("regular files are FILE_REPLAY, not a DMA device")
            if INFO.unpack(self._ioctl(IOC_INFO, INFO.size)) != EXPECTED_INFO:
                raise LinuxDmaError("kernel UAPI/address/frame contract mismatch")
            self._verified_device = True
            self._deadline = time.monotonic() + 8.0
            self._start_sent = True  # consume before sending; error is uncertain.
            self._ioctl(IOC_START)
        except (OSError, ValueError, LinuxDmaError) as exc:
            self._fail(exc)

    def _request_stop(self) -> None:
        if self._fd is not None and self._start_sent and not self._stop_sent and not self._published:
            self._stop_sent = True
            try:
                self._ioctl(IOC_STOP)
            except OSError as exc:
                self.cleanup_error = f"{self.cleanup_error}; {exc}" if self.cleanup_error else str(exc)

    def _fail(self, exc: Exception) -> None:
        if self.failure is None:
            self.failure = str(exc)
        self._records.clear()
        if self._fd is not None and self._verified_device and self._start_sent and self.last_status is None:
            try:
                # GET_STATUS copies the kernel's saved result; it performs no MMIO,
                # DMA, retry, or new target query, including after a failed read.
                self.last_status = STATUS.unpack(self._ioctl(IOC_STATUS, STATUS.size))
            except (OSError, ValueError, struct.error) as diagnostic_error:
                self.cleanup_error = f"cached status unavailable: {diagnostic_error}"
        self._request_stop()
        self.close()
        raise LinuxDmaError(self.failure) from exc

    def read_records(self, max_records: int) -> tuple[bytes, ...]:
        if not isinstance(max_records, int) or isinstance(max_records, bool):
            raise TypeError("max_records must be an integer")
        if max_records < 0:
            raise ValueError("max_records cannot be negative")
        if self.failure:
            raise LinuxDmaError(self.failure)
        if self._closed or not self._start_sent:
            raise LinuxDmaError("source is not active; explicit start_once required")
        if max_records == 0:
            return ()
        if not self._published:
            try:
                if time.monotonic() >= self._deadline:
                    raise LinuxDmaError("finite capture deadline; no retry")
                try:
                    block = os.read(self._fd, RAW_BYTES - len(self._raw))
                except BlockingIOError:
                    return ()
                if not block:
                    raise LinuxDmaError(f"incomplete EMD1 packet: {len(self._raw)} bytes")
                self._raw.extend(block)
                if len(self._raw) < RAW_BYTES:
                    return ()
                raw = bytes(self._raw)
                self.last_status = STATUS.unpack(self._ioctl(IOC_STATUS, STATUS.size))
                validate_terminal(self.last_status, raw)
                records = parse_finite_emd1(raw)
                self._records.extend(r.to_frame(SourceType.ADS1299_DMA).pack() for r in records)
                self._published = True
            except (OSError, ValueError, LinuxDmaError) as exc:
                self._fail(exc)
        result = tuple(self._records.popleft() for _ in range(min(max_records, len(self._records))))
        return result

    def reset(self) -> None:
        raise LinuxDmaError("finite DMA cannot restart; new separately authorized session required")

    def close(self) -> None:
        if not self._closed:
            self._request_stop()
            if self._fd is not None:
                fd, self._fd = self._fd, None
                try:
                    os.close(fd)  # release requests cancellation; not a hardware-halt receipt
                except OSError as exc:
                    self.cleanup_error = f"{self.cleanup_error}; {exc}" if self.cleanup_error else str(exc)
            self._closed = True
