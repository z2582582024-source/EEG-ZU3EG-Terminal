"""Incremental source with a hard queue bound, no file input and no pacing."""
from __future__ import annotations
from collections import deque
import datetime as dt
import hashlib,json,os,threading,time,zlib
from pathlib import Path
from eeg_zynq.emd1 import DESIGN_RATE_HZ
from eeg_zynq.frame import SourceType
from eeg_zynq.electrodes import ELECTRODE_CHANNELS
from . import protocol as w
from .profile import ADC_PROFILE, PROFILE_SHA256
from .assessment import assess


class ChunkSource:
    design_sample_rate_hz = DESIGN_RATE_HZ
    capacity = 128

    def __init__(self, *, fixture=False, clock=time.monotonic):
        self.fixture,self.clock=fixture,clock
        self.lock=threading.RLock()
        self.active=self.complete=False
        self.failure=self.stop_reason=None
        self.cancel=threading.Event();self.prefix_ack=threading.Event();self.display_ack=threading.Event()
        self.queue=deque();self.received=self.consumed=self.highwater=0
        self.identity=self.tag=self.parser=None
        self.raw=bytearray();self.records=bytearray();self.transactions=[]
        self.ready_status=self.post_status=self.prefix_receipt=self.final_receipt=None
        self.end_received_at=None
        self.powered_input_fact=None
        self.powered_input_fact_file=None
        self.input_plan=None

    @property
    def source_id(self):return 'LOCAL_FIXTURE_NOT_HARDWARE' if self.fixture else 'LINUX_DMA_EXTERNAL_LIVE'
    @property
    def source_type(self):return SourceType.FILE_REPLAY if self.fixture else SourceType.ADS1299_DMA
    @property
    def display_name(self):return self.source_id+' · 8×32分块/总256帧 · 外部输入候选/非人体'
    @property
    def is_real_hardware(self):return self.active and not self.fixture
    @property
    def finished(self):return self.complete and not self.queue

    def activate(self, identity, tag):
        with self.lock:
            if self.active or self.failure or self.cancel.is_set():raise w.WireError('NO_REACTIVATION')
            required=('event_id','entry_binding_sha256','session_sha256','permit_sha256','power_cycle')
            if any(not identity.get(key) for key in required):raise w.WireError('CONSUMED_CURRENT_IDENTITY_REQUIRED')
            if identity.get('evidence_scope') != ('LOCAL_FIXTURE_NOT_HARDWARE' if self.fixture else 'CURRENT_CONSUMED_EVENT_ACTUAL_LINUX_READY'):
                raise w.WireError('PROVENANCE_SCOPE_CONFLICT')
            if not self.fixture and (identity.get('adc_profile_sha256')!=PROFILE_SHA256 or not identity.get('external_input_plan_sha256') or not identity.get('external_input_facts')):
                raise w.WireError('EXTERNAL_INPUT_PROVENANCE_REQUIRED')
            self.identity=dict(identity);self.tag=bytes(tag);self.parser=w.Parser(self.tag);self.active=True

    def fail(self, reason):
        with self.lock:
            if self.failure is None:self.failure=str(reason)
            if self.stop_reason is None:self.stop_reason=self.failure
        self.cancel.set()

    def request_cancel(self, reason='USER_STOP'):
        with self.lock:
            if self.stop_reason is None:self.stop_reason=reason
        self.cancel.set()

    def receive(self, data, *, before_enqueue=None):
        with self.lock:
            if not self.active or self.failure or self.cancel.is_set():raise w.WireError('STOP_NO_MORE_PUBLISH')
            try:
                for packet in self.parser.feed(data):
                    if self.complete:raise w.WireError('PACKET_AFTER_END')
                    if packet.kind==w.READY:
                        if self.ready_status is not None or self.received or (packet.index,packet.first,packet.frames)!=(0,0,0):raise w.WireError('DUPLICATE_OR_OLD_READY')
                        if len(packet.payload)!=w.INFO.size+w.STATUS.size or w.INFO.unpack(packet.payload[:64])!=w.EXPECTED_INFO:raise w.WireError('CHUNK_INFO_IDENTITY')
                        self.ready_status=packet.payload[64:];w.validate_status(self.ready_status,complete=False)
                    elif packet.kind==w.DATA:
                        if self.ready_status is None or packet.index!=self.received//32 or (packet.first,packet.frames)!=(self.received,32):raise w.WireError('CHUNK_ORDER_OR_COUNT')
                        records,raw,meta=w.decode_chunk(packet.payload,packet.index)
                        if self.received+32>256 or len(self.queue)+32>self.capacity:raise w.WireError('QUEUE_OVERFLOW_STOP_NO_DROP')
                        if packet.target_ns!=(meta[12]|meta[13]<<32):raise w.WireError('CHUNK_MONOTONIC_IDENTITY')
                        if not packet.target_ns or (self.transactions and packet.target_ns<=self.transactions[-1]['target_completion_monotonic_ns']):raise w.WireError('NONMONOTONIC_TARGET_COMPLETION')
                        packed=tuple(record.to_frame(self.source_type).pack() for record in records)
                        transaction=dict(index=packet.index,first=packet.first,frames=32,host_monotonic=self.clock(),
                            host_utc=dt.datetime.now(dt.timezone.utc).isoformat(),target_completion_monotonic_ns=packet.target_ns,
                            target_next_dma_active=bool(meta[11]),raw_crc32=f'{meta[7]:08x}',raw_sha256=hashlib.sha256(raw).hexdigest())
                        if before_enqueue is not None:before_enqueue(raw,b''.join(packed),transaction)
                        self.raw.extend(raw);self.records.extend(b''.join(packed));self.transactions.append(transaction)
                        self.queue.extend(packed);self.received+=32;self.highwater=max(self.highwater,len(self.queue))
                    elif packet.kind==w.END:
                        if self.received!=256 or (packet.index,packet.first,packet.frames)!=(8,256,0):raise w.WireError('EARLY_END')
                        status=w.validate_status(packet.payload,complete=True)
                        if zlib.crc32(self.raw)&0xffffffff!=status[12]:raise w.WireError('END_TO_RAW_CRC')
                        self.post_status=packet.payload;self.end_received_at=self.clock();self.complete=True
                    elif packet.kind==w.ERROR:
                        raise w.WireError('TARGET_ERROR:'+packet.payload.hex())
                    else:raise w.WireError('TARGET_SENT_HOST_CONTROL')
            except BaseException as exc:
                self.fail(exc);raise

    def read_records(self, maximum):
        if type(maximum) is not int or maximum<0:raise ValueError('NONNEGATIVE_INTEGER_LIMIT_REQUIRED')
        with self.lock:
            if self.failure or self.cancel.is_set():raise w.WireError(self.failure or self.stop_reason)
            records=tuple(self.queue.popleft() for _ in range(min(maximum,len(self.queue))))
            self.consumed+=len(records)
            return records

    def acknowledge_prefix(self, receipt):
        with self.lock:
            if self.prefix_ack.is_set() or self.consumed<32 or self.failure or self.cancel.is_set():raise w.WireError('PREFIX_ACK_STATE')
            if receipt.get('source_id')!=self.source_id or receipt.get('frames',0)<32 or receipt.get('raw') is not True or receipt.get('rendered') is not True:
                raise w.WireError('PREFIX_RENDER_RECEIPT')
            self.prefix_receipt=dict(receipt,host_monotonic=self.clock(),end_already_received=self.complete)
            self.prefix_ack.set()

    def acknowledge_display(self, receipt):
        with self.lock:
            if self.display_ack.is_set() or not self.finished or self.consumed!=256 or not self.prefix_ack.is_set() or self.failure or self.cancel.is_set():raise w.WireError('FINAL_RENDER_STATE')
            if receipt.get('source_id')!=self.source_id or receipt.get('frames')!=256 or receipt.get('channels')!=16 or receipt.get('last_sequence')!=255:raise w.WireError('FINAL_RENDER_RECEIPT')
            self.final_receipt=dict(receipt);self.display_ack.set()

    def state(self):
        with self.lock:
            return dict(source_id=self.source_id,active=self.active,frames_received=self.received,frames_consumed=self.consumed,
                queue=len(self.queue),capacity=self.capacity,queue_high_water=self.highwater,complete=self.complete,
                failure=self.failure,stop_reason=self.stop_reason,chunks_received=self.received//32,prefix_rendered=self.prefix_ack.is_set(),
                semantic='BOUNDED_8X32_CHUNKS_SINGLE_ARM_256_FRAMES_NOT_INDEFINITE_CONTINUOUS')

    def reset(self):raise w.WireError('NEW_AUTHORIZED_EVENT_REQUIRED_NO_REPLAY')
    def close(self):self.request_cancel('SOURCE_CLOSED_NO_BOARD_HALT_CLAIM')


