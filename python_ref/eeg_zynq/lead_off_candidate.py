"""Offline contract for the real ADS1299 lead-off adapter.

The adapter is intentionally separate from the frozen production DMA owner.
It consumes a controller-provided RREG snapshot and emits only SPI commands
that the reviewed ADS1299 state machine permits. It never estimates ohms.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import Enum
from typing import Mapping, Protocol

from .leadoff import (
    Ads1299Command,
    Ads1299Register,
    CONFIG4_LOFF_COMP_ENABLE,
    LeadOffConfiguration,
    LeadOffProtocolError,
    RegisterSnapshot,
    parse_lead_off_status_word,
)


class CandidateLeadOffState(Enum):
    UNAVAILABLE = "unavailable"
    READY = "ready"
    RUNNING = "running"
    COMPLETE = "complete"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class LeadOffHardwareContract:
    """Facts a future controller must prove before register writes."""

    exclusive_owner: bool
    continuous_quiesced: bool
    frame_boundary_safe: bool
    config_snapshot_valid: bool
    snapshot_sha256: str
    transport_identity: str

    def validate(self) -> None:
        if not self.exclusive_owner or not self.continuous_quiesced or not self.frame_boundary_safe:
            raise PermissionError("LEAD_OFF_QUIESCE_OR_EXCLUSIVE_OWNER_REQUIRED")
        if not self.config_snapshot_valid:
            raise PermissionError("LEAD_OFF_CONFIG_SNAPSHOT_REQUIRED")
        if len(self.snapshot_sha256) != 64 or any(c not in "0123456789abcdef" for c in self.snapshot_sha256):
            raise ValueError("snapshot_sha256 must be lowercase SHA256")
        if not self.transport_identity:
            raise ValueError("transport_identity is required")


@dataclass(frozen=True, slots=True)
class SpiCommand:
    adc: int
    command: Ads1299Command | None
    register: Ads1299Register | None
    value: int | None
    verify: bool = False
    decode_delay_cycles: int = 4
    wait_status_frames: int = 0


SNAPSHOT_REGISTERS = (
    Ads1299Register.CONFIG1, Ads1299Register.CONFIG2, Ads1299Register.CONFIG3,
    Ads1299Register.CH1SET, Ads1299Register.CH2SET, Ads1299Register.CH3SET,
    Ads1299Register.CH4SET, Ads1299Register.CH5SET, Ads1299Register.CH6SET,
    Ads1299Register.CH7SET, Ads1299Register.CH8SET, Ads1299Register.BIAS_SENSP,
    Ads1299Register.BIAS_SENSN, Ads1299Register.MISC1, Ads1299Register.LOFF,
    Ads1299Register.LOFF_SENSP, Ads1299Register.LOFF_SENSN,
    Ads1299Register.LOFF_FLIP, Ads1299Register.CONFIG4,
)


class LeadOffControllerTransport(Protocol):
    """Future PL/driver bridge. No socket, COM, or target access is implied."""

    def execute(self, command: SpiCommand) -> int | None: ...

    def status_words(self) -> Mapping[int, tuple[int, ...]]: ...


def snapshot_digest(snapshot: Mapping[int, Mapping[Ads1299Register, int]]) -> str:
    rows = []
    for adc in sorted(snapshot):
        values = snapshot[adc]
        if isinstance(values, RegisterSnapshot):
            values = values.values
        rows.append((adc, tuple(sorted((int(reg), value) for reg, value in values.items()))))
    return hashlib.sha256(json.dumps(rows, separators=(",", ":")).encode("ascii")).hexdigest()


def command_plan(
    snapshot: Mapping[int, Mapping[Ads1299Register, int]],
    configuration: LeadOffConfiguration | None = None,
) -> tuple[SpiCommand, ...]:
    """Build the reviewed SDATAC/configure/status-cleanup/restore plan."""

    config = configuration or LeadOffConfiguration()
    if tuple(sorted(snapshot)) != (1, 2):
        raise ValueError("lead-off requires ADS1 and ADS2 snapshots")
    for adc in (1, 2):
        if set(snapshot[adc]) != set(SNAPSHOT_REGISTERS):
            raise ValueError(f"ADS{adc} must provide the complete 19-register EEG snapshot")
        if any(not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 0xff
               for value in snapshot[adc].values()):
            raise ValueError(f"ADS{adc} snapshot values must be bytes")
    commands: list[SpiCommand] = []
    for adc in (1, 2):
        commands.append(SpiCommand(adc, Ads1299Command.SDATAC, None, None))
    for adc in (1, 2):
        if not snapshot[adc][Ads1299Register.MISC1] & 0x20:
            raise LeadOffProtocolError(f"ADS{adc} SRB1 is not enabled in snapshot")
        if len(SNAPSHOT_REGISTERS) != 19:
            raise AssertionError("snapshot register contract drift")
        values = (
            (Ads1299Register.LOFF, config.loff_value),
            (Ads1299Register.LOFF_SENSP, config.positive_mask),
            (Ads1299Register.LOFF_SENSN, config.negative_mask_for_adc(adc)),
            (Ads1299Register.LOFF_FLIP, 0),
            (Ads1299Register.CONFIG4,
             snapshot[adc][Ads1299Register.CONFIG4] | CONFIG4_LOFF_COMP_ENABLE),
        )
        for register, value in values:
            commands.append(SpiCommand(adc, None, register, value))
            commands.append(SpiCommand(adc, None, register, value, verify=True))
    for adc in (1, 2):
        commands.append(SpiCommand(adc, Ads1299Command.RDATAC, None, None))
    commands.append(SpiCommand(0, None, None, None,
                               decode_delay_cycles=0,
                               wait_status_frames=config.required_status_samples))
    for adc in (1, 2):
        commands.append(SpiCommand(adc, Ads1299Command.SDATAC, None, None))
    for adc in (1, 2):
        for register, value in (
            (Ads1299Register.LOFF_SENSP, 0),
            (Ads1299Register.LOFF_SENSN, 0),
            (Ads1299Register.CONFIG4,
             snapshot[adc][Ads1299Register.CONFIG4] & ~CONFIG4_LOFF_COMP_ENABLE),
        ):
            commands.append(SpiCommand(adc, None, register, value))
            commands.append(SpiCommand(adc, None, register, value, verify=True))
    for adc in (1, 2):
        for register in SNAPSHOT_REGISTERS:
            value = snapshot[adc][register]
            commands.append(SpiCommand(adc, None, register, value))
            commands.append(SpiCommand(adc, None, register, value, verify=True))
    for adc in (1, 2):
        commands.append(SpiCommand(adc, Ads1299Command.RDATAC, None, None))
    return tuple(commands)


def classify_status_words(words: Mapping[int, tuple[int, ...]]) -> dict[str, object]:
    """Validate fresh status words and return masks only, with no ohm estimate."""

    if tuple(sorted(words)) != (1, 2) or not words[1] or not words[2]:
        raise LeadOffProtocolError("LEAD_OFF_STATUS_INCOMPLETE")
    parsed = {adc: tuple(parse_lead_off_status_word(word) for word in values)
              for adc, values in words.items()}
    if any(not values for values in parsed.values()):
        raise LeadOffProtocolError("LEAD_OFF_STATUS_INCOMPLETE")
    return {
        "adc1_positive_off_mask": _or_masks(parsed[1], "positive_mask"),
        "adc1_negative_off_mask": _or_masks(parsed[1], "negative_mask"),
        "adc2_positive_off_mask": _or_masks(parsed[2], "positive_mask"),
        "adc2_negative_off_mask": _or_masks(parsed[2], "negative_mask"),
        "samples_adc1": len(parsed[1]),
        "samples_adc2": len(parsed[2]),
        "equivalent_impedance_ohms": None,
        "source": "ADS1299_COMPARATOR_STATUS_ONLY; CALIBRATION_REQUIRED",
    }


def _or_masks(rows: tuple[object, ...], field: str) -> int:
    result = 0
    for row in rows:
        result |= int(getattr(row, field))
    return result
