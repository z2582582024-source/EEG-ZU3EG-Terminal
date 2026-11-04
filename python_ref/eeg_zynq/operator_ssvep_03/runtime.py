"""One owner per live stream; bounded queues/storage, explicit normal lifecycle.

No target operation on import. No reconnect, automatic re-ARM, fault reset, or
target actions from Status/Logs. A deployed version needs one local activation
receipt; individual operator Starts then generate their own UUIDs automatically.
"""
from __future__ import annotations
from collections import deque
from dataclasses import dataclass
from pathlib import Path
import hashlib
import json
import os
import queue
import re
import select
import socket
import subprocess
import threading
import time
import uuid
import zlib

from ..continuous.protocol import (Message, Decoder, ProtocolError, decode_raw,
    validate_hello, validate_live_observation, json_bytes, LIVE, SIMULATED,
    HELLO, START, DATA, HEARTBEAT, STOP, END, ERROR, CHANNELS, RATE)

ROOT=Path(__file__).resolve().parents[3]
BUILD=ROOT/'build/p5b_operator_v2'
RUNS=BUILD/'runs'
SCOPE=BUILD/'ssvep_03'
RUNS=SCOPE/'runs'
SIM_RUNS=SCOPE/'simulated_runs'
CONTROL=ROOT/'build/p5b_operator_control'
_hardware_guard=threading.RLock()
_hardware_holders=[]
_hardware_references=0


@dataclass(frozen=True)
class RecordedBlock:
    """Host logical sequence survives NET1's uint32 wrap without mutating it."""
    first:int
    frames:int
    payload:bytes


