"""Single-source relative register contract for Milestone 4D-4I."""

from __future__ import annotations

from enum import IntEnum, IntFlag


class BaseRegister(IntEnum):
    CONTROL=0x00; STATUS=0x04; ERROR_FLAGS=0x08; ADS1_ID_READS=0x0C
    ADS2_ID_READS=0x10; ADS1_CONFIG_PASS=0x14; ADS2_CONFIG_PASS=0x18
    COMPLETE_PAIR_COUNT=0x1C; DISCARDED_COUNT=0x20; PARTIAL_COUNT=0x24
    DRDY_ERROR_COUNT=0x28; SPI_ERROR_COUNT=0x2C; ADS1_LAST_STATUS=0x30
    ADS2_LAST_STATUS=0x34; DATA_WINDOW_SUMMARY=0x38
    WATCHDOG_SHUTDOWN_REASON=0x3C; ADS1_CONFIG_0=0x40; ADS1_CONFIG_1=0x44
    ADS1_CONFIG_2=0x48; ADS2_CONFIG_0=0x4C; ADS2_CONFIG_1=0x50
    ADS2_CONFIG_2=0x54; TOTAL_DRDY_COUNT=0x58; FRAME_TIMEOUT_COUNT=0x5C
    FRAME_LENGTH_ERROR_COUNT=0x60; STATUS_HEADER_ERROR_COUNT=0x64
    DETAIL_STATUS=0x68; EVENT_STATUS=0x6C; CDC_STATUS=0x70
    CAPABILITY=0x74; WATCHDOG_COUNT=0x78; ADS1_CHANNEL_WORD0=0x80
    ADS2_CHANNEL_WORD0=0x98


class BaseControl(IntFlag):
    ARM_START=1<<0; SOFTWARE_SHUTDOWN=1<<1; CLEAR_STICKY=1<<2; SNAPSHOT=1<<3


class FrameBufferRegister(IntEnum):
    FIFO_STATUS=0xB0; FIFO_OCCUPANCY=0xB4; FIFO_CAPACITY=0xB8
    PRODUCER_SEQUENCE=0xBC; CONSUMER_SEQUENCE=0xC0; OVERFLOW_COUNT=0xC4
    DROPPED_COUNT=0xC8; PARTIAL_PAIR_COUNT=0xCC; SEQUENCE_GAP_COUNT=0xD0
    LAST_ACCEPTED_SEQUENCE=0xD4; LAST_CONSUMED_SEQUENCE=0xD8
    SNAPSHOT_TOKEN=0xDC; FRAME_METADATA=0xE0; FRAME_ADS1_STATUS=0xE4
    FRAME_ADS2_STATUS=0xE8; FRAME_ADS1_WORD0=0xEC
    FRAME_ADS2_WORD0=0x10C; CONTROL=0x12C


class FrameBufferControl(IntFlag):
    SNAPSHOT=1<<0; CONSUME_ACK=1<<1; SOFTWARE_SHUTDOWN=1<<2
    FAULT_SAFE=1<<3; CLEAR_STICKY=1<<4


class FrameBufferStatusBits(IntFlag):
    EMPTY=1<<0; FULL=1<<1; ALMOST_FULL=1<<2; OVERFLOW=1<<3
    STOPPED=1<<4; SHUTDOWN_SAFE=1<<5; FAULT_SAFE=1<<6; SNAPSHOT_VALID=1<<7


REGISTER_DEFINITIONS = tuple(
    [(item.name, int(item), "RW" if item is BaseRegister.CONTROL else "RO", "4D")
     for item in BaseRegister]
    + [(item.name, int(item), "RW" if item is FrameBufferRegister.CONTROL else "RO", "4F")
       for item in FrameBufferRegister]
)


def validate_register_map() -> None:
    offsets=[item[1] for item in REGISTER_DEFINITIONS]
    if len(offsets) != len(set(offsets)):
        raise RuntimeError("relative register offsets overlap")
    if max(int(item) for item in BaseRegister) >= min(int(item) for item in FrameBufferRegister):
        raise RuntimeError("4D and 4F relative register ranges overlap")


validate_register_map()