class Recorder:
    """Preserve partial evidence on failure; no final file before terminal checks."""
    def __init__(self, folder, source):
        self.folder,self.source=Path(folder),source
        self.raw=(self.folder/'raw_dma.bin.partial').open('xb')
        self.records=(self.folder/'live_records.eegbin.partial').open('xb')
        self.meta=(self.folder/'chunk_arrivals.jsonl').open('x',encoding='utf-8')
        self.closed=False

    def append(self,raw,records,transaction):
        for stream,value in ((self.raw,raw),(self.records,records)):
            if stream.write(value)!=len(value):raise OSError('SHORT_EVIDENCE_WRITE')
            stream.flush()
        self.meta.write(json.dumps(transaction,sort_keys=True)+'\n');self.meta.flush()

    def close(self):
        if not self.closed:
            self.closed=True
            for stream in (self.raw,self.records,self.meta):stream.close()

    def finalize(self):
        s=self.source
        if not s.complete or not s.display_ack.is_set() or s.failure:raise w.WireError('NO_FINAL_FILE_WITHOUT_COMPLETE_VALIDATED_RENDER')
        if not s.fixture and (not s.powered_input_fact or not s.powered_input_fact_file or not s.input_plan):
            raise w.WireError('CURRENT_INPUT_MEASUREMENT_PROVENANCE_REQUIRED')
        assessment=assess(bytes(s.raw),s.input_plan,s.powered_input_fact,fixture=s.fixture)
        for stream in (self.raw,self.records,self.meta):
            stream.flush();os.fsync(stream.fileno())
        self.close()
        for name in ('raw_dma.bin','live_records.eegbin'):
            pending=self.folder/(name+'.partial')
            os.link(pending,self.folder/name);pending.unlink()
        manifest=dict(schema='eeg.p5b.linux_dma_external_source.v1', adc_profile=ADC_PROFILE, adc_profile_sha256=PROFILE_SHA256, external_input=True, human_or_cap=False, external_signal_acceptance=assessment['result'],source_id=s.source_id,
            observed_adc_configuration=w.observed_adc_configuration(s.post_status),
            powered_input_measurement=s.powered_input_fact,
            powered_input_fact_file=s.powered_input_fact_file,
            powered_input_fact_canonical_sha256=(hashlib.sha256(json.dumps(s.powered_input_fact,sort_keys=True,separators=(',',':'),ensure_ascii=True).encode('ascii')).hexdigest() if s.powered_input_fact else None),
            external_electrical_assessment=assessment,
            fresh_current_session=not s.fixture,identity=s.identity,bounded_frames=256,chunks=8,chunk_frames=32,
            transport='SAME_EXCLUSIVE_PS_UART_BINARY_460800',queue_policy='128_FRAMES_OVERFLOW_STOP_NO_DROP_NO_FILL',
            processing='RAW_COUNTS_NO_SMOOTHING_INTERPOLATION_OR_SYNTHETIC_PACING',
            design_sample_rate_hz=DESIGN_RATE_HZ,sample_timebase='DESIGN_RELATIVE_4096000NS_NOT_EXTERNAL_MEASUREMENT',
            target_timebase='LINUX_MONOTONIC_NS',host_timebase='WINDOWS_MONOTONIC_AND_UTC_NOT_CLOCK_SYNCHRONIZED',
            continuous_acquisition=False,channel_mapping=[channel.to_dict() for channel in ELECTRODE_CHANNELS],
            raw_bytes=len(s.raw),raw_crc32=f'{zlib.crc32(s.raw)&0xffffffff:08x}',raw_sha256=hashlib.sha256(s.raw).hexdigest(),
            qt_record_bytes=len(s.records),qt_record_sha256=hashlib.sha256(s.records).hexdigest(),
            chunks_received=s.transactions,prefix_display=s.prefix_receipt,final_display=s.final_receipt,
            ready_status_hex=s.ready_status.hex(),terminal_status_hex=s.post_status.hex(),reopening_saved_data_is='FILE_REPLAY')
        with (self.folder/'live_source_manifest.json').open('x',encoding='utf-8') as stream:json.dump(manifest,stream,ensure_ascii=False,indent=2)
        return manifest
