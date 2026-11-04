"""ADS1299 lead-off register protocol, recovery state machine, and simulation.

The register definitions and command values in this module follow TI
SBAS499C (ADS1299 Rev. C), sections 9.3.2.4.3, 9.3.2.4.4, 9.5.3,
9.6.1.4-9.6.1.17, and application section 10.1.2.1.  This module reports
comparator contact states only.  It does not calculate or claim electrode
impedance.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, IntEnum
from types import MappingProxyType
from typing import Mapping, Protocol

from .electrodes import BIAS_ELECTRODE, ELECTRODE_CHANNELS, REFERENCE_ELECTRODE


class Ads1299Command(IntEnum):
    NOP = 0x00
    WAKEUP = 0x02
    STANDBY = 0x04
    RESET = 0x06
    START = 0x08
    STOP = 0x0A
    RDATAC = 0x10
    SDATAC = 0x11
    RDATA = 0x12


class Ads1299Register(IntEnum):
    ID = 0x00
    CONFIG1 = 0x01
    CONFIG2 = 0x02
    CONFIG3 = 0x03
    LOFF = 0x04
    CH1SET = 0x05
    CH2SET = 0x06
    CH3SET = 0x07
    CH4SET = 0x08
    CH5SET = 0x09
    CH6SET = 0x0A
    CH7SET = 0x0B
    CH8SET = 0x0C
    BIAS_SENSP = 0x0D
    BIAS_SENSN = 0x0E
    LOFF_SENSP = 0x0F
    LOFF_SENSN = 0x10
    LOFF_FLIP = 0x11
    LOFF_STATP = 0x12
    LOFF_STATN = 0x13
    GPIO = 0x14
    MISC1 = 0x15
    MISC2 = 0x16
    CONFIG4 = 0x17


CONFIG3_RESERVED_MASK = 0x60
CONFIG3_RESERVED_VALUE = 0x60
CONFIG3_BIAS_MEAS = 0x10
CONFIG3_BIASREF_INT = 0x08
CONFIG3_PD_BIAS = 0x04
CONFIG3_BIAS_LOFF_SENS = 0x02
CONFIG3_BIAS_STAT = 0x01

LOFF_COMP_TH_MASK = 0xE0
LOFF_RESERVED_MASK = 0x10
LOFF_CURRENT_MASK = 0x0C
LOFF_FREQUENCY_MASK = 0x03

MISC1_SRB1 = 0x20
MISC1_RESERVED_MASK = 0xDF

CONFIG4_SINGLE_SHOT = 0x08
# TI SBAS499C Table 28 and Application 10.1.2.1 define bit 1 = 1 as enabled.
CONFIG4_LOFF_COMP_ENABLE = 0x02
CONFIG4_PD_LOFF_COMP = CONFIG4_LOFF_COMP_ENABLE
CONFIG4_RESERVED_MASK = 0xF5

ADS1299_STATUS_HEADER = 0xC
T_SDECODE_CLOCKS = 4


class ComparatorThreshold(IntEnum):
    P95_N5 = 0
    P92_5_N7_5 = 1
    P90_N10 = 2
    P87_5_N12_5 = 3
    P85_N15 = 4
    P80_N20 = 5
    P75_N25 = 6
    P70_N30 = 7


class LeadOffCurrent(IntEnum):
    NA_6 = 0
    NA_24 = 1
    UA_6 = 2
    UA_24 = 3


class LeadOffFrequency(IntEnum):
    DC = 0
    AC_7_8_HZ = 1
    AC_31_2_HZ = 2
    DATA_RATE_DIV_4 = 3


class RegisterAccessError(RuntimeError):
    """A register access violates the modeled ADS1299 protocol."""


class ReservedBitError(RegisterAccessError):
    """A write attempts to change a documented reserved bit."""


class LeadOffProtocolError(RuntimeError):
    """A lead-off transaction, status word, or recovery operation failed."""


@dataclass(frozen=True, slots=True)
class RegisterSpec:
    address: Ads1299Register
    reset: int
    writable_mask: int
    reserved_mask: int = 0
    reserved_value: int = 0
    read_only_mask: int = 0

    def validate_write(self, value: int, current: int) -> None:
        if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 0xFF:
            raise RegisterAccessError("register value must be an unsigned byte")
        if self.writable_mask == 0:
            raise RegisterAccessError(f"{self.address.name} is read-only")
        if value & self.reserved_mask != self.reserved_value:
            raise ReservedBitError(
                f"{self.address.name} reserved bits must be "
                f"0x{self.reserved_value:02X} within mask 0x{self.reserved_mask:02X}"
            )
        changed_read_only = (value ^ current) & self.read_only_mask
        if changed_read_only:
            raise RegisterAccessError(
                f"{self.address.name} write changes read-only mask 0x{changed_read_only:02X}"
            )


REGISTER_SPECS = MappingProxyType(
    {
        Ads1299Register.CONFIG3: RegisterSpec(
            Ads1299Register.CONFIG3,
            reset=0x60,
            writable_mask=0xFE,
            reserved_mask=CONFIG3_RESERVED_MASK,
            reserved_value=CONFIG3_RESERVED_VALUE,
            read_only_mask=CONFIG3_BIAS_STAT,
        ),
        Ads1299Register.LOFF: RegisterSpec(
            Ads1299Register.LOFF,
            reset=0x00,
            writable_mask=0xEF,
            reserved_mask=LOFF_RESERVED_MASK,
        ),
        Ads1299Register.BIAS_SENSP: RegisterSpec(
            Ads1299Register.BIAS_SENSP, reset=0x00, writable_mask=0xFF
        ),
        Ads1299Register.BIAS_SENSN: RegisterSpec(
            Ads1299Register.BIAS_SENSN, reset=0x00, writable_mask=0xFF
        ),
        Ads1299Register.LOFF_SENSP: RegisterSpec(
            Ads1299Register.LOFF_SENSP, reset=0x00, writable_mask=0xFF
        ),
        Ads1299Register.LOFF_SENSN: RegisterSpec(
            Ads1299Register.LOFF_SENSN, reset=0x00, writable_mask=0xFF
        ),
        Ads1299Register.LOFF_FLIP: RegisterSpec(
            Ads1299Register.LOFF_FLIP, reset=0x00, writable_mask=0xFF
        ),
        Ads1299Register.LOFF_STATP: RegisterSpec(
            Ads1299Register.LOFF_STATP, reset=0x00, writable_mask=0x00
        ),
        Ads1299Register.LOFF_STATN: RegisterSpec(
            Ads1299Register.LOFF_STATN, reset=0x00, writable_mask=0x00
        ),
        Ads1299Register.MISC1: RegisterSpec(
            Ads1299Register.MISC1,
            reset=0x00,
            writable_mask=MISC1_SRB1,
            reserved_mask=MISC1_RESERVED_MASK,
        ),
        Ads1299Register.CONFIG4: RegisterSpec(
            Ads1299Register.CONFIG4,
            reset=0x00,
            writable_mask=CONFIG4_SINGLE_SHOT | CONFIG4_PD_LOFF_COMP,
            reserved_mask=CONFIG4_RESERVED_MASK,
        ),
    }
)

WRITABLE_CONFIGURATION_REGISTERS = tuple(
    register for register, spec in REGISTER_SPECS.items() if spec.writable_mask
)


@dataclass(frozen=True, slots=True)
class RegisterSnapshot:
    values: Mapping[Ads1299Register, int]

    def __post_init__(self) -> None:
        object.__setattr__(self, "values", MappingProxyType(dict(self.values)))


class Ads1299RegisterMirror:
    """One ADS1299 software register mirror with reserved/read-only protection."""

    def __init__(self) -> None:
        self._values = {register: spec.reset for register, spec in REGISTER_SPECS.items()}

    def read(self, register: Ads1299Register | int) -> int:
        try:
            address = Ads1299Register(register)
            return self._values[address]
        except (ValueError, KeyError) as exc:
            raise RegisterAccessError(f"unsupported ADS1299 register: 0x{int(register):02X}") from exc

    def write(self, register: Ads1299Register | int, value: int) -> None:
        try:
            address = Ads1299Register(register)
            spec = REGISTER_SPECS[address]
        except (ValueError, KeyError) as exc:
            raise RegisterAccessError(f"unsupported ADS1299 register: 0x{int(register):02X}") from exc
        current = self._values[address]
        spec.validate_write(value, current)
        self._values[address] = value

    def update_status(self, register: Ads1299Register, value: int) -> None:
        spec = REGISTER_SPECS[register]
        if spec.writable_mask:
            raise RegisterAccessError(f"{register.name} is not a read-only status register")
        if not 0 <= value <= 0xFF:
            raise RegisterAccessError("status register value must be an unsigned byte")
        self._values[register] = value

    def update_bias_status(self, disconnected: bool) -> None:
        value = self._values[Ads1299Register.CONFIG3]
        self._values[Ads1299Register.CONFIG3] = (
            value | CONFIG3_BIAS_STAT if disconnected else value & ~CONFIG3_BIAS_STAT
        )

    def snapshot(self) -> RegisterSnapshot:
        return RegisterSnapshot(
            {register: self._values[register] for register in WRITABLE_CONFIGURATION_REGISTERS}
        )

    def restore(self, snapshot: RegisterSnapshot) -> None:
        expected = set(WRITABLE_CONFIGURATION_REGISTERS)
        if set(snapshot.values) != expected:
            raise RegisterAccessError("register snapshot does not cover the modeled writable set")
        for register in WRITABLE_CONFIGURATION_REGISTERS:
            value = snapshot.values[register]
            if register is Ads1299Register.CONFIG3:
                value = (value & ~CONFIG3_BIAS_STAT) | (
                    self._values[register] & CONFIG3_BIAS_STAT
                )
            self.write(register, value)


@dataclass(frozen=True, slots=True)
class ParsedLeadOffStatus:
    raw: int
    positive_mask: int
    negative_mask: int
    gpio: int


def parse_lead_off_status_word(status: int) -> ParsedLeadOffStatus:
    if not isinstance(status, int) or isinstance(status, bool) or not 0 <= status <= 0xFFFFFF:
        raise LeadOffProtocolError("ADS1299 status must be an unsigned 24-bit word")
    if status >> 20 != ADS1299_STATUS_HEADER:
        raise LeadOffProtocolError(
            f"ADS1299 status header must be 0xC, got 0x{status >> 20:X}"
        )
    return ParsedLeadOffStatus(
        raw=status,
        positive_mask=(status >> 12) & 0xFF,
        negative_mask=(status >> 4) & 0xFF,
        gpio=status & 0x0F,
    )


@dataclass(frozen=True, slots=True)
class LeadOffConfiguration:
    comparator_threshold: ComparatorThreshold = ComparatorThreshold.P95_N5
    current: LeadOffCurrent = LeadOffCurrent.NA_6
    frequency: LeadOffFrequency = LeadOffFrequency.DC
    positive_mask: int = 0xFF
    reference_adc_index: int = 1
    reference_input_index: int = 1
    required_status_samples: int = 5
    wait_ready_poll_limit: int = 8
    status_poll_limit: int = 16

    def __post_init__(self) -> None:
        if not 0 <= self.positive_mask <= 0xFF:
            raise ValueError("positive_mask must be an unsigned byte")
        if self.reference_adc_index not in (1, 2):
            raise ValueError("reference_adc_index must be 1 or 2")
        if not 1 <= self.reference_input_index <= 8:
            raise ValueError("reference_input_index must be 1..8")
        for name, value in (
            ("required_status_samples", self.required_status_samples),
            ("wait_ready_poll_limit", self.wait_ready_poll_limit),
            ("status_poll_limit", self.status_poll_limit),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")

    @property
    def loff_value(self) -> int:
        return (
            (int(self.comparator_threshold) << 5)
            | (int(self.current) << 2)
            | int(self.frequency)
        )

    def negative_mask_for_adc(self, adc_index: int) -> int:
        return (
            1 << (self.reference_input_index - 1)
            if adc_index == self.reference_adc_index
            else 0
        )


class LeadOffRegisterIo(Protocol):
    """Hardware-independent operations required by the lead-off state machine."""

    @property
    def adc_indices(self) -> tuple[int, ...]: ...

    def snapshot(self, adc_index: int) -> RegisterSnapshot: ...

    def send_command(self, adc_index: int, command: Ads1299Command) -> None: ...

    def delay_clock_cycles(self, cycles: int) -> None: ...

    def write_register(
        self, adc_index: int, register: Ads1299Register, value: int
    ) -> None: ...

    def read_register(self, adc_index: int, register: Ads1299Register) -> int: ...

    def restore_snapshot(self, adc_index: int, snapshot: RegisterSnapshot) -> None: ...

    def lead_off_status_ready(self) -> bool: ...

    def poll_status_words(self) -> Mapping[int, int | None]: ...


class LeadOffPhase(Enum):
    IDLE = "idle"
    SAVE_EEG_CONFIG = "save_eeg_config"
    STOP_CONTINUOUS = "stop_continuous"
    CONFIGURE_LEAD_OFF = "configure_lead_off"
    WAIT_STABLE = "wait_stable"
    ACQUIRE_STATUS = "acquire_status"
    CLASSIFY_CHANNELS = "classify_channels"
    DISABLE_LEAD_OFF = "disable_lead_off"
    RESTORE_EEG_CONFIG = "restore_eeg_config"
    RESUME_CONTINUOUS = "resume_continuous"
    COMPLETE = "complete"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        return self in (self.COMPLETE, self.FAILED, self.CANCELLED)


PHASE_SEQUENCE = (
    LeadOffPhase.IDLE,
    LeadOffPhase.SAVE_EEG_CONFIG,
    LeadOffPhase.STOP_CONTINUOUS,
    LeadOffPhase.CONFIGURE_LEAD_OFF,
    LeadOffPhase.WAIT_STABLE,
    LeadOffPhase.ACQUIRE_STATUS,
    LeadOffPhase.CLASSIFY_CHANNELS,
    LeadOffPhase.DISABLE_LEAD_OFF,
    LeadOffPhase.RESTORE_EEG_CONFIG,
    LeadOffPhase.RESUME_CONTINUOUS,
    LeadOffPhase.COMPLETE,
)


class ContactState(Enum):
    NOT_TESTED = "not_tested"
    CONNECTED = "connected"
    ELECTRODE_OFF = "electrode_off"
    UNSTABLE = "unstable"
    UNKNOWN = "unknown"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True, slots=True)
class ChannelContactResult:
    logical_channel: str
    electrode: str
    adc_index: int
    adc_input_index: int
    state: ContactState
    off_samples: int
    total_samples: int
    equivalent_impedance_ohms: None = None


@dataclass(frozen=True, slots=True)
class AuxiliaryContactResult:
    role: str
    electrode: str
    state: ContactState
    note: str
    off_samples: int = 0
    total_samples: int = 0
    equivalent_impedance_ohms: None = None


@dataclass(frozen=True, slots=True)
class LeadOffReport:
    phase: LeadOffPhase
    scenario: str
    channels: tuple[ChannelContactResult, ...]
    reference: AuxiliaryContactResult
    bias: AuxiliaryContactResult
    primary_error: str | None
    recovery_errors: tuple[str, ...]
    status_samples_per_adc: tuple[int, int]
    note: str = (
        "Software comparator simulation only; no real electrode impedance measurement."
    )


def _classify_bits(bits: tuple[bool, ...]) -> ContactState:
    if not bits:
        return ContactState.UNKNOWN
    if all(bits):
        return ContactState.ELECTRODE_OFF
    if not any(bits):
        return ContactState.CONNECTED
    return ContactState.UNSTABLE


class LeadOffStateMachine:
    """Step-driven, best-effort-recovery lead-off protocol controller."""

    def __init__(
        self,
        io: LeadOffRegisterIo,
        configuration: LeadOffConfiguration | None = None,
        *,
        scenario_name: str = "external-or-unspecified",
    ) -> None:
        self.io = io
        self.configuration = configuration or LeadOffConfiguration()
        if tuple(io.adc_indices) != (1, 2):
            raise ValueError("lead-off state machine requires independent ADS1 and ADS2")
        self.scenario_name = scenario_name
        self.phase = LeadOffPhase.IDLE
        self.phase_history = [self.phase]
        self._snapshots: dict[int, RegisterSnapshot] = {}
        self._stopped_adcs: set[int] = set()
        self._status: dict[int, list[ParsedLeadOffStatus]] = {1: [], 2: []}
        self._ready_polls = 0
        self._status_polls = 0
        self._cancel_requested = False
        self._primary_error: str | None = None
        self._recovery_errors: list[str] = []
        self._requested_terminal = LeadOffPhase.FAILED
        self.report: LeadOffReport | None = None

    @property
    def active(self) -> bool:
        return self.phase is not LeadOffPhase.IDLE and not self.phase.terminal

    @property
    def progress_percent(self) -> float:
        if self.phase in (LeadOffPhase.FAILED, LeadOffPhase.CANCELLED):
            return 100.0
        try:
            return 100.0 * PHASE_SEQUENCE.index(self.phase) / (len(PHASE_SEQUENCE) - 1)
        except ValueError:
            return 0.0

    def start(self) -> None:
        if self.phase is not LeadOffPhase.IDLE:
            raise LeadOffProtocolError("lead-off state machine has already started")
        self._transition(LeadOffPhase.SAVE_EEG_CONFIG)

    def cancel(self) -> None:
        if self.phase.terminal or self.phase is LeadOffPhase.IDLE:
            return
        self._cancel_requested = True

    def run_to_terminal(self, max_steps: int = 256) -> LeadOffReport:
        for _ in range(max_steps):
            if self.phase.terminal:
                assert self.report is not None
                return self.report
            self.step()
        raise LeadOffProtocolError("state machine did not terminate within max_steps")

    def step(self) -> LeadOffPhase:
        if self.phase is LeadOffPhase.IDLE:
            raise LeadOffProtocolError("start must be called before step")
        if self.phase.terminal:
            return self.phase
        if self._cancel_requested and self.phase not in (
            LeadOffPhase.DISABLE_LEAD_OFF,
            LeadOffPhase.RESTORE_EEG_CONFIG,
            LeadOffPhase.RESUME_CONTINUOUS,
        ):
            self._begin_cleanup("lead-off operation cancelled", LeadOffPhase.CANCELLED)
            if self.phase.terminal:
                return self.phase

        try:
            if self.phase is LeadOffPhase.SAVE_EEG_CONFIG:
                self._save_configuration()
            elif self.phase is LeadOffPhase.STOP_CONTINUOUS:
                self._stop_continuous()
            elif self.phase is LeadOffPhase.CONFIGURE_LEAD_OFF:
                self._configure_lead_off()
            elif self.phase is LeadOffPhase.WAIT_STABLE:
                self._wait_until_ready()
            elif self.phase is LeadOffPhase.ACQUIRE_STATUS:
                self._acquire_status()
            elif self.phase is LeadOffPhase.CLASSIFY_CHANNELS:
                self.report = self._build_report(LeadOffPhase.COMPLETE)
                self._transition(LeadOffPhase.DISABLE_LEAD_OFF)
            elif self.phase is LeadOffPhase.DISABLE_LEAD_OFF:
                self._disable_lead_off_best_effort()
                self._transition(LeadOffPhase.RESTORE_EEG_CONFIG)
            elif self.phase is LeadOffPhase.RESTORE_EEG_CONFIG:
                self._restore_best_effort()
                self._transition(LeadOffPhase.RESUME_CONTINUOUS)
            elif self.phase is LeadOffPhase.RESUME_CONTINUOUS:
                self._resume_best_effort()
                self._finish()
        except (LeadOffProtocolError, RegisterAccessError, OSError, ValueError) as exc:
            if self.phase in (
                LeadOffPhase.DISABLE_LEAD_OFF,
                LeadOffPhase.RESTORE_EEG_CONFIG,
                LeadOffPhase.RESUME_CONTINUOUS,
            ):
                self._recovery_errors.append(f"{self.phase.value}: {exc}")
                next_phase = {
                    LeadOffPhase.DISABLE_LEAD_OFF: LeadOffPhase.RESTORE_EEG_CONFIG,
                    LeadOffPhase.RESTORE_EEG_CONFIG: LeadOffPhase.RESUME_CONTINUOUS,
                    LeadOffPhase.RESUME_CONTINUOUS: LeadOffPhase.FAILED,
                }[self.phase]
                if next_phase is LeadOffPhase.FAILED:
                    self._finish(force_failed=True)
                else:
                    self._transition(next_phase)
            else:
                self._begin_cleanup(str(exc), LeadOffPhase.FAILED)
        return self.phase

    def _transition(self, phase: LeadOffPhase) -> None:
        self.phase = phase
        self.phase_history.append(phase)

    def _begin_cleanup(self, error: str, terminal: LeadOffPhase) -> None:
        if self._primary_error is None:
            self._primary_error = error
            self._requested_terminal = terminal
        if not self._stopped_adcs:
            self.report = self._build_report(terminal)
            self._transition(terminal)
            return
        self._transition(LeadOffPhase.DISABLE_LEAD_OFF)

    def _save_configuration(self) -> None:
        self._snapshots = {adc: self.io.snapshot(adc) for adc in self.io.adc_indices}
        self._transition(LeadOffPhase.STOP_CONTINUOUS)

    def _stop_continuous(self) -> None:
        for adc in self.io.adc_indices:
            self.io.send_command(adc, Ads1299Command.SDATAC)
            self._stopped_adcs.add(adc)
            self.io.delay_clock_cycles(T_SDECODE_CLOCKS)
        self._transition(LeadOffPhase.CONFIGURE_LEAD_OFF)

    def _write_verified(self, adc: int, register: Ads1299Register, value: int) -> None:
        self.io.write_register(adc, register, value)
        self.io.delay_clock_cycles(T_SDECODE_CLOCKS)
        actual = self.io.read_register(adc, register)
        self.io.delay_clock_cycles(T_SDECODE_CLOCKS)
        if actual != value:
            raise LeadOffProtocolError(
                f"ADS{adc} {register.name} readback 0x{actual:02X} != 0x{value:02X}"
            )

    def _configure_lead_off(self) -> None:
        for adc in sorted(self._stopped_adcs):
            misc1 = self.io.read_register(adc, Ads1299Register.MISC1)
            self.io.delay_clock_cycles(T_SDECODE_CLOCKS)
            if not misc1 & MISC1_SRB1:
                raise LeadOffProtocolError(
                    f"ADS{adc} MISC1.SRB1 is not enabled for the frozen referential montage"
                )
            config4 = self.io.read_register(adc, Ads1299Register.CONFIG4)
            self.io.delay_clock_cycles(T_SDECODE_CLOCKS)
            self._write_verified(adc, Ads1299Register.LOFF, self.configuration.loff_value)
            self._write_verified(
                adc, Ads1299Register.LOFF_SENSP, self.configuration.positive_mask
            )
            self._write_verified(
                adc,
                Ads1299Register.LOFF_SENSN,
                self.configuration.negative_mask_for_adc(adc),
            )
            self._write_verified(adc, Ads1299Register.LOFF_FLIP, 0x00)
            self._write_verified(
                adc,
                Ads1299Register.CONFIG4,
                config4 | CONFIG4_LOFF_COMP_ENABLE,
            )
        self._transition(LeadOffPhase.WAIT_STABLE)

    def _wait_until_ready(self) -> None:
        self._ready_polls += 1
        if self.io.lead_off_status_ready():
            self._transition(LeadOffPhase.ACQUIRE_STATUS)
        elif self._ready_polls >= self.configuration.wait_ready_poll_limit:
            raise LeadOffProtocolError("timeout waiting for lead-off status readiness")

    def _acquire_status(self) -> None:
        self._status_polls += 1
        words = self.io.poll_status_words()
        for adc in sorted(self._stopped_adcs):
            word = words.get(adc)
            if word is not None:
                self._status[adc].append(parse_lead_off_status_word(word))
        if all(
            len(self._status[adc]) >= self.configuration.required_status_samples
            for adc in self.io.adc_indices
        ):
            self._transition(LeadOffPhase.CLASSIFY_CHANNELS)
        elif self._status_polls >= self.configuration.status_poll_limit:
            counts = ", ".join(
                f"ADS{adc}={len(self._status[adc])}" for adc in self.io.adc_indices
            )
            raise LeadOffProtocolError(f"insufficient status data before timeout: {counts}")

    def _disable_lead_off_best_effort(self) -> None:
        errors: list[str] = []
        for adc in sorted(self._stopped_adcs):
            for register, value in (
                (Ads1299Register.LOFF_SENSP, 0x00),
                (Ads1299Register.LOFF_SENSN, 0x00),
            ):
                try:
                    self._write_verified(adc, register, value)
                except (LeadOffProtocolError, RegisterAccessError, OSError, ValueError) as exc:
                    errors.append(f"ADS{adc} {register.name}: {exc}")
            try:
                config4 = self.io.read_register(adc, Ads1299Register.CONFIG4)
                self.io.delay_clock_cycles(T_SDECODE_CLOCKS)
                self._write_verified(
                    adc,
                    Ads1299Register.CONFIG4,
                    config4 & ~CONFIG4_LOFF_COMP_ENABLE,
                )
            except (LeadOffProtocolError, RegisterAccessError, OSError, ValueError) as exc:
                errors.append(f"ADS{adc} CONFIG4: {exc}")
        self._recovery_errors.extend(errors)

    def _restore_best_effort(self) -> None:
        for adc in sorted(self._stopped_adcs):
            snapshot = self._snapshots.get(adc)
            if snapshot is None:
                self._recovery_errors.append(f"ADS{adc}: configuration snapshot unavailable")
                continue
            try:
                self.io.restore_snapshot(adc, snapshot)
                self.io.delay_clock_cycles(T_SDECODE_CLOCKS)
            except (RegisterAccessError, OSError, ValueError) as exc:
                self._recovery_errors.append(f"ADS{adc} restore: {exc}")

    def _resume_best_effort(self) -> None:
        for adc in sorted(self._stopped_adcs):
            try:
                self.io.send_command(adc, Ads1299Command.RDATAC)
                self.io.delay_clock_cycles(T_SDECODE_CLOCKS)
            except (RegisterAccessError, OSError, ValueError) as exc:
                self._recovery_errors.append(f"ADS{adc} RDATAC: {exc}")

    def _finish(self, *, force_failed: bool = False) -> None:
        terminal = self._requested_terminal if self._primary_error else LeadOffPhase.COMPLETE
        if force_failed or self._recovery_errors:
            terminal = LeadOffPhase.FAILED
        self.report = self._build_report(terminal)
        self._transition(terminal)

    def _build_report(self, phase: LeadOffPhase) -> LeadOffReport:
        channels: list[ChannelContactResult] = []
        for channel in ELECTRODE_CHANNELS:
            statuses = self._status[channel.adc_index]
            bits = tuple(
                bool(status.positive_mask & (1 << (channel.adc_input_index - 1)))
                for status in statuses
            )
            channels.append(
                ChannelContactResult(
                    logical_channel=channel.logical_channel,
                    electrode=channel.electrode,
                    adc_index=channel.adc_index,
                    adc_input_index=channel.adc_input_index,
                    state=_classify_bits(bits),
                    off_samples=sum(bits),
                    total_samples=len(bits),
                )
            )

        ref_adc = self.configuration.reference_adc_index
        ref_mask = 1 << (self.configuration.reference_input_index - 1)
        ref_bits = tuple(
            bool(status.negative_mask & ref_mask) for status in self._status[ref_adc]
        )
        reference = AuxiliaryContactResult(
            role=REFERENCE_ELECTRODE.role,
            electrode=REFERENCE_ELECTRODE.electrode,
            state=_classify_bits(ref_bits),
            note=(
                f"Shared SRB1 reference; one N-side source only: "
                f"ADS{ref_adc} IN{self.configuration.reference_input_index}N"
            ),
            off_samples=sum(ref_bits),
            total_samples=len(ref_bits),
        )
        bias = AuxiliaryContactResult(
            role=BIAS_ELECTRODE.role,
            electrode=BIAS_ELECTRODE.electrode,
            state=ContactState.UNSUPPORTED,
            note=(
                "Independent BIAS impedance is unsupported during normal BIAS-amplifier "
                "operation; hardware behavior remains unverified"
            ),
        )
        return LeadOffReport(
            phase=phase,
            scenario=self.scenario_name,
            channels=tuple(channels),
            reference=reference,
            bias=bias,
            primary_error=self._primary_error,
            recovery_errors=tuple(self._recovery_errors),
            status_samples_per_adc=(len(self._status[1]), len(self._status[2])),
        )


class SimulationScenario(Enum):
    NORMAL = "normal"
    POOR_CONTACT = "poor_contact"
    OPEN = "open"
    ABNORMALLY_LOW = "abnormally_low"
    THRESHOLD_BOUNDARY = "threshold_boundary"
    NOISY_JITTER = "noisy_jitter"
    ADS1_STATUS_MISSING = "ads1_status_missing"
    ADS2_STATUS_MISSING = "ads2_status_missing"
    COMMON_REFERENCE_OFF = "common_reference_off"
    BIAS_UNKNOWN = "bias_unknown"
    TIMEOUT = "timeout"
    REGISTER_ERROR = "register_error"
    RESTORE_FAILURE = "restore_failure"


@dataclass(frozen=True, slots=True)
class TransportEvent:
    operation: str
    adc_index: int | None = None
    command: Ads1299Command | None = None
    register: Ads1299Register | None = None
    value: int | None = None
    cycles: int | None = None


def build_status_word(positive_mask: int = 0, negative_mask: int = 0, gpio: int = 0) -> int:
    if not 0 <= positive_mask <= 0xFF or not 0 <= negative_mask <= 0xFF:
        raise ValueError("lead-off masks must be unsigned bytes")
    if not 0 <= gpio <= 0x0F:
        raise ValueError("GPIO status must be a nibble")
    return (
        (ADS1299_STATUS_HEADER << 20)
        | (positive_mask << 12)
        | (negative_mask << 4)
        | gpio
    )


class SimulatedLeadOffTransport:
    """Deterministic two-device register/status transport; never real hardware."""

    def __init__(self, scenario: SimulationScenario = SimulationScenario.NORMAL) -> None:
        self.scenario = scenario
        self._mirrors = {1: Ads1299RegisterMirror(), 2: Ads1299RegisterMirror()}
        for mirror in self._mirrors.values():
            mirror.write(Ads1299Register.MISC1, MISC1_SRB1)
        self._continuous = {1: True, 2: True}
        self.events: list[TransportEvent] = []
        self._ready_polls = 0
        self._status_index = 0
        self._register_error_raised = False
        self._restore_failure_raised = False

    @property
    def adc_indices(self) -> tuple[int, ...]:
        return (1, 2)

    @property
    def continuous_reading(self) -> Mapping[int, bool]:
        return MappingProxyType(dict(self._continuous))

    def mirror(self, adc_index: int) -> Ads1299RegisterMirror:
        try:
            return self._mirrors[adc_index]
        except KeyError as exc:
            raise RegisterAccessError(f"unknown ADS index: {adc_index}") from exc

    def snapshot(self, adc_index: int) -> RegisterSnapshot:
        self.events.append(TransportEvent("SNAPSHOT", adc_index=adc_index))
        return self.mirror(adc_index).snapshot()

    def send_command(self, adc_index: int, command: Ads1299Command) -> None:
        self.mirror(adc_index)
        command = Ads1299Command(command)
        self.events.append(TransportEvent("COMMAND", adc_index=adc_index, command=command))
        if command is Ads1299Command.SDATAC:
            self._continuous[adc_index] = False
        elif command is Ads1299Command.RDATAC:
            self._continuous[adc_index] = True

    def delay_clock_cycles(self, cycles: int) -> None:
        if not isinstance(cycles, int) or isinstance(cycles, bool) or cycles < T_SDECODE_CLOCKS:
            raise RegisterAccessError(f"command decode delay must be at least {T_SDECODE_CLOCKS} tCLK")
        self.events.append(TransportEvent("WAIT_TCLK", cycles=cycles))

    def _require_sdatac(self, adc_index: int) -> None:
        if self._continuous[adc_index]:
            raise RegisterAccessError(
                f"ADS{adc_index} register access attempted before SDATAC"
            )

    def write_register(
        self, adc_index: int, register: Ads1299Register, value: int
    ) -> None:
        self._require_sdatac(adc_index)
        if (
            self.scenario is SimulationScenario.REGISTER_ERROR
            and register is Ads1299Register.LOFF
            and not self._register_error_raised
        ):
            self._register_error_raised = True
            raise RegisterAccessError("injected WREG failure")
        self.mirror(adc_index).write(register, value)
        self.events.append(
            TransportEvent(
                "WREG", adc_index=adc_index, register=register, value=value
            )
        )

    def read_register(self, adc_index: int, register: Ads1299Register) -> int:
        self._require_sdatac(adc_index)
        value = self.mirror(adc_index).read(register)
        self.events.append(
            TransportEvent(
                "RREG", adc_index=adc_index, register=register, value=value
            )
        )
        return value

    def restore_snapshot(self, adc_index: int, snapshot: RegisterSnapshot) -> None:
        self._require_sdatac(adc_index)
        self.events.append(TransportEvent("RESTORE", adc_index=adc_index))
        if (
            self.scenario is SimulationScenario.RESTORE_FAILURE
            and adc_index == 2
            and not self._restore_failure_raised
        ):
            self._restore_failure_raised = True
            raise RegisterAccessError("injected restore failure")
        self.mirror(adc_index).restore(snapshot)

    def lead_off_status_ready(self) -> bool:
        self._ready_polls += 1
        self.events.append(TransportEvent("STATUS_READY"))
        if self.scenario is SimulationScenario.TIMEOUT:
            return False
        return self._ready_polls >= 2

    def poll_status_words(self) -> Mapping[int, int | None]:
        self.events.append(TransportEvent("STATUS_POLL"))
        index = self._status_index
        self._status_index += 1
        p1 = p2 = n1 = n2 = 0

        if self.scenario is SimulationScenario.POOR_CONTACT:
            p1 = (0, 0x02, 0, 0x02, 0x02)[index % 5]
        elif self.scenario is SimulationScenario.OPEN:
            p1 = 0x04
        elif self.scenario is SimulationScenario.THRESHOLD_BOUNDARY:
            p1 = (0, 0x10)[index % 2]
        elif self.scenario is SimulationScenario.NOISY_JITTER:
            p1 = (0, 0x20, 0, 0, 0x20)[index % 5]
        elif self.scenario is SimulationScenario.COMMON_REFERENCE_OFF:
            n1 = 0x01

        word1: int | None = build_status_word(p1, n1)
        word2: int | None = build_status_word(p2, n2)
        if self.scenario is SimulationScenario.ADS1_STATUS_MISSING:
            word1 = None
        if self.scenario is SimulationScenario.ADS2_STATUS_MISSING:
            word2 = None

        if word1 is not None:
            parsed = parse_lead_off_status_word(word1)
            self._mirrors[1].update_status(Ads1299Register.LOFF_STATP, parsed.positive_mask)
            self._mirrors[1].update_status(Ads1299Register.LOFF_STATN, parsed.negative_mask)
        if word2 is not None:
            parsed = parse_lead_off_status_word(word2)
            self._mirrors[2].update_status(Ads1299Register.LOFF_STATP, parsed.positive_mask)
            self._mirrors[2].update_status(Ads1299Register.LOFF_STATN, parsed.negative_mask)
        return MappingProxyType({1: word1, 2: word2})


SIMULATION_SCENARIO_NOTES = MappingProxyType(
    {
        SimulationScenario.NORMAL: "all comparator samples report connected",
        SimulationScenario.POOR_CONTACT: "intermittent comparator state on CH02",
        SimulationScenario.OPEN: "comparator reports electrode-off on CH03",
        SimulationScenario.ABNORMALLY_LOW: (
            "comparator still reports connected; abnormal-low impedance is not "
            "distinguishable without a validated amplitude method"
        ),
        SimulationScenario.THRESHOLD_BOUNDARY: "alternating comparator state on CH05",
        SimulationScenario.NOISY_JITTER: "deterministic comparator jitter on CH06",
        SimulationScenario.ADS1_STATUS_MISSING: "ADS1 status words are absent",
        SimulationScenario.ADS2_STATUS_MISSING: "ADS2 status words are absent",
        SimulationScenario.COMMON_REFERENCE_OFF: "single SRB1 reference status reports off",
        SimulationScenario.BIAS_UNKNOWN: "BIAS remains unsupported in normal operation",
        SimulationScenario.TIMEOUT: "status readiness never arrives",
        SimulationScenario.REGISTER_ERROR: "first LOFF WREG fails",
        SimulationScenario.RESTORE_FAILURE: "ADS2 snapshot restore fails",
    }
)
