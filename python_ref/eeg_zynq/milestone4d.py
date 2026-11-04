"""Software/Qt-facing contract for the Milestone 4D preparation build.

This module does not authorize hardware execution.  It centralizes the frozen
ID gate, state/error meanings, and ILA result interpretation so later Python
and Qt integration cannot silently accept the existing U1 value 0x3C.
"""

from dataclasses import dataclass
from enum import IntEnum, IntFlag

from .milestone4_common import (
    ADS1299_ID_EXPECTED,
    ADS1299_ID_MASK,
    ads1299_id_is_8ch,
    dual_id_gate_passed,
)
TEST_CONFIG2_VALUE = 0xD0
HARDWARE_EXECUTION_ALLOWED = False


class Milestone4DState(IntEnum):
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
    ADS1_WREG_TEST_CFG = 15
    ADS1_RREG_TEST_CFG = 16
    ADS2_WREG_TEST_CFG = 17
    ADS2_RREG_TEST_CFG = 18
    TEST_DATA_WINDOW = 19
    SHUTDOWN_SAFE = 20
    FAULT_SAFE = 21


class Milestone4DError(IntFlag):
    ID_MISMATCH = 0x01
    SPI_TIMEOUT = 0x02
    DUAL_CS = 0x04
    REGISTER_READBACK = 0x08
    TEST_WINDOW = 0x10
    INVALID_STATE = 0x20
    INCOMPLETE_ID = 0x40
    PROTOCOL = 0x80


def is_ads1299_8ch(device_id: int) -> bool:
    """Apply the frozen TI-supported eight-channel identity criterion."""
    return ads1299_id_is_8ch(device_id)


def unpack_three_ids(packed: int) -> tuple[int, int, int]:
    """Decode {read_3, read_2, read_1} into chronological order."""
    value = int(packed) & 0xFFFFFF
    return value & 0xFF, (value >> 8) & 0xFF, (value >> 16) & 0xFF


@dataclass(frozen=True)
class Milestone4DSnapshot:
    state: Milestone4DState
    error_flags: Milestone4DError
    ads1_ids: tuple[int, int, int]
    ads2_ids: tuple[int, int, int]
    config_status: int
    config_readback: tuple[int, int]
    shutdown_safe: bool
    fault_safe: bool

    @property
    def dual_id_gate_passed(self) -> bool:
        return dual_id_gate_passed(self.ads1_ids, self.ads2_ids)

    @property
    def configuration_verified(self) -> bool:
        return (
            self.config_status == 0xF
            and self.config_readback == (TEST_CONFIG2_VALUE, TEST_CONFIG2_VALUE)
            and not (self.error_flags & Milestone4DError.REGISTER_READBACK)
        )
