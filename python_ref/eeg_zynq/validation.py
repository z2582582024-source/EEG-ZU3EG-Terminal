"""Stateful validation and error accounting for EegFrameV1 streams."""

from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass

from .ads1299 import FrameLengthError, parse_ads1299_frame
from .frame import (
    EegFrameV1,
    ErrorFlag,
    RecordCrcError,
    RecordFormatError,
    RecordLengthError,
    SourceType,
    combine_ads1299_frames,
)

DEFAULT_SEQUENCE_HISTORY_SIZE = 4096


@dataclass(slots=True)
class RawPairCounters:
    """Per-device counters at the replaceable raw ADS1299 input boundary."""

    pairs_received: int = 0
    adc1_frames_parsed: int = 0
    adc2_frames_parsed: int = 0
    adc1_missing_frames: int = 0
    adc2_missing_frames: int = 0
    adc1_length_errors: int = 0
    adc2_length_errors: int = 0
    adc1_status_header_errors: int = 0
    adc2_status_header_errors: int = 0

    def to_dict(self) -> dict[str, int]:
        return asdict(self)


class RawPairDecoder:
    """Convert an optional raw dual-ADC pair into one EegFrameV1.

    The same boundary can be fed by recorded raw bytes in simulation or by a
    future SPI/DMA adapter.  A missing or malformed ADC frame is zero-filled
    by ``combine_ads1299_frames`` and remains distinguishable through flags,
    presence bits, and per-device counters.
    """

    def __init__(self) -> None:
        self.counters = RawPairCounters()

    def decode(
        self,
        adc1_raw: bytes | bytearray | memoryview | None,
        adc2_raw: bytes | bytearray | memoryview | None,
        *,
        sequence: int,
        timestamp_ns: int,
        sample_rate_millihz: int,
        source: SourceType,
        extra_flags: ErrorFlag = ErrorFlag.NONE,
    ) -> EegFrameV1:
        if source is SourceType.ADS1299_DMA:
            raise ValueError(
                "ADS1299_DMA is reserved for a future verified hardware adapter; "
                "the generic raw-pair decoder cannot assert hardware provenance"
            )
        counters = self.counters
        counters.pairs_received += 1
        length_error = False

        if adc1_raw is None:
            adc1 = None
            counters.adc1_missing_frames += 1
        else:
            try:
                adc1 = parse_ads1299_frame(adc1_raw, validate_status_header=False)
            except FrameLengthError:
                adc1 = None
                length_error = True
                counters.adc1_length_errors += 1
            else:
                counters.adc1_frames_parsed += 1
                if not adc1.status_header_valid:
                    counters.adc1_status_header_errors += 1

        if adc2_raw is None:
            adc2 = None
            counters.adc2_missing_frames += 1
        else:
            try:
                adc2 = parse_ads1299_frame(adc2_raw, validate_status_header=False)
            except FrameLengthError:
                adc2 = None
                length_error = True
                counters.adc2_length_errors += 1
            else:
                counters.adc2_frames_parsed += 1
                if not adc2.status_header_valid:
                    counters.adc2_status_header_errors += 1

        if length_error:
            extra_flags |= ErrorFlag.RAW_LENGTH
        return combine_ads1299_frames(
            adc1,
            adc2,
            sequence=sequence,
            timestamp_ns=timestamp_ns,
            sample_rate_millihz=sample_rate_millihz,
            source=source,
            extra_flags=extra_flags,
        )


@dataclass(slots=True)
class ErrorCounters:
    records_received: int = 0
    valid_records: int = 0
    invalid_records: int = 0
    length_errors: int = 0
    crc_errors: int = 0
    format_errors: int = 0
    adc1_status_header_errors: int = 0
    adc2_status_header_errors: int = 0
    sequence_gap_frames: int = 0
    duplicate_records: int = 0
    out_of_order_records: int = 0
    adc1_timeout_records: int = 0
    adc2_timeout_records: int = 0
    sync_skew_records: int = 0

    def to_dict(self) -> dict[str, int]:
        return asdict(self)


class StreamValidator:
    """Validate records and account sequence anomalies without hiding faults.

    A forward jump is counted immediately as missing frames.  If an older
    frame arrives later it is additionally counted as out-of-order; the prior
    gap count is intentionally not decremented, preserving real-time evidence.
    """

    def __init__(
        self,
        *,
        expected_first_sequence: int | None = None,
        sequence_history_size: int = DEFAULT_SEQUENCE_HISTORY_SIZE,
    ) -> None:
        if expected_first_sequence is not None and expected_first_sequence < 0:
            raise ValueError("expected_first_sequence cannot be negative")
        if not isinstance(sequence_history_size, int) or isinstance(sequence_history_size, bool):
            raise TypeError("sequence_history_size must be an integer")
        if sequence_history_size < 1:
            raise ValueError("sequence_history_size must be positive")
        self.counters = ErrorCounters()
        self._expected_sequence = expected_first_sequence
        self._sequence_history_size = sequence_history_size
        self._sequence_history: deque[int] = deque()
        self._seen_sequences: set[int] = set()

    @property
    def sequence_history_size(self) -> int:
        """Maximum number of recent sequence numbers retained for duplicate checks."""

        return self._sequence_history_size

    @property
    def remembered_sequence_count(self) -> int:
        """Current bounded sequence-history occupancy, exposed for diagnostics/tests."""

        return len(self._sequence_history)

    def _remember_sequence(self, sequence: int) -> None:
        if sequence in self._seen_sequences:
            return
        if len(self._sequence_history) == self._sequence_history_size:
            expired = self._sequence_history.popleft()
            self._seen_sequences.remove(expired)
        self._sequence_history.append(sequence)
        self._seen_sequences.add(sequence)

    def observe_record(self, record: bytes | bytearray | memoryview) -> EegFrameV1 | None:
        counters = self.counters
        counters.records_received += 1
        try:
            frame = EegFrameV1.unpack(record)
        except RecordLengthError:
            counters.invalid_records += 1
            counters.length_errors += 1
            return None
        except RecordCrcError:
            counters.invalid_records += 1
            counters.crc_errors += 1
            return None
        except RecordFormatError:
            counters.invalid_records += 1
            counters.format_errors += 1
            return None

        counters.valid_records += 1
        if frame.adc_present_mask & 0x01 and frame.adc1_status >> 20 != 0xC:
            counters.adc1_status_header_errors += 1
        if frame.adc_present_mask & 0x02 and frame.adc2_status >> 20 != 0xC:
            counters.adc2_status_header_errors += 1

        if self._expected_sequence is None:
            self._expected_sequence = frame.sequence
        if frame.sequence in self._seen_sequences:
            counters.duplicate_records += 1
        elif frame.sequence < self._expected_sequence:
            counters.out_of_order_records += 1
        elif frame.sequence > self._expected_sequence:
            counters.sequence_gap_frames += frame.sequence - self._expected_sequence

        self._remember_sequence(frame.sequence)
        if frame.sequence >= self._expected_sequence:
            self._expected_sequence = frame.sequence + 1

        if frame.flags & ErrorFlag.ADC1_TIMEOUT:
            counters.adc1_timeout_records += 1
        if frame.flags & ErrorFlag.ADC2_TIMEOUT:
            counters.adc2_timeout_records += 1
        if frame.flags & ErrorFlag.SYNC_SKEW:
            counters.sync_skew_records += 1
        return frame
