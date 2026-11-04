"""Offline contract for the Milestone 4D production-integration candidate.

Offsets are relative to the AXI4-Lite slave.  This module intentionally has
no physical base address and no /dev/mem, UIO, PYNQ, or hardware backend.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum, IntFlag
import time
from typing import Callable, Protocol

from .milestone4_common import ADS1299_ID_EXPECTED, ADS1299_ID_MASK, ads1299_id_is_8ch, decode_signed24 as _decode_signed24
from .milestone4_registers import BaseControl as Control, BaseRegister as Register

HARDWARE_EXECUTION_ALLOWED = False
ARTIFACT_CLASS = "OFFLINE_INTEGRATION_CANDIDATE"
DOWNLOAD_GATE = "DO_NOT_DOWNLOAD"
PS_ADDRESS_MAP_STATUS = "PS_ADDRESS_MAP_NOT_FROZEN"
U1_STATUS = "U1_REPLACEMENT_PENDING"
class State(IntEnum):
    ARM_SAFE = 0
    STARTUP_TPOR_WAIT = 1
    VCAP_SETTLE_WAIT = 2
    RESET_PULSE_LOW = 3
    RESET_RELEASE_WAIT = 4
    SPI_PREPARE = 5
    ADS1_SDATAC = 6
    ADS1_ID_READ_1 = 7
    ADS1_ID_READ_2 = 8
    ADS1_ID_READ_3 = 9
    ADS2_SDATAC = 10
    ADS2_ID_READ_1 = 11
    ADS2_ID_READ_2 = 12
    ADS2_ID_READ_3 = 13
    ID_CONFIRM = 14
    CONFIG_WREG = 15
    CONFIG_RREG = 16
    RDATAC_ADS1 = 17
    RDATAC_ADS2 = 18
    START_ASSERT = 19
    WAIT_DRDY = 20
    FRAME_RECEIVE = 21
    TEST_DATA_WINDOW = 22
    EXIT_STOP = 23
    EXIT_SDATAC_ADS1 = 24
    EXIT_SDATAC_ADS2 = 25
    SHUTDOWN_SAFE = 26
    FAULT_SAFE = 27


class Error(IntFlag):
    ID_GATE = 0x0001
    SPI_TIMEOUT = 0x0002
    SPI_PROTOCOL = 0x0004
    ILLEGAL_CHIP_SELECT = 0x0008
    CONFIG_READBACK = 0x0010
    DRDY_PAIR = 0x0020
    PARTIAL_FRAME = 0x0040
    FRAME_LENGTH = 0x0080
    STATUS_HEADER = 0x0100
    DATA_WINDOW = 0x0200
    WATCHDOG = 0x0400
    ILLEGAL_STATE = 0x0800


class MMIO(Protocol):
    is_fake: bool

    def read32(self, offset: int) -> int: ...
    def write32(self, offset: int, value: int) -> None: ...


def unpack_ids(value: int) -> tuple[int, int, int]:
    value &= 0xFFFFFF
    return value & 0xFF, (value >> 8) & 0xFF, (value >> 16) & 0xFF


def id_is_expected(value: int) -> bool:
    return ads1299_id_is_8ch(value)


def decode_error_names(value: Error | int) -> tuple[str, ...]:
    errors = Error(value)
    return tuple(item.name for item in Error if errors & item)


def decode_signed24(value: int) -> int:
    return _decode_signed24(value & 0xFFFFFF)


def decode_channels(words: tuple[int, ...]) -> tuple[int, ...]:
    if len(words) != 6:
        raise ValueError("an ADS1299 frame requires six 32-bit channel words")
    packed = sum((word & 0xFFFFFFFF) << (32 * index)
                 for index, word in enumerate(words))
    return tuple(decode_signed24(packed >> (24 * (7 - index)))
                 for index in range(8))


@dataclass(frozen=True)
class Snapshot:
    state: State
    busy: bool
    id_gate_passed: bool
    config_passed: bool
    data_window_active: bool
    shutdown_safe: bool
    fault_safe: bool
    errors: Error
    ads1_ids: tuple[int, int, int]
    ads2_ids: tuple[int, int, int]
    ads1_config_pass: int
    ads2_config_pass: int
    complete_pairs: int
    discarded_frames: int
    partial_frames: int
    drdy_errors: int
    spi_errors: int
    ads1_status: int
    ads2_status: int
    data_window_summary: int
    shutdown_reason: int
    watchdog_count: int
    ads1_config_readbacks: tuple[int, ...]
    ads2_config_readbacks: tuple[int, ...]
    total_drdy_events: int
    frame_timeouts: int
    frame_length_errors: int
    status_header_errors: int
    valid_channel_positions: int
    accepted_event_count: int
    shutdown_request_seen: bool
    ads1_channels: tuple[int, ...]
    ads2_channels: tuple[int, ...]

    @property
    def ids_consistent(self) -> tuple[bool, bool]:
        return len(set(self.ads1_ids)) == 1, len(set(self.ads2_ids)) == 1

    @property
    def software_id_gate(self) -> bool:
        consistent = self.ids_consistent
        return (all(consistent) and
                all(id_is_expected(item)
                    for item in self.ads1_ids + self.ads2_ids))


def _bytes_from_words(words: tuple[int, int, int]) -> tuple[int, ...]:
    raw = b"".join(int(word & 0xFFFFFFFF).to_bytes(4, "little")
                   for word in words)
    return tuple(raw[:11])


class OfflineController:
    """Typed control workflow restricted to a fake MMIO backend."""

    def __init__(self, mmio: MMIO):
        if not getattr(mmio, "is_fake", False):
            raise RuntimeError("real MMIO is prohibited until PS integration is frozen")
        self.mmio = mmio

    def _issue_command(self, command: Control, timeout: float = 0.1,
                       poll_interval: float = 0.001) -> None:
        self.mmio.write32(Register.CONTROL, int(command))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.mmio.read32(Register.CONTROL) & int(command):
                return
            time.sleep(poll_interval)
        raise TimeoutError(
            f"offline AXI command acknowledgement timed out: {command.name}")

    def request_snapshot(self, timeout: float = 0.1,
                         poll_interval: float = 0.001) -> None:
        self._issue_command(Control.SNAPSHOT, timeout, poll_interval)

    def snapshot(self) -> Snapshot:
        self.request_snapshot()
        status = self.mmio.read32(Register.STATUS)
        detail = self.mmio.read32(Register.DETAIL_STATUS)
        event = self.mmio.read32(Register.EVENT_STATUS)
        reason = self.mmio.read32(Register.WATCHDOG_SHUTDOWN_REASON)
        a1_config = tuple(self.mmio.read32(Register.ADS1_CONFIG_0 + 4 * i)
                          for i in range(3))
        a2_config = tuple(self.mmio.read32(Register.ADS2_CONFIG_0 + 4 * i)
                          for i in range(3))
        a1_words = tuple(self.mmio.read32(Register.ADS1_CHANNEL_WORD0 + 4 * i)
                         for i in range(6))
        a2_words = tuple(self.mmio.read32(Register.ADS2_CHANNEL_WORD0 + 4 * i)
                         for i in range(6))
        return Snapshot(
            state=State(status & 0x1F), busy=bool(status & (1 << 5)),
            id_gate_passed=bool(status & (1 << 6)),
            config_passed=bool(status & (1 << 7)),
            data_window_active=bool(status & (1 << 8)),
            shutdown_safe=bool(status & (1 << 9)),
            fault_safe=bool(status & (1 << 10)),
            errors=Error(self.mmio.read32(Register.ERROR_FLAGS) & 0xFFFF),
            ads1_ids=unpack_ids(self.mmio.read32(Register.ADS1_ID_READS)),
            ads2_ids=unpack_ids(self.mmio.read32(Register.ADS2_ID_READS)),
            ads1_config_pass=self.mmio.read32(Register.ADS1_CONFIG_PASS) & 0x7FF,
            ads2_config_pass=self.mmio.read32(Register.ADS2_CONFIG_PASS) & 0x7FF,
            complete_pairs=self.mmio.read32(Register.COMPLETE_PAIR_COUNT),
            discarded_frames=self.mmio.read32(Register.DISCARDED_COUNT),
            partial_frames=self.mmio.read32(Register.PARTIAL_COUNT),
            drdy_errors=self.mmio.read32(Register.DRDY_ERROR_COUNT),
            spi_errors=self.mmio.read32(Register.SPI_ERROR_COUNT),
            ads1_status=self.mmio.read32(Register.ADS1_LAST_STATUS) & 0xFFFFFF,
            ads2_status=self.mmio.read32(Register.ADS2_LAST_STATUS) & 0xFFFFFF,
            data_window_summary=self.mmio.read32(Register.DATA_WINDOW_SUMMARY) & 0xFF,
            shutdown_reason=reason & 0xFF,
            watchdog_count=self.mmio.read32(Register.WATCHDOG_COUNT),
            ads1_config_readbacks=_bytes_from_words(a1_config),
            ads2_config_readbacks=_bytes_from_words(a2_config),
            total_drdy_events=self.mmio.read32(Register.TOTAL_DRDY_COUNT),
            frame_timeouts=self.mmio.read32(Register.FRAME_TIMEOUT_COUNT),
            frame_length_errors=self.mmio.read32(Register.FRAME_LENGTH_ERROR_COUNT),
            status_header_errors=self.mmio.read32(Register.STATUS_HEADER_ERROR_COUNT),
            valid_channel_positions=detail & 0xFFFF,
            accepted_event_count=event & 0x3,
            shutdown_request_seen=bool(event & 0x4),
            ads1_channels=decode_channels(a1_words),
            ads2_channels=decode_channels(a2_words),
        )

    def arm_start(self, timeout: float = 0.1,
                  poll_interval: float = 0.001) -> None:
        self._issue_command(Control.ARM_START, timeout, poll_interval)

    def software_shutdown(self, timeout: float = 1.0,
                          poll_interval: float = 0.001) -> Snapshot:
        self._issue_command(Control.SOFTWARE_SHUTDOWN, timeout, poll_interval)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = self.snapshot()
            if value.shutdown_safe or value.fault_safe:
                return value
            time.sleep(poll_interval)
        raise TimeoutError("offline shutdown did not reach a terminal safe state")


class FakeMMIO:
    """Deterministic fake backend; it never maps a physical address."""

    is_fake = True

    def __init__(self, registers: dict[int, int] | None = None,
                 on_control: Callable[[int, "FakeMMIO"], None] | None = None):
        self.registers = {int(key): int(value) & 0xFFFFFFFF
                          for key, value in (registers or {}).items()}
        self.on_control = on_control
        self.writes: list[tuple[int, int]] = []

    def read32(self, offset: int) -> int:
        return self.registers.get(int(offset), 0)

    def write32(self, offset: int, value: int) -> None:
        offset, value = int(offset), int(value) & 0xFFFFFFFF
        self.writes.append((offset, value))
        if offset == Register.CONTROL and self.on_control:
            self.on_control(value, self)

    def set_ids(self, ads1: tuple[int, int, int],
                ads2: tuple[int, int, int]) -> None:
        self.registers[int(Register.ADS1_ID_READS)] = (
            ads1[0] | ads1[1] << 8 | ads1[2] << 16)
        self.registers[int(Register.ADS2_ID_READS)] = (
            ads2[0] | ads2[1] << 8 | ads2[2] << 16)