def atomic_json(path, value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_name(path.name+'.next')
    with temp.open('w',encoding='utf-8',newline='\n') as f:
        json.dump(value,f,ensure_ascii=False,separators=(',',':'))
        f.flush()
    os.replace(temp,path)


def read_json(path, default=None):
    try:return json.loads(Path(path).read_text(encoding='utf-8'))
    except FileNotFoundError:return default


class OwnerLock:
    def __init__(self, path):
        self.file=Path(path).open('a+b')
        if not self.file.tell():self.file.write(b'0');self.file.flush()
        self.file.seek(0)
        try:
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(self.file.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(self.file,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError:
            self.file.close();raise RuntimeError('ANOTHER_ACQUISITION_OWNER')

    def close(self):self.file.close()


class HardwareLease:
    """One cooperating target owner across deployment and daily operation.

    Hold legacy v1 lock files too, so its frozen entry cannot acquire a new
    capture/deployment owner while v2 owns the board. Unknown state is latched
    independently of raw recording. Status/Stop may inspect/revoke that state;
    they never clear it or authorize another Start.
    """
    def __init__(self,*,allow_unknown=False):
        global _hardware_references
        self.closed=True
        with _hardware_guard:
            CONTROL.mkdir(parents=True,exist_ok=True)
            if not allow_unknown and (CONTROL/'unknown.json').exists():
                require_reviewed_cold_recovery()
            if not _hardware_references:
                try:
                    for path in (CONTROL/'owner.lock',
                        ROOT/'build/p5b_operator_v1/runs/owner.lock',
                        ROOT/'build/p5b_operator_v1/deployment_authorities/owner.lock',
                        BUILD/'deployment_authorities/owner.lock'):
                        path.parent.mkdir(parents=True,exist_ok=True)
                        _hardware_holders.append(OwnerLock(path))
                    if not allow_unknown:
                        for name in ('active.json','fault.json'):
                            if (ROOT/'build/p5b_operator_v1/runs'/name).exists():
                                raise RuntimeError('LEGACY_OPERATOR_STATE_REQUIRES_REVIEW')
                except BaseException:
                    for holder in reversed(_hardware_holders):holder.close()
                    _hardware_holders.clear()
                    raise
            _hardware_references+=1;self.closed=False

    def close(self):
        global _hardware_references
        with _hardware_guard:
            if self.closed:return
            self.closed=True;_hardware_references-=1
            if not _hardware_references:
                for holder in reversed(_hardware_holders):holder.close()
                _hardware_holders.clear()

    def __enter__(self):return self
    def __exit__(self,*args):self.close()


class SegmentedRecorder:
    """Never deletes/overwrites raw; exhaustion ends recording, not acquisition.

    Recording Off retains a bounded latest view and cumulative summary only.
    There is intentionally no growing target raw file or host wire duplicate.
    """
    def __init__(self,folder,recording='off',*,segment_bytes=32*1024*1024,
                 limit_bytes=2*1024*1024*1024,reserve_bytes=128*1024*1024):
        self.folder=Path(folder);self.recording=recording
        self.segment_bytes=segment_bytes;self.limit_bytes=limit_bytes;self.reserve_bytes=reserve_bytes
        self.total=self.segment_size=0;self.file=None;self.segments=[]
        self.sha=None;self.crc=0
        if recording not in ('off','segments'):raise ValueError('RECORDING_MODE')
        if segment_bytes<3072 or limit_bytes<segment_bytes:raise ValueError('RECORDING_LIMITS')

    def _finish(self):
        if self.file:
            current=self.file;self.file=None
            try:current.flush();os.fsync(current.fileno())
            finally:current.close()
            self.segments[-1].update(bytes=self.segment_size,sha256=self.sha.hexdigest(),crc32=f'{self.crc:08x}')
            atomic_json(self.folder/'segments.json',self.segments)

    def append(self,message):
        if self.recording=='off':return
        blob=message.payload
        if self.total+len(blob)>self.limit_bytes:raise OSError('RECORD_QUOTA_RECORDING_ONLY_NO_OVERWRITE')
        import shutil
        if shutil.disk_usage(self.folder).free<self.reserve_bytes+len(blob):raise OSError('DISK_RESERVE_RECORDING_ONLY')
        first=message.first
        if self.file and self.segments[-1]['last_sequence']+1!=first:self._finish()
        if self.file and self.segment_size+len(blob)>self.segment_bytes:self._finish()
        if self.file is None:
            n=len(self.segments)
            if n>=4096:raise OSError('SEGMENT_INDEX_LIMIT_RECORDING_ONLY')
            self.file=(self.folder/f'raw_{n:04d}.bin').open('xb',buffering=0)
            self.segments.append(dict(file=f'raw_{n:04d}.bin',first_sequence=first,last_sequence=None))
            self.segment_size=0;self.sha=hashlib.sha256();self.crc=0
        remaining=memoryview(blob)
        while remaining:
            written=self.file.write(remaining)
            if not written:raise OSError('SHORT_RECORD_WRITE')
            remaining=remaining[written:]
        self.sha.update(blob);self.crc=zlib.crc32(blob,self.crc)
        self.segment_size+=len(blob);self.total+=len(blob)
        self.segments[-1]['last_sequence']=first+message.frames-1

    def close(self):self._finish()

    def references(self,first,last):
        """Index already written raw records without creating a duplicate file.

        An open segment is provisional until its close/hash is recorded. This
        is an observation, not a claim of durable evidence on a failing disk.
        """
        result=[];expected=first
        for entry in tuple(self.segments):
            row=dict(entry);end=row.get('last_sequence')
            if end is None or end<expected:continue
            if row['first_sequence']>expected:return []
            stop=min(last,end)
            result.append(dict(file=row['file'],offset_bytes=(expected-row['first_sequence'])*96,
                first_sequence=expected,last_sequence=stop,finalized='sha256' in row,
                segment_sha256=row.get('sha256')))
            expected=stop+1
            if expected>last:return result
        return []


class Session:
    def __init__(self,folder,tag,*,mode=LIVE,recording='off',host='192.168.137.2',port=47124,
                 launch=None,retirement=None,recorder_options=None,persist_metadata=True,stop_path=None):
        self.folder=Path(folder);self.tag=tag;self.mode=mode;self.recording=recording
        self.host=host;self.port=port;self.launch=launch;self.retirement=retirement
        self.recorder_options=recorder_options or {}
        self.lock=threading.RLock();self.samples=deque(maxlen=8192)
        self.anchors=deque(maxlen=256)
        self.sums=[0]*16;self.saturated=[0]*16;self.frames=0;self.crc=0
        self.expected=0;self.next_index=0;self.status={};self.failure=None;self.clean=False
        self.state='CREATED';self.source='DISCONNECTED';self.last_data=None;self.stop_reason=None
        self.done=threading.Event();self.stop_requested=threading.Event();self.ui_tick=time.monotonic()
        self.thread=None;self.writer=None;self.write_queue=queue.Queue(maxsize=32)
        self.queue_high=0;self.writer_failure=None;self.view_skipped=0;self.target_end=None
        self.want_recording=recording=='segments';self.recording_state='RECORDING' if self.want_recording else 'OFF'
        self.unsaved_frames=0;self.recording_events=deque(maxlen=128);self.recording_toggles=0
        self.recorder=None;self.logging_error=None
        self.persist_metadata=persist_metadata;self.stop_path=stop_path
        if not persist_metadata:
            self.logging_error='RUN_LOG_ARCHIVE_FULL; current run identity/status in memory only'
            self._disable_recording('RUN_LOG_ARCHIVE_FULL_RECORDING_ONLY')

    def start(self):
        if self.thread:raise RuntimeError('NO_SECOND_START_WITHIN_RUN')
        if self.persist_metadata:
            try:self.folder.mkdir(parents=True,exist_ok=False)
            except OSError as exc:
                self.persist_metadata=False;self.logging_error=repr(exc)
                self._disable_recording('RUN_FOLDER_UNAVAILABLE_RECORDING_ONLY')
        self.log_json('start.json',dict(run_id=self.tag.hex(),mode=self.mode,recording=self.recording,
            created_unix_ns=time.time_ns(),time_basis='DESIGN_INTERVAL_NOT_MEASURED_ADC_TIME',
            channels=list(CHANNELS),target_raw_saved=False))
        self.thread=threading.Thread(target=self._run,name='operator-NET1-owner',daemon=True);self.thread.start()

    def touch_ui(self):self.ui_tick=time.monotonic()

    def stop(self,reason='USER_STOP'):
        self.stop_reason=self.stop_reason or reason;self.stop_requested.set()

    def set_recording(self,enabled):
        """Operator control; storage trouble never requests capture Stop."""
        if enabled and not self.persist_metadata:
            self._disable_recording('METADATA_STORAGE_UNAVAILABLE_RECORDING_ONLY');return False
        try:self.write_queue.put_nowait(('on' if enabled else 'off',None))
        except queue.Full:
            self._disable_recording('RECORDING_QUEUE_FULL');return False
        self.recording_toggles+=1;self.want_recording=bool(enabled)
        self.recording_state='RECORDING_REQUESTED' if enabled else 'OFF_DRAIN_PENDING'
        self.recording_events.append(dict(frame=self.frames,event='ON' if enabled else 'OFF',monotonic_ns=time.monotonic_ns()))
        return True

    def _disable_recording(self,reason):
        self.want_recording=False;self.recording_state='STOPPED_RECORDING_ONLY';self.writer_failure=reason
        self.recording_events.append(dict(frame=self.frames,event=reason,monotonic_ns=time.monotonic_ns()))

    def window_recording(self,first,last):
        if not self.want_recording or self.writer_failure or self.recorder is None:return []
        return self.recorder.references(first,last)

    def log_json(self,name,value):
        if not self.persist_metadata:return False
        try:atomic_json(self.folder/name,value);return True
        except OSError as exc:
            self.logging_error=repr(exc);self._disable_recording('METADATA_STORAGE_UNAVAILABLE')
            return False

    def snapshot(self,rows=True):
        with self.lock:
            fresh=self.last_data is not None and time.monotonic()-self.last_data<1
            value=dict(run_id=self.tag.hex(),state=self.state,source=self.source if fresh or self.done.is_set() else 'STALE',
                recording=self.recording_state,raw_saved_complete=getattr(self,'saved_bytes',0)==self.frames*96,frames=self.frames,
                raw_crc32=f'{self.crc:08x}',means=[s/max(1,self.frames) for s in self.sums],
                saturation=list(self.saturated),seq_gaps=0 if not self.failure else None,
                queue_high=self.queue_high,queue_current=self.write_queue.qsize(),
                fresh=fresh,data_age_seconds=None if self.last_data is None else time.monotonic()-self.last_data,
                failure=self.failure,stop_reason=self.stop_reason,clean=self.clean,target=dict(self.status),
                saved_bytes=getattr(self,'saved_bytes',0),raw_not_saved=self.unsaved_frames>0,
                recording_error=self.writer_failure,logging_error=self.logging_error,
                metadata_persistent=self.persist_metadata,
                recording_events=list(self.recording_events))
            if rows:
                value['samples']=list(self.samples);value['dma_completion_anchors']=list(self.anchors)
            return value

    def _write(self):
        recorder=SegmentedRecorder(self.folder,'segments',**self.recorder_options)
        self.recorder=recorder
        enabled=self.want_recording
        try:
            while True:
                item=self.write_queue.get()
                try:
                    if item is None:break
                    command,message=item
                    if command=='on':enabled=True;self.writer_failure=None;self.recording_state='RECORDING'
                    elif command=='off':enabled=False;recorder._finish();self.recording_state='OFF'
                    elif enabled:
                        recorder.append(message);self.saved_bytes=recorder.total
                    else:self.unsaved_frames+=message.frames
                except BaseException as exc:
                    enabled=False
                    if item is not None and item[1] is not None:self.unsaved_frames+=item[1].frames
                    self._disable_recording(repr(exc))
                finally:self.write_queue.task_done()
        finally:
            try:recorder.close()
            except BaseException as exc:self._disable_recording(repr(exc))

    def _run(self):
        sock=None;hello=False;end=False;start_sent=False;stopping=None
        self.writer=threading.Thread(target=self._write,name='operator-bounded-recorder',daemon=True);self.writer.start()
        try:
            self.state='STARTING_TARGET'
            if self.launch:self.launch(self.tag.hex(),self.folder)
            if self.stop_requested.is_set():
                # Still connect once to request a provable cancellation of the owned helper.
                self.state='CANCEL_PENDING'
            sock=socket.create_connection((self.host,self.port),timeout=3)
            sock.settimeout(.5);sock.setsockopt(socket.IPPROTO_TCP,socket.TCP_NODELAY,1)
            decoder=Decoder(self.tag,self.mode);last_message=time.monotonic();last_heartbeat=0.
            while not end:
                now=time.monotonic()
                if now-self.ui_tick>2:self.stop('UI_HEARTBEAT_EXPIRED')
                if (self.folder/'stop.request').exists():self.stop('USER_STOP_COMMAND')
                if self.stop_path:
                    try:requested=read_json(self.stop_path,{})
                    except (OSError,ValueError):requested={}
                    if requested.get('run_id')==self.tag.hex():self.stop('USER_STOP_COMMAND')
                if self.stop_requested.is_set() and hello and stopping is None:
                    sock.sendall(Message(STOP,self.mode,self.tag).encode());stopping=now
                    self.state='STOPPING_DRAINING_WAIT_END'
                if stopping and now-stopping>4:raise TimeoutError('TARGET_STOP_RECEIPT_MISSING')
                if hello and now-last_heartbeat>=.25:
                    sock.sendall(Message(HEARTBEAT,self.mode,self.tag).encode());last_heartbeat=now
                ready,_,errors=select.select([sock],[],[sock],.02)
                if errors:raise ConnectionError('TCP_EXCEPTION')
                if not ready:
                    if now-last_message>2:raise TimeoutError('TARGET_HEARTBEAT_EXPIRED')
                    continue
                blob=sock.recv(16384)
                if not blob:raise ConnectionError('EOF_WITHOUT_END; target state UNKNOWN')
                last_message=now
                for m in decoder.feed(blob):
                    if end:raise ProtocolError('TRAILING_MESSAGE_AFTER_END')
                    if m.kind==HELLO:
                        if hello:raise ProtocolError('DUPLICATE_HELLO')
                        info=json.loads(m.payload);validate_hello(info,self.mode)
                        if self.mode==LIVE and info.get('operator_version')!=1:raise ProtocolError('OPERATOR_TARGET_REQUIRED')
                        self.status.update(info);hello=True
                        self.log_json('hello.json',info)
                        if not self.stop_requested.is_set():
                            sock.sendall(Message(START,self.mode,self.tag,payload=json_bytes({'max_seconds':0})).encode())
                            start_sent=True;self.state='START_SENT_WAIT_DATA'
                    elif m.kind==HEARTBEAT:
                        self.status.update(json.loads(m.payload) if m.payload else {})
                        presentation=self.status.get('presentation')
                        if presentation and (m.target_ns-presentation.get('last_vblank_ns',0)>.5e9 or m.target_ns<presentation.get('last_vblank_ns',0)):
                            presentation['timing_valid']=False
                    elif m.kind==DATA:
                        if not start_sent or m.index!=(self.next_index&0xffffffff) or m.first!=(self.expected&0xffffffff) or m.frames!=32:
                            raise ProtocolError('SEQUENCE_OR_LIFECYCLE')
                        if self.mode==LIVE:validate_live_observation(self.status)
                        rows=decode_raw(m.payload,self.expected&0xffffffff)
                        if self.want_recording:
                            try:self.write_queue.put_nowait(('data',RecordedBlock(self.expected,m.frames,m.payload)))
                            except queue.Full:
                                self._disable_recording('RECORDER_BACKPRESSURE_RECORDING_ONLY')
                                self.unsaved_frames+=m.frames
                        else:self.unsaved_frames+=m.frames
                        self.queue_high=max(self.queue_high,self.write_queue.qsize())
                        with self.lock:
                            self.anchors.append(dict(first_sequence=self.expected,frames=m.frames,
                                dma_completion_monotonic_ns=m.target_ns))
                            self.samples.extend(rows);self.frames+=32;self.crc=zlib.crc32(m.payload,self.crc)
                            for r in rows:
                                for i,v in enumerate(r):self.sums[i]+=v;self.saturated[i]+=int(v in (-8388608,8388607))
                            self.last_data=now
                        self.expected+=32;self.next_index+=1
                        self.source='LIVE_STREAM' if self.mode==LIVE else 'SIMULATED'
                        self.state='RUNNING' if stopping is None else 'STOPPING_DRAINING_WAIT_END'
                    elif m.kind in (END,ERROR):
                        receipt=json.loads(m.payload);self.status.update(receipt);self.target_end=receipt
                        self.log_json('target_receipt.json',receipt)
                        not_started=not start_sent and not self.frames and receipt.get('capture_started') is False
                        if m.kind==ERROR or receipt.get('frames')!=self.frames or receipt.get('raw_crc32')!=f'{self.crc:08x}' or receipt.get('error') or receipt.get('ownership_uncertain') or receipt.get('core_errors') or receipt.get('dma_errors'):
                            raise ProtocolError('TARGET_TERMINAL_MISMATCH')
                        if not not_started and (receipt.get('gpio_revoked') is not True or receipt.get('shutdown_confirmed') is not True):
                            raise ProtocolError('TARGET_NOT_QUIESCENT')
                        # No further keepalives after a validated END. The target
                        # drains in-flight STOP/HEARTBEAT frames and awaits EOF;
                        # send our EOF before awaiting its monitor (no deadlock).
                        sock.shutdown(socket.SHUT_WR)
                        end=True
                    else:raise ProtocolError('UNEXPECTED_TARGET_MESSAGE')
                if end and decoder.pending:raise ProtocolError('TRAILING_PARTIAL_MESSAGE')
            if self.retirement:self.retirement(self.tag.hex())
            self.clean=True;self.state='STOPPED_CONFIRMED' if start_sent else 'CANCELLED_BEFORE_ARM'
        except BaseException as exc:
            self.failure=repr(exc);self.clean=False;self.state='FAULT_NO_RETRY_TARGET_UNKNOWN'
        finally:
            if sock is not None:sock.close()
            # Bounded writer wait; a stuck disk cannot keep peer lease alive.
            try:self.write_queue.put(None,timeout=.5)
            except queue.Full:self.writer_failure=self.writer_failure or 'WRITER_QUEUE_STUCK'
            self.writer.join(3)
            if self.writer.is_alive():self.writer_failure='WRITER_DID_NOT_DRAIN'
            # Storage outcome is separate from the validated hardware lifecycle.
            # A record limit/error never converts a clean target Stop to a DMA fault.
            self.log_json('receipt.json',self.snapshot(rows=False))
            self.done.set()


def activation():
    p=SCOPE/'deployment.json';a=read_json(p)
    if not a or a.get('operator_enabled') is not True:raise RuntimeError('NOT_DEPLOYED_OR_ENABLED; local software only')
    binding=BUILD/'release_12/entry_binding.json'
    if a.get('binding_sha256')!=hashlib.sha256(binding.read_bytes()).hexdigest():raise RuntimeError('DEPLOYMENT_VERSION_MISMATCH')
    manifest=read_json(binding,{})
    for row in manifest.get('files',[]):
        if row.get('role')=='ssvep_host':
            path=ROOT/row['path']
            if hashlib.sha256(path.read_bytes()).hexdigest()!=row['sha256']:
                raise RuntimeError('DEPLOYED_HOST_SOURCE_CHANGED:'+row['path'])
    closure=read_json(ROOT/a['closure_path'])
    if not closure or closure.get('result')!='DEPLOYED_SERVICES_ENABLED_NO_CAPTURE' or closure.get('static_display_launch',{}).get('display_ready_static') is not True:
        raise RuntimeError('ACTUAL_DEPLOYMENT_AND_STATIC_READY_REQUIRED')
    if hashlib.sha256((ROOT/a['closure_path']).read_bytes()).hexdigest()!=a['closure_sha256']:
        raise RuntimeError('DEPLOYMENT_CLOSURE_IDENTITY_CHANGED')
    return a


def require_current_human_scope():
    a=activation();ready=read_json(SCOPE/'session_ready.json',{})
    if ready.get('binding_sha256')!=a['binding_sha256'] or ready.get('deployment_identity')!=a['deployment_identity']:
        raise PermissionError('CURRENT_SESSION_FACTS_REQUIRED')
    for name in ('adc_power_and_isolation_ready','person_connection_ready','exclusive_owner','visual_stimulus_applicable_and_agreed'):
        if ready.get(name) is not True:raise PermissionError('CURRENT_PHYSICAL_FACT_REQUIRED:'+name)
    return ready


def require_reviewed_cold_recovery():
    """An actual new cold deployment can supersede exactly one old UNKNOWN.

    Keep the original global evidence untouched. A new fault changes its hash
    and immediately blocks this proof. Neither software preparation nor an
    approval file alone satisfies this gate.
    """
    unknown_path=CONTROL/'unknown.json'
    raw=unknown_path.read_bytes();unknown=json.loads(raw)
    proof=read_json(SCOPE/'cold_recovery.json',{})
    a=activation()
    if (proof.get('source_unknown_sha256')!=hashlib.sha256(raw).hexdigest() or
            proof.get('source_run_id')!=unknown.get('run_id') or
            proof.get('deployment_identity')!=a['deployment_identity'] or
            proof.get('binding_sha256')!=a['binding_sha256'] or
            proof.get('closure_sha256')!=a['closure_sha256']):
        raise RuntimeError('GLOBAL_TARGET_STATE_UNKNOWN_REVIEW_REQUIRED')
    authorization=Path(proof.get('authorization_path','')).resolve()
    if not authorization.is_relative_to(BUILD):
        raise RuntimeError('COLD_RECOVERY_AUTHORIZATION_PATH')
    auth_raw=authorization.read_bytes();auth=json.loads(auth_raw)
    if hashlib.sha256(auth_raw).hexdigest()!=a['authorization_sha256']:
        raise RuntimeError('COLD_RECOVERY_AUTHORIZATION_IDENTITY')
    facts=auth.get('current_facts',{})
    if (facts.get('jtag')!='0000' or any(facts.get(k) is not True for k in
            ('adc_physically_off_isolated','person_disconnected','exclusive_hardware_owner','ps_pl_cold_por_no_intervention')) or
            auth.get('prior_unknown',{}).get('sha256')!=hashlib.sha256(raw).hexdigest()):
        raise RuntimeError('ACTUAL_REVIEWED_COLD_ENTRY_REQUIRED')
    return proof


def ssh_command(words,folder=None,*,input_data=None):
    a=activation()
    if not isinstance(words,list) or not words or words[0]!='eegctl':raise ValueError('RESTRICTED_SSH_COMMAND')
    if any(not isinstance(word,str) or not re.fullmatch(r'[A-Za-z0-9_-]+',word) for word in words):
        raise ValueError('RESTRICTED_SSH_ARGUMENT')
    if words==['eegctl','panel']:
        if a.get('static_display_enabled') is not True:raise PermissionError('DISPLAY_PANEL_NOT_ENABLED')
        if not isinstance(input_data,bytes) or not 64<=len(input_data)<=131072:raise ValueError('BOUNDED_PANEL_INPUT_REQUIRED')
    elif input_data is not None:raise ValueError('STDIN_ONLY_FOR_BOUNDED_PANEL')
    if words[:2]==['eegctl','start'] and a.get('capture_use') not in ('UNMANNED','CAP1_BASELINE'):
        raise PermissionError('CAPTURE_NOT_ENABLED_IN_FIXED_OPERATOR_SCOPE')
    if words[:2]==['eegctl','start']:require_current_human_scope()
    if words[:3]==['eegctl','display','start'] and a.get('static_display_enabled') is not True:
        raise PermissionError('NEW_DISPLAY_CONNECTION_NOT_ENABLED_IN_OPERATOR_SCOPE')
    if words[:3]==['eegctl','stimulus','start'] and a.get('stimulus_enabled') is not True:
        raise PermissionError('VISUAL_STIMULUS_NOT_ENABLED; static display remains available')
    if words[:3]==['eegctl','stimulus','start']:require_current_human_scope()
    command=['ssh','-F','none','-T','-p','22','-i',str(ROOT/'build/p5b_operator_v1/secrets/operator_ed25519'),
        '-o','BatchMode=yes','-o','IdentitiesOnly=yes','-o','StrictHostKeyChecking=yes',
        '-o','UserKnownHostsFile='+str(BUILD/'release_03/known_hosts'),'-o','ConnectTimeout=4',
        '-o','ConnectionAttempts=1','-o','ControlMaster=no','-o','ControlPath=none','-o','ClearAllForwardings=yes',
        '-o','ServerAliveInterval=2','-o','ServerAliveCountMax=2',a['ssh_destination'],' '.join(words)]
    inspect_or_stop=words in (['eegctl','status'],['eegctl','logs'],['eegctl','stop'],
        ['eegctl','display','status'],['eegctl','display','stop'],['eegctl','stimulus','stop'])
    action=SCOPE/'controls'/('command-'+time.strftime('%Y%m%d_%H%M%S')+'-'+uuid.uuid4().hex)
    action.mkdir(parents=True,exist_ok=False)
    atomic_json(action/'request.json',dict(command=words,stdin_bytes=len(input_data or b''),
        stdin_sha256=hashlib.sha256(input_data).hexdigest() if input_data else None,
        started_unix_ns=time.time_ns(),retry=False))
    try:
        with HardwareLease(allow_unknown=inspect_or_stop):
            p=subprocess.run(command,cwd=ROOT,input=input_data,capture_output=True,timeout=12,
                creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    except BaseException as exc:
        (action/'stdout.bin').write_bytes(getattr(exc,'stdout',None) or b'')
        (action/'stderr.bin').write_bytes(getattr(exc,'stderr',None) or b'')
        atomic_json(action/'result.json',dict(error=repr(exc),retry=False))
        if not inspect_or_stop:atomic_json(CONTROL/'unknown.json',dict(reason='SSH_COMMAND_UNCONFIRMED',command=words))
        raise
    (action/'stdout.bin').write_bytes(p.stdout);(action/'stderr.bin').write_bytes(p.stderr)
    atomic_json(action/'result.json',dict(returncode=p.returncode,retry=False))
    if folder and Path(folder).is_dir():
        try:
            (Path(folder)/'ssh_start.stdout').write_bytes(p.stdout);(Path(folder)/'ssh_start.stderr').write_bytes(p.stderr)
        except OSError:pass # Control success must not depend on log storage.
    if p.returncode:raise RuntimeError('SSH_CONTROL_FAILED:'+str(p.returncode)+':'+p.stderr.decode('utf-8','replace')[:500])
    if words[:2]==['eegctl','start'] and p.stdout!=b'NET1_READY_LISTEN_NO_ARM\n':raise RuntimeError('EXACT_HELPER_READY_REQUIRED')
    if words==['eegctl','display','start'] and p.stdout!=b'DISPLAY_READY_STATIC\n':raise RuntimeError('EXACT_STATIC_READY_REQUIRED')
    return p.stdout.decode('utf-8')


class Manager:
    def __init__(self,root=RUNS,simulation_root=SIM_RUNS):
        self.root=Path(root);self.root.mkdir(parents=True,exist_ok=True)
        self.simulation_root=Path(simulation_root);self.current_root=self.root
        self.owner=OwnerLock(self.root/'owner.lock');self.simulation_owner=None
        self.current=None;self.blocked=None;self.hardware=None
        if (self.root/'active.json').exists() or (self.root/'fault.json').exists():self.blocked='PREVIOUS_OWNER_OR_FAULT_REQUIRES_REVIEW'

    def start(self,recording='off',*,simulation=False,peer=None,recorder_options=None):
        self.poll()
        if self.blocked and not simulation:raise RuntimeError(self.blocked)
        if self.current and not self.current.done.is_set():raise RuntimeError('ALREADY_RUNNING')
        session_root=self.simulation_root if simulation else self.root
        session_root.mkdir(parents=True,exist_ok=True)
        if simulation:
            if self.simulation_owner is None:self.simulation_owner=OwnerLock(session_root/'owner.lock')
        persist_metadata=sum(1 for p in session_root.glob('run-*') if p.is_dir())<128
        if not simulation:
            activation();self.hardware=HardwareLease()
        tag=uuid.uuid4().bytes;folder=session_root/('run-'+time.strftime('%Y%m%d_%H%M%S')+'-'+tag.hex())
        launch=retirement=None
        if not simulation:
            launch=lambda identity,out:ssh_command(['eegctl','start',identity],out)
            def retirement(identity):
                receipt=json.loads(ssh_command(['eegctl','retired',identity]))
                if receipt.get('run_id')!=identity or receipt.get('state')!='STOPPED_CONFIRMED':
                    raise ProtocolError('TARGET_MONITOR_RETIREMENT_UNCONFIRMED')
        if simulation and peer is None:
            from ..continuous.stream import SimulatedPeer
            peer=SimulatedPeer(tag,seconds=0)
        session=Session(folder,tag,mode=SIMULATED if simulation else LIVE,recording=recording,
            host='127.0.0.1' if simulation else '192.168.137.2',port=peer.port if simulation else 47124,
            launch=launch,retirement=retirement,recorder_options=recorder_options,persist_metadata=persist_metadata,
            stop_path=session_root/'stop.request.json')
        self.peer=peer;self.current=session
        self.current_root=session_root
        try:atomic_json(session_root/'active.json',dict(folder=str(folder),run_id=tag.hex(),pid=os.getpid()))
        except OSError as exc:
            session.logging_error=repr(exc);session._disable_recording('METADATA_STORAGE_UNAVAILABLE_RECORDING_ONLY')
        try:session.start()
        except BaseException:
            self.blocked='START_FAILED_REVIEW_REQUIRED'
            if self.hardware:
                atomic_json(CONTROL/'unknown.json',dict(reason=self.blocked,run_id=tag.hex(),version=2))
                self.hardware.close();self.hardware=None
            raise
        return session

    def poll(self):
        if not self.current:return None
        s=self.current;session_root=self.current_root;snapshot=s.snapshot(False)
        try:atomic_json(session_root/'latest.json',snapshot)
        except OSError as exc:
            self.current._disable_recording('METADATA_STORAGE_UNAVAILABLE:'+repr(exc))
            self.logging_error=repr(exc)
        if s.done.is_set():
            if not s.clean and s.mode!=SIMULATED:self.blocked='FAULT_OR_UNKNOWN; no reconnect/re-ARM'
            if self.hardware:
                if not s.clean:
                    try:atomic_json(CONTROL/'unknown.json',dict(reason=self.blocked,run_id=s.tag.hex(),version=2,failure=s.failure))
                    except OSError:pass
                self.hardware.close();self.hardware=None
            try:
                if not s.clean and s.mode!=SIMULATED:atomic_json(session_root/'fault.json',dict(folder=str(s.folder),failure=s.failure))
                if (session_root/'active.json').exists():
                    if s.persist_metadata:os.replace(session_root/'active.json',s.folder/'lifecycle.closed.json')
                    else:(session_root/'active.json').unlink()
            except OSError as exc:
                self.logging_error=repr(exc)
                # This process retains the verified END in memory. Storage
                # failure cannot prohibit its next explicit Start. A fresh
                # process still refuses any stale persistent active marker.
            if self.simulation_owner:
                self.simulation_owner.close();self.simulation_owner=None
        return snapshot

    def close(self):
        if self.current and not self.current.done.is_set():
            self.current.stop('APPLICATION_EXIT')
            deadline=time.monotonic()+20
            while not self.current.done.wait(.1) and time.monotonic()<deadline:
                self.current.touch_ui()
            self.poll()
            # If still unknown, retain the active marker. A new process cannot
            # take it over merely because this application's lock was released.
        if self.hardware:
            try:atomic_json(CONTROL/'unknown.json',dict(reason='APPLICATION_EXIT_TARGET_NOT_RETIRED',version=2))
            except OSError:pass
            self.hardware.close();self.hardware=None
        if self.simulation_owner:
            self.simulation_owner.close();self.simulation_owner=None
        self.owner.close()
