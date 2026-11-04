"""Bounded in-memory serial ingress. No file, socket, device or START API.

The frozen kernel exposes a completed finite batch, not streaming DMA buffers.
Arrival time is a host observation; sample time is the unchanged design clock.
"""
from __future__ import annotations

from collections import deque
import datetime as dt
import hashlib
import json
from pathlib import Path
import threading
import time
import zlib

from eeg_zynq.emd1 import DESIGN_RATE_HZ, INTERVAL_NS, RAW_BYTES, parse_finite_emd1
from eeg_zynq.frame import SourceType
from eeg_zynq.electrodes import ELECTRODE_CHANNELS
from eeg_zynq.linux_dma import EXPECTED_INFO, INFO, STATUS, validate_terminal


class LiveError(RuntimeError):
    pass


def exclusive_json(path: Path, value: object) -> None:
    data = (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
    with path.open('xb') as stream:
        stream.write(data)
        stream.flush()


class LiveSource:
    """Exactly one validated live batch; pause only affects display consumption.

    Production activation is supplied exclusively by the bound Supervisor after
    actual Linux/resources/INFO/STATUS checks. Local tests use fixture=True and
    can never claim hardware provenance. There is no path-based input method.
    """
    design_sample_rate_hz = DESIGN_RATE_HZ

    def __init__(self, *, clock=time.monotonic, fixture=False):
        self.clock, self.fixture = clock, fixture
        self._lock = threading.RLock()
        self._queue = deque()
        self.active = False
        self.received = self.consumed = 0
        self.max_queue = 0
        self.complete = False
        self.failure = None
        self.stop_reason = None
        self.identity = None
        self.pre_info = self.pre_status = None
        self.post_status = None
        self.arrival = None
        self.cancel = threading.Event()
        self.display_ack = threading.Event()
        self.display_receipt = None
        self.trace = []

    @property
    def capacity(self):
        return 256

    @property
    def source_type(self):
        return SourceType.FILE_REPLAY if self.fixture else SourceType.ADS1299_DMA

    @property
    def source_id(self):
        return 'LOCAL_FIXTURE_NOT_HARDWARE' if self.fixture else 'LINUX_DMA_LIVE'

    @property
    def display_name(self):
        return self.source_id + ' · 有限窗口 · 整批完成后串口到达 · 内部测试/非人体'

    @property
    def is_real_hardware(self):
        return self.active and not self.fixture

    @property
    def finished(self):
        with self._lock:
            return self.complete and not self._queue

    def activate(self, identity, pre_info: bytes, pre_status: bytes):
        with self._lock:
            if self.active or self.failure or self.cancel.is_set():
                raise LiveError('SOURCE_ACTIVATION_ALREADY_CONSUMED_OR_CANCELLED')
            if not identity.get('event_id') or not identity.get('entry_binding_sha256') or not identity.get('permit_sha256'):
                raise LiveError('CURRENT_CONSUMED_AUTHORITY_IDENTITY_REQUIRED')
            if identity.get('evidence_scope') != ('LOCAL_FIXTURE_NOT_HARDWARE' if self.fixture else 'CURRENT_CONSUMED_EVENT_ACTUAL_LINUX_READY'):
                raise LiveError('SOURCE_PROVENANCE_SCOPE_CONFLICT')
            if INFO.unpack(pre_info) != EXPECTED_INFO:
                raise LiveError('SOURCE_INFO_IDENTITY_CONFLICT')
            status = STATUS.unpack(pre_status)
            if status[:4] != (0x31534445, 1, 128, 0) or status[4] or status[6] or status[8]:
                raise LiveError('SOURCE_READY_NOT_IDLE_VALID0_DMA0')
            self.identity = dict(identity)
            self.pre_info, self.pre_status = bytes(pre_info), bytes(pre_status)
            self.active = True
            self.trace.append(dict(kind='ACTUAL_READY' if not self.fixture else 'FIXTURE_READY', at=self.clock()))

    def publish_serial_batch(self, data: bytes, post_status: bytes, *, target_sha256: str, transaction: dict):
        """Called with bytes from the current console transaction, before disk publish."""
        try:
            with self._lock:
                if not self.active or self.complete or self.received or self.failure or self.cancel.is_set():
                    raise LiveError('ONE_LIVE_BATCH_ONLY_OR_CANCELLED')
                if transaction.get('event_id') != self.identity['event_id'] or transaction.get('transport') != 'EXCLUSIVE_CURRENT_PS_UART':
                    raise LiveError('CURRENT_SERIAL_TRANSACTION_REQUIRED')
                if transaction.get('origin') != 'CONSOLE_RECEIVED_BYTES_BEFORE_RAW_FILE_PUBLISH':
                    raise LiveError('FILES_ARE_NOT_LIVE_INGRESS')
                if hashlib.sha256(data).hexdigest() != target_sha256:
                    raise LiveError('LIVE_TRANSPORT_SHA_CONFLICT')
                status = STATUS.unpack(post_status)
                validate_terminal(status, data)
                records = parse_finite_emd1(data)
                if len(records) + len(self._queue) > self.capacity:
                    raise LiveError('BOUNDED_QUEUE_OVERFLOW_STOP_NO_DROP')
                packed = tuple(record.to_frame(self.source_type).pack() for record in records)
                self._queue.extend(packed)
                self.received = len(records)
                self.max_queue = max(self.max_queue, len(self._queue))
                self.post_status = bytes(post_status)
                self.arrival = dict(host_monotonic=self.clock(),
                    host_utc=dt.datetime.now(dt.timezone.utc).isoformat(), transaction=dict(transaction),
                    raw_sha256=target_sha256, raw_crc32=f'{zlib.crc32(data) & 0xffffffff:08x}')
                self.complete = True
                self.trace.append(dict(kind='BATCH_VALIDATED_ENQUEUED', frames=len(packed), at=self.clock()))
                return b''.join(packed)
        except BaseException as exc:
            self.fail(str(exc))
            raise

    def read_records(self, max_records):
        if type(max_records) is not int or max_records < 0:
            raise ValueError('NONNEGATIVE_INTEGER_READ_LIMIT_REQUIRED')
        with self._lock:
            if self.failure:
                raise LiveError(self.failure)
            if self.cancel.is_set():
                raise LiveError(self.stop_reason or 'USER_CANCELLED')
            result = tuple(self._queue.popleft() for _ in range(min(max_records, len(self._queue))))
            self.consumed += len(result)
            return result

    def state(self):
        with self._lock:
            return dict(source_id=self.source_id, active=self.active, frames_received=self.received,
                frames_consumed=self.consumed, queue=len(self._queue), capacity=self.capacity,
                queue_high_water=self.max_queue, complete=self.complete, failure=self.failure,
                stop_reason=self.stop_reason, arrival=self.arrival,
                semantic='FINITE_POST_COMPLETION_BATCH_NOT_CONTINUOUS_OR_PER_SAMPLE_STREAM')

    def acknowledge_display(self, receipt):
        with self._lock:
            if self.display_ack.is_set() or self.failure or self.cancel.is_set():
                raise LiveError('DISPLAY_RECEIPT_ALREADY_SET_OR_STOPPED')
            if not self.complete or self.consumed != 256 or self._queue:
                raise LiveError('DISPLAY_RECEIPT_BEFORE_ALL_FRAMES_CONSUMED')
            if receipt.get('last_sequence') != 255 or receipt.get('channels') != 16 or receipt.get('source_id') != self.source_id:
                raise LiveError('DISPLAY_RECEIPT_IDENTITY_OR_COMPLETENESS')
            self.display_receipt = dict(receipt)
            self.display_ack.set()

    def request_cancel(self, reason='USER_CANCELLED'):
        with self._lock:
            if self.stop_reason is None:
                self.stop_reason = reason
            self.cancel.set()

    def fail(self, reason):
        with self._lock:
            if self.failure is None:
                self.failure = reason
            if self.stop_reason is None:
                self.stop_reason = self.failure

    def reset(self):
        raise LiveError('NO_RESTART_OR_REPLAY_NEW_EVENT_REQUIRED')

    def close(self):
        self.request_cancel('WINDOW_CLOSED_NO_TARGET_HALT_CLAIM')


def save_live_records(folder: Path, source: LiveSource, records: bytes):
    """Independent evidence; loading this file later is always FILE_REPLAY."""
    path = folder / 'live_records.eegbin'
    with path.open('xb') as stream:
        stream.write(records)
        stream.flush()
    meta = dict(schema='eeg.p5b.linux_qt_live_source.v1', source_id=source.source_id,
        realtime_acquisition=False, fresh_current_session=not source.fixture,
        semantics='FINITE_DMA_BATCH_FORWARDED_DURING_CURRENT_SESSION_AFTER_SAFE_COMPLETION',
        identity=source.identity, frames=256, channels=16, raw_format='EMD1_V1_96_BYTES',
        raw_file=dict(path='raw_dma.bin', bytes=RAW_BYTES, sha256=source.arrival['raw_sha256'],
                      crc32=source.arrival['raw_crc32']),
        channel_mapping=[channel.to_dict() for channel in ELECTRODE_CHANNELS],
        qt_record_format='EegFrameV1_112_BYTES', design_sample_rate_hz=DESIGN_RATE_HZ,
        sample_timebase=dict(kind='DESIGN_RELATIVE_NOT_MEASURED', interval_ns=INTERVAL_NS),
        arrival=source.arrival, queue_policy='256_MAX_STOP_ON_OVERFLOW_NO_DROP_NO_FILL',
        processing='RAW_COUNTS_NO_SMOOTHING_INTERPOLATION_OR_SYNTHETIC_PACING',
        records=dict(path=path.name, bytes=len(records), sha256=hashlib.sha256(records).hexdigest()),
        pre_info_hex=source.pre_info.hex(), pre_status_hex=source.pre_status.hex(),
        post_status_hex=source.post_status.hex(), reopening_saved_data_must_be='FILE_REPLAY')
    exclusive_json(folder / 'live_source_manifest.json', meta)
    return meta
