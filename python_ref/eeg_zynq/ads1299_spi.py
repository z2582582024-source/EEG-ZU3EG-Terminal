"""ADS1299 SPI transport boundary and dual-device board bring-up model.

This module implements the byte-level command protocol from TI SBAS499C
(ADS1299 Rev. C).  It deliberately stops at a small wire-port protocol so a
future AXI, RPU, or direct GPIO/SPI implementation can replace the transport
without changing the lead-off or frame-pairing state machines.

No class in this module claims that a physical board has passed bring-up.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Mapping, Protocol, Sequence

from .ads1299 import ADS1299_FRAME_BYTES, Ads1299Frame, parse_ads1299_frame
from .milestone4_common import (
    ADS1299_ID_EXPECTED as ADS1299_ID_8_CHANNEL_VALUE,
    ADS1299_ID_MASK as ADS1299_ID_DEVICE_MASK,
)
from .leadoff import (
    REGISTER_SPECS,
    Ads1299Command,
    Ads1299Register,
    Ads1299RegisterMirror,
    RegisterAccessError,
    RegisterSnapshot,
)


ADS1299_ADC_INDICES = (1, 2)
ADS1299_ADC_MASK = {1: 0b01, 2: 0b10}
ADS1299_BOTH_MASK = 0b11
T_SDECODE_CLOCKS = 4
T_SCCS_CLOCKS = 4
T_CSH_CLOCKS = 2
T_POR_CLOCKS = 1 << 18
T_RESET_LOW_CLOCKS = 2
T_RESET_RELEASE_CLOCKS = 18
T_START_LOW_SYNC_CLOCKS = 16
T_START_HIGH_CLOCKS = 2

DEFAULT_DEVICE_CLOCK_HZ = 2_048_000
REFERENCE_STARTUP_TYPICAL_MS = 150
DEFAULT_REFERENCE_SETTLE_CLOCKS = 409_600  # 200 ms at 2.048 MHz; design margin.

CONFIG1_250_SPS = 0x96
CONFIG2_INTERNAL_TEST = 0xD0
CONFIG3_INTERNAL_REFERENCE = 0xE0
CHANNEL_SET_TEST_SIGNAL_GAIN1 = 0x05
CHANNEL_SET_INPUT_SHORT_GAIN1 = 0x01


class Ads1299WirePort(Protocol):
    """Minimum physical transport expected by :class:`Ads1299SpiAdapter`.

    ``shift_byte`` performs one CPOL=0, CPHA=1, MSB-first transfer while the
    transaction-selected CS lines remain low.  Its result contains one RX
    byte for each selected ADC.  Delay units are ADS1299 device-clock cycles,
    not host CPU or PL clock cycles.
    """

    def set_pwdn_n(self, level: bool) -> None: ...

    def set_reset_n(self, level: bool) -> None: ...

    def set_start(self, level: bool) -> None: ...

    def begin_transaction(self, adc_mask: int) -> None: ...

    def shift_byte(self, tx_byte: int) -> Mapping[int, int]: ...

    def end_transaction(self) -> None: ...

    def delay_device_clocks(self, cycles: int) -> None: ...

    def read_drdy_low_mask(self) -> int: ...


class Ads1299TransportError(RuntimeError):
    """The physical transport failed or returned an invalid response."""


class Ads1299ProtocolError(RuntimeError):
    """A command was attempted in an incompatible ADS1299 protocol mode."""


class Ads1299TimeoutError(Ads1299ProtocolError):
    """One or both ADS1299 devices did not become ready in time."""


class Ads1299ReadbackError(Ads1299ProtocolError):
    """An ID or writable-register readback did not match the request."""


def validate_ads1299_id(value: int) -> bool:
    """Return true for the documented eight-channel ADS1299 ID fields.

    Revision bits 7:5 are intentionally ignored.  Bits 4:0 must encode the
    fixed bit, DEV_ID=11, and NU_CH=10 described by the ID register table.
    """

    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and 0 <= value <= 0xFF
        and (value & ADS1299_ID_DEVICE_MASK) == ADS1299_ID_8_CHANNEL_VALUE
    )


class Ads1299SpiAdapter:
    """Register and data operations over an :class:`Ads1299WirePort`.

    The adapter is the sole owner of its software register mirrors.  A
    snapshot is therefore read from that mirror and never issues RREG while
    RDATAC is active.  Real firmware must route every configuration write
    through this object (or keep an equivalent authoritative mirror).
    """

    def __init__(self, wire: Ads1299WirePort) -> None:
        self.wire = wire
        self._continuous = {adc: True for adc in ADS1299_ADC_INDICES}
        self._mirrors = {
            adc: Ads1299RegisterMirror() for adc in ADS1299_ADC_INDICES
        }
        self._observed_registers: dict[int, dict[int, int]] = {
            adc: {} for adc in ADS1299_ADC_INDICES
        }

    @property
    def adc_indices(self) -> tuple[int, ...]:
        return ADS1299_ADC_INDICES

    def continuous_reading(self, adc_index: int) -> bool:
        self._check_adc(adc_index)
        return self._continuous[adc_index]

    def delay_clock_cycles(self, cycles: int) -> None:
        if not isinstance(cycles, int) or isinstance(cycles, bool) or cycles < 0:
            raise ValueError("cycles must be a non-negative integer")
        self.wire.delay_device_clocks(cycles)

    def mark_hardware_reset(self) -> None:
        """Reset protocol state after a RESET pin pulse or RESET command."""

        self._continuous = {adc: True for adc in ADS1299_ADC_INDICES}
        self._mirrors = {
            adc: Ads1299RegisterMirror() for adc in ADS1299_ADC_INDICES
        }
        self._observed_registers = {adc: {} for adc in ADS1299_ADC_INDICES}

    def send_command(self, adc_index: int, command: Ads1299Command | int) -> None:
        self._check_adc(adc_index)
        self._send_command_mask(ADS1299_ADC_MASK[adc_index], command)

    def send_command_all(self, command: Ads1299Command | int) -> None:
        self._send_command_mask(ADS1299_BOTH_MASK, command)

    def _send_command_mask(self, adc_mask: int, command: Ads1299Command | int) -> None:
        try:
            decoded = Ads1299Command(command)
        except ValueError as exc:
            raise Ads1299ProtocolError(f"unsupported ADS1299 command: 0x{int(command):02X}") from exc
        self._transfer(adc_mask, (int(decoded),))
        selected = self._indices_for_mask(adc_mask)
        if decoded is Ads1299Command.SDATAC:
            for adc in selected:
                self._continuous[adc] = False
        elif decoded in (Ads1299Command.RDATAC, Ads1299Command.RESET):
            if decoded is Ads1299Command.RESET:
                for adc in selected:
                    self._continuous[adc] = True
                    self._mirrors[adc] = Ads1299RegisterMirror()
                    self._observed_registers[adc] = {}
            else:
                for adc in selected:
                    self._continuous[adc] = True

    def read_register(self, adc_index: int, register: Ads1299Register | int) -> int:
        return self.read_registers(adc_index, register, 1)[0]

    def read_registers(
        self, adc_index: int, start_register: Ads1299Register | int, count: int
    ) -> bytes:
        self._check_adc(adc_index)
        start = self._validate_register_span(start_register, count)
        if self._continuous[adc_index]:
            raise Ads1299ProtocolError("RREG requires SDATAC before register access")
        responses = self._transfer(
            ADS1299_ADC_MASK[adc_index],
            (0x20 | start, count - 1, *((0,) * count)),
            decode_after=(0, 1),
        )[adc_index]
        values = bytes(responses[2:])
        for offset, value in enumerate(values):
            address = start + offset
            self._observed_registers[adc_index][address] = value
            try:
                register = Ads1299Register(address)
            except ValueError:
                continue
            if register in (Ads1299Register.LOFF_STATP, Ads1299Register.LOFF_STATN):
                self._mirrors[adc_index].update_status(register, value)
            elif register is Ads1299Register.CONFIG3:
                self._mirrors[adc_index].update_bias_status(bool(value & 0x01))
        return values

    def write_register(
        self, adc_index: int, register: Ads1299Register | int, value: int
    ) -> None:
        self.write_registers(adc_index, register, (value,))

    def write_registers(
        self,
        adc_index: int,
        start_register: Ads1299Register | int,
        values: Sequence[int],
    ) -> None:
        self._check_adc(adc_index)
        payload = tuple(values)
        start = self._validate_register_span(start_register, len(payload))
        if self._continuous[adc_index]:
            raise Ads1299ProtocolError("WREG requires SDATAC before register access")
        for offset, value in enumerate(payload):
            if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 0xFF:
                raise RegisterAccessError("register value must be an unsigned byte")
            address = start + offset
            try:
                register = Ads1299Register(address)
            except ValueError:
                continue
            spec = REGISTER_SPECS.get(register)
            if spec is not None:
                current = self._mirrors[adc_index].read(register)
                spec.validate_write(value, current)
        self._transfer(
            ADS1299_ADC_MASK[adc_index],
            (0x40 | start, len(payload) - 1, *payload),
            decode_after=(0, 1),
        )
        for offset, value in enumerate(payload):
            address = start + offset
            self._observed_registers[adc_index][address] = value
            try:
                register = Ads1299Register(address)
            except ValueError:
                continue
            if register in REGISTER_SPECS:
                self._mirrors[adc_index].write(register, value)

    def snapshot(self, adc_index: int) -> RegisterSnapshot:
        self._check_adc(adc_index)
        return self._mirrors[adc_index].snapshot()

    def restore_snapshot(self, adc_index: int, snapshot: RegisterSnapshot) -> None:
        self._check_adc(adc_index)
        for register, value in snapshot.values.items():
            self.write_register(adc_index, register, value)

    def configure_input_short_gain1(self) -> None:
        """Configure both devices for the manual's gain-1 input-short test.

        The caller must first stop continuous conversion.  Every channel
        register is read back before this method returns.
        """

        for adc in ADS1299_ADC_INDICES:
            self.write_registers(
                adc,
                Ads1299Register.CH1SET,
                (CHANNEL_SET_INPUT_SHORT_GAIN1,) * 8,
            )
            actual = self.read_registers(adc, Ads1299Register.CH1SET, 8)
            expected = bytes((CHANNEL_SET_INPUT_SHORT_GAIN1,) * 8)
            if actual != expected:
                raise Ads1299ReadbackError(
                    f"ADS{adc} input-short CHnSET readback {actual.hex()} "
                    f"!= {expected.hex()}"
                )

    def lead_off_status_ready(self) -> bool:
        return (self.wire.read_drdy_low_mask() & ADS1299_BOTH_MASK) == ADS1299_BOTH_MASK

    def poll_status_words(self) -> Mapping[int, int | None]:
        if not self.lead_off_status_ready():
            return {1: None, 2: None}
        raw = self.read_data_once(ADS1299_BOTH_MASK)
        return {adc: int.from_bytes(frame[:3], "big") for adc, frame in raw.items()}

    def read_data_once(self, adc_mask: int) -> Mapping[int, bytes]:
        selected = self._indices_for_mask(adc_mask)
        if any(self._continuous[adc] for adc in selected):
            raise Ads1299ProtocolError("RDATA requires SDATAC for every selected ADS1299")
        responses = self._transfer(
            adc_mask,
            (int(Ads1299Command.RDATA), *((0,) * ADS1299_FRAME_BYTES)),
            decode_after=(0,),
        )
        return {adc: bytes(responses[adc][1:]) for adc in selected}

    def read_dual_continuous_frame(self) -> Mapping[int, bytes]:
        if not all(self._continuous.values()):
            raise Ads1299ProtocolError("dual continuous frame read requires RDATAC on both ADCs")
        responses = self._transfer(
            ADS1299_BOTH_MASK, (0,) * ADS1299_FRAME_BYTES
        )
        return {adc: bytes(responses[adc]) for adc in ADS1299_ADC_INDICES}

    def _transfer(
        self,
        adc_mask: int,
        tx_bytes: Sequence[int],
        *,
        decode_after: Sequence[int] = (),
    ) -> dict[int, bytearray]:
        selected = self._indices_for_mask(adc_mask)
        if not tx_bytes:
            raise Ads1299ProtocolError("SPI transaction cannot be empty")
        for value in tx_bytes:
            if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 0xFF:
                raise Ads1299ProtocolError("SPI TX values must be unsigned bytes")
        rx = {adc: bytearray() for adc in selected}
        active = False
        try:
            self.wire.begin_transaction(adc_mask)
            active = True
            for index, tx_byte in enumerate(tx_bytes):
                returned = self.wire.shift_byte(tx_byte)
                for adc in selected:
                    if adc not in returned:
                        raise Ads1299TransportError(
                            f"SPI response omitted selected ADS{adc}"
                        )
                    value = returned[adc]
                    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 0xFF:
                        raise Ads1299TransportError(
                            f"SPI response from ADS{adc} is not an unsigned byte"
                        )
                    rx[adc].append(value)
                if index in decode_after:
                    self.wire.delay_device_clocks(T_SDECODE_CLOCKS)
            self.wire.delay_device_clocks(T_SCCS_CLOCKS)
            self.wire.end_transaction()
            active = False
            self.wire.delay_device_clocks(T_CSH_CLOCKS)
            return rx
        except (Ads1299ProtocolError, Ads1299TransportError, RegisterAccessError):
            if active:
                self._end_failed_transaction()
            raise
        except Exception as exc:
            if active:
                self._end_failed_transaction()
            raise Ads1299TransportError(f"ADS1299 SPI transaction failed: {exc}") from exc

    def _end_failed_transaction(self) -> None:
        try:
            self.wire.delay_device_clocks(T_SCCS_CLOCKS)
            self.wire.end_transaction()
            self.wire.delay_device_clocks(T_CSH_CLOCKS)
        except Exception:
            pass

    @staticmethod
    def _check_adc(adc_index: int) -> None:
        if adc_index not in ADS1299_ADC_INDICES:
            raise ValueError("adc_index must be 1 or 2")

    @staticmethod
    def _indices_for_mask(adc_mask: int) -> tuple[int, ...]:
        if not isinstance(adc_mask, int) or isinstance(adc_mask, bool):
            raise ValueError("adc_mask must be an integer")
        selected = tuple(
            adc for adc in ADS1299_ADC_INDICES if adc_mask & ADS1299_ADC_MASK[adc]
        )
        if not selected or adc_mask & ~ADS1299_BOTH_MASK:
            raise ValueError("adc_mask must select ADS1, ADS2, or both")
        return selected

    @staticmethod
    def _validate_register_span(start_register: Ads1299Register | int, count: int) -> int:
        start = int(start_register)
        if not isinstance(count, int) or isinstance(count, bool) or count <= 0:
            raise ValueError("register count must be a positive integer")
        if not 0 <= start <= 0x17 or start + count - 1 > 0x17 or count > 0x20:
            raise ValueError("ADS1299 register span must stay within 0x00..0x17")
        return start


@dataclass(slots=True)
class DualFrameCounters:
    paired_frames: int = 0
    discarded_settling_frames: int = 0
    ads1_missing_timeouts: int = 0
    ads2_missing_timeouts: int = 0
    both_missing_timeouts: int = 0
    desynchronization_events: int = 0
    spi_errors: int = 0
    format_errors: int = 0
    synchronization_recoveries: int = 0


@dataclass(frozen=True, slots=True)
class DualAds1299Frame:
    pair_sequence: int
    adc1: Ads1299Frame
    adc2: Ads1299Frame


class DualAds1299FrameReceiver:
    """Pair two independent DOUT streams only after both DRDY lines are low."""

    def __init__(self, adapter: Ads1299SpiAdapter) -> None:
        self.adapter = adapter
        self.counters = DualFrameCounters()
        self._next_sequence = 0

    def capture_pair(self, *, max_drdy_polls: int = 1024) -> DualAds1299Frame:
        if not isinstance(max_drdy_polls, int) or isinstance(max_drdy_polls, bool) or max_drdy_polls <= 0:
            raise ValueError("max_drdy_polls must be a positive integer")
        last_mask = 0
        saw_partial = False
        for _ in range(max_drdy_polls):
            last_mask = self.adapter.wire.read_drdy_low_mask() & ADS1299_BOTH_MASK
            if last_mask == ADS1299_BOTH_MASK:
                return self._receive_pair()
            if last_mask in (0b01, 0b10):
                saw_partial = True
        if last_mask == 0b01:
            self.counters.ads2_missing_timeouts += 1
        elif last_mask == 0b10:
            self.counters.ads1_missing_timeouts += 1
        else:
            self.counters.both_missing_timeouts += 1
        if saw_partial:
            self.counters.desynchronization_events += 1
        raise Ads1299TimeoutError(
            f"dual DRDY timeout after {max_drdy_polls} polls; low mask=0b{last_mask:02b}"
        )

    def recover_synchronization(
        self, *, max_drdy_polls: int = 1024
    ) -> DualAds1299Frame:
        """Restart both converters and return the fourth, settled data pair."""

        self.adapter.wire.set_start(False)
        self.adapter.delay_clock_cycles(T_START_LOW_SYNC_CLOCKS)
        self.adapter.wire.set_start(True)
        self.adapter.delay_clock_cycles(T_START_HIGH_CLOCKS)
        for _ in range(3):
            self.capture_pair(max_drdy_polls=max_drdy_polls)
            self.counters.discarded_settling_frames += 1
        stable = self.capture_pair(max_drdy_polls=max_drdy_polls)
        self.counters.synchronization_recoveries += 1
        return stable

    def _receive_pair(self) -> DualAds1299Frame:
        try:
            raw = self.adapter.read_dual_continuous_frame()
        except (Ads1299TransportError, Ads1299ProtocolError):
            self.counters.spi_errors += 1
            raise
        try:
            adc1 = parse_ads1299_frame(raw[1])
            adc2 = parse_ads1299_frame(raw[2])
        except ValueError:
            self.counters.format_errors += 1
            raise
        pair = DualAds1299Frame(self._next_sequence, adc1, adc2)
        self._next_sequence += 1
        self.counters.paired_frames += 1
        return pair


class BringupPhase(Enum):
    IDLE = "idle"
    SAFE_OUTPUTS = "safe_outputs"
    WAIT_POWER_ON_RESET = "wait_power_on_reset"
    RESET_PULSE = "reset_pulse"
    STOP_CONTINUOUS = "stop_continuous"
    READ_ID = "read_id"
    CONFIGURE_INTERNAL_TEST = "configure_internal_test"
    WAIT_REFERENCE = "wait_reference"
    VERIFY_READBACK = "verify_readback"
    ENABLE_CONTINUOUS = "enable_continuous"
    START_SYNCHRONOUS = "start_synchronous"
    DISCARD_SETTLING_FRAMES = "discard_settling_frames"
    COMPLETE = "complete"
    FAILED = "failed"

    @property
    def terminal(self) -> bool:
        return self in (self.COMPLETE, self.FAILED)


@dataclass(frozen=True, slots=True)
class BringupReport:
    phase: BringupPhase
    ids: Mapping[int, int]
    stable_pair: DualAds1299Frame | None
    error: str | None
    note: str = "Software/RTL preparation result; physical board measurements not performed."


class Ads1299BringupController:
    """Step-driven no-human bring-up sequence through internal test data."""

    _CONFIG_START = Ads1299Register.CONFIG1
    _CONFIG_VALUES = (
        CONFIG1_250_SPS,
        CONFIG2_INTERNAL_TEST,
        CONFIG3_INTERNAL_REFERENCE,
        0x00,
        *((CHANNEL_SET_TEST_SIGNAL_GAIN1,) * 8),
    )

    def __init__(
        self,
        adapter: Ads1299SpiAdapter,
        receiver: DualAds1299FrameReceiver | None = None,
        *,
        reference_settle_clocks: int = DEFAULT_REFERENCE_SETTLE_CLOCKS,
        max_drdy_polls: int = 1024,
    ) -> None:
        if reference_settle_clocks < 307_200:
            raise ValueError("reference_settle_clocks must cover the 150 ms typical startup")
        if max_drdy_polls <= 0:
            raise ValueError("max_drdy_polls must be positive")
        self.adapter = adapter
        self.receiver = receiver or DualAds1299FrameReceiver(adapter)
        self.reference_settle_clocks = reference_settle_clocks
        self.max_drdy_polls = max_drdy_polls
        self.phase = BringupPhase.IDLE
        self.phase_history = [self.phase]
        self.ids: dict[int, int] = {}
        self.stable_pair: DualAds1299Frame | None = None
        self.error: str | None = None
        self.report: BringupReport | None = None

    def start(self) -> None:
        if self.phase is not BringupPhase.IDLE:
            raise Ads1299ProtocolError("bring-up controller has already started")
        self._transition(BringupPhase.SAFE_OUTPUTS)

    def run_to_terminal(self, max_steps: int = 32) -> BringupReport:
        for _ in range(max_steps):
            if self.phase.terminal:
                assert self.report is not None
                return self.report
            self.step()
        raise Ads1299ProtocolError("bring-up controller did not terminate")

    def step(self) -> BringupPhase:
        if self.phase is BringupPhase.IDLE:
            raise Ads1299ProtocolError("start must be called before step")
        if self.phase.terminal:
            return self.phase
        try:
            if self.phase is BringupPhase.SAFE_OUTPUTS:
                self.adapter.wire.set_start(False)
                self.adapter.wire.set_reset_n(False)
                self.adapter.wire.set_pwdn_n(False)
                self._transition(BringupPhase.WAIT_POWER_ON_RESET)
            elif self.phase is BringupPhase.WAIT_POWER_ON_RESET:
                self.adapter.wire.set_pwdn_n(True)
                self.adapter.wire.set_reset_n(True)
                self.adapter.delay_clock_cycles(T_POR_CLOCKS)
                self._transition(BringupPhase.RESET_PULSE)
            elif self.phase is BringupPhase.RESET_PULSE:
                self.adapter.wire.set_reset_n(False)
                self.adapter.delay_clock_cycles(T_RESET_LOW_CLOCKS)
                self.adapter.wire.set_reset_n(True)
                self.adapter.delay_clock_cycles(T_RESET_RELEASE_CLOCKS)
                self.adapter.mark_hardware_reset()
                self._transition(BringupPhase.STOP_CONTINUOUS)
            elif self.phase is BringupPhase.STOP_CONTINUOUS:
                self.adapter.send_command_all(Ads1299Command.SDATAC)
                self._transition(BringupPhase.READ_ID)
            elif self.phase is BringupPhase.READ_ID:
                for adc in ADS1299_ADC_INDICES:
                    value = self.adapter.read_register(adc, Ads1299Register.ID)
                    if not validate_ads1299_id(value):
                        raise Ads1299ReadbackError(
                            f"ADS{adc} ID 0x{value:02X} is not an eight-channel ADS1299"
                        )
                    self.ids[adc] = value
                self._transition(BringupPhase.CONFIGURE_INTERNAL_TEST)
            elif self.phase is BringupPhase.CONFIGURE_INTERNAL_TEST:
                for adc in ADS1299_ADC_INDICES:
                    self.adapter.write_registers(
                        adc, self._CONFIG_START, self._CONFIG_VALUES
                    )
                self._transition(BringupPhase.WAIT_REFERENCE)
            elif self.phase is BringupPhase.WAIT_REFERENCE:
                self.adapter.delay_clock_cycles(self.reference_settle_clocks)
                self._transition(BringupPhase.VERIFY_READBACK)
            elif self.phase is BringupPhase.VERIFY_READBACK:
                for adc in ADS1299_ADC_INDICES:
                    actual = self.adapter.read_registers(
                        adc, self._CONFIG_START, len(self._CONFIG_VALUES)
                    )
                    for offset, (expected, observed) in enumerate(
                        zip(self._CONFIG_VALUES, actual)
                    ):
                        address = int(self._CONFIG_START) + offset
                        mask = 0xFE if address == int(Ads1299Register.CONFIG3) else 0xFF
                        if (observed & mask) != (expected & mask):
                            raise Ads1299ReadbackError(
                                f"ADS{adc} register 0x{address:02X} readback "
                                f"0x{observed:02X} != 0x{expected:02X} (mask 0x{mask:02X})"
                            )
                self._transition(BringupPhase.ENABLE_CONTINUOUS)
            elif self.phase is BringupPhase.ENABLE_CONTINUOUS:
                self.adapter.send_command_all(Ads1299Command.RDATAC)
                self._transition(BringupPhase.START_SYNCHRONOUS)
            elif self.phase is BringupPhase.START_SYNCHRONOUS:
                self.adapter.wire.set_start(True)
                self.adapter.delay_clock_cycles(T_START_HIGH_CLOCKS)
                self._transition(BringupPhase.DISCARD_SETTLING_FRAMES)
            elif self.phase is BringupPhase.DISCARD_SETTLING_FRAMES:
                for _ in range(3):
                    self.receiver.capture_pair(max_drdy_polls=self.max_drdy_polls)
                    self.receiver.counters.discarded_settling_frames += 1
                self.stable_pair = self.receiver.capture_pair(
                    max_drdy_polls=self.max_drdy_polls
                )
                self._transition(BringupPhase.COMPLETE)
                self._finish()
        except (OSError, ValueError, RegisterAccessError, Ads1299ProtocolError) as exc:
            self.error = str(exc)
            try:
                self.adapter.wire.set_start(False)
            except Exception:
                pass
            self._transition(BringupPhase.FAILED)
            self._finish()
        return self.phase

    def _transition(self, phase: BringupPhase) -> None:
        self.phase = phase
        self.phase_history.append(phase)

    def _finish(self) -> None:
        self.report = BringupReport(
            phase=self.phase,
            ids=dict(self.ids),
            stable_pair=self.stable_pair,
            error=self.error,
        )
