"""Acquisition, bounded recording and rendering snapshots have separate ownership.

The network worker is the only TCP owner. It records before publishing samples.
No automatic reconnect, resync, second START, interpolation or raw dropping.
"""
from __future__ import annotations

from collections import deque
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import select
import shutil
import socket
import threading
import time
import uuid
import zlib

from .protocol import (CHANNELS, CHUNK, DATA, Decoder, END, ERROR, FILE_REPLAY,
                       HEARTBEAT, HELLO, INTERVAL_NS, LIVE, Message, RAW, SIMULATED,
                       START, STOP, ProtocolError, decode_raw, json_bytes, validate_hello,
                       validate_live_observation)

PROJECT = Path(__file__).resolve().parents[3]
BUILD = PROJECT / "build/p5b_continuous_cap_v1"
MAX_SECONDS = 86400  # zero selects manual stop; resource/watchdog stops remain
MAX_RECORD_BYTES = 32 * 1024 * 1024
RESERVE_BYTES = 64 * 1024 * 1024


def inside_project(path):
    path = Path(path).resolve()
    if PROJECT not in path.parents:
        raise ValueError("OUTPUT_MUST_STAY_IN_PROJECT_SUBDIRECTORY")
    return path


class Recorder:
    def __init__(self, folder, mode, *, limit=MAX_RECORD_BYTES, free=shutil.disk_usage):
        self.folder, self.mode = inside_project(folder), mode
        self.folder.mkdir(parents=True, exist_ok=False)
        self.limit, self.free, self.total = limit, free, 0
        self.raw_hash, self.wire_hash = hashlib.sha256(), hashlib.sha256()
        self.raw_crc = 0
        self.raw_bytes = 0
        self.raw = (self.folder / "raw_dma.bin").open("xb")
        self.wire = (self.folder / "net1_messages.bin").open("xb")
        self.received = (self.folder / "tcp_received.bin").open("xb")
        self.received_hash = hashlib.sha256()
        self.received_bytes = 0
        self.closed = False

    def check(self, extra):
        if self.total + extra > self.limit:
            raise OSError("RECORD_LIMIT")
        if self.free(self.folder).free < RESERVE_BYTES + extra:
            raise OSError("DISK_RESERVE")

    @staticmethod
    def write_exact(file, data):
        if file.write(data) != len(data):
            raise OSError("SHORT_RECORD_WRITE")
        file.flush()

    def record(self, message):
        encoded = message.encode()
        extra = len(encoded) + (len(message.payload) if message.kind == DATA else 0)
        self.check(extra)
        self.write_exact(self.wire, encoded)
        self.wire_hash.update(encoded)
        if message.kind == DATA:
            self.write_exact(self.raw, message.payload)
            self.raw_hash.update(message.payload)
            self.raw_crc = zlib.crc32(message.payload, self.raw_crc)
            self.raw_bytes += len(message.payload)
        self.total += extra

    def receive(self, data):
        # Retain malformed/partial network evidence before protocol interpretation.
        self.check(len(data))
        self.write_exact(self.received, data)
        self.received_hash.update(data)
        self.received_bytes += len(data)
        self.total += len(data)

    def close(self, state):
        if self.closed:
            return
        self.closed = True
        errors = []
        for file in (self.raw, self.wire, self.received):
            try:
                file.flush()
                os.fsync(file.fileno())
            except OSError as exc:
                errors.append(repr(exc))
            finally:
                file.close()
        manifest = dict(schema="eeg.net1.record.v1", source_mode=self.mode,
                        created_at=dt.datetime.now(dt.timezone.utc).isoformat(),
                        raw_bytes=self.raw_bytes, raw_sha256=self.raw_hash.hexdigest(),
                        raw_crc32=f"{self.raw_crc:08x}", wire_sha256=self.wire_hash.hexdigest(),
                        received_bytes=self.received_bytes, received_sha256=self.received_hash.hexdigest(),
                        raw_transformed=False, interpolated_frames=0, write_errors=errors,
                        state=state)
        with (self.folder / "manifest.json").open("x", encoding="utf-8", newline="\n") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)


class ViewModel:
    def __init__(self):
        self.lock = threading.RLock()
        self.samples = deque(maxlen=8192)  # Display history only; raw is recorded first.
        self.saturated = [0] * 16
        self.sum_counts = [0] * 16
        self.frames = 0
        self.last_rx = None
        self.ui_tick = time.monotonic()
        self.mode, self.state = "DISCONNECTED", "IDLE"
        self.failure, self.stop_reason = None, None
        self.target_status = {}
        self.record_folder = None
        self.seq_gaps = 0
        self.chunks = 0
        self.started, self.done = threading.Event(), threading.Event()

    def append(self, rows):
        with self.lock:
            for values in rows:
                self.samples.append(values)
                for i, v in enumerate(values):
                    self.sum_counts[i] += v
                    self.saturated[i] += v in (-8388608, 8388607)
            self.frames += len(rows)
            self.chunks += 1
            self.last_rx = time.monotonic()

    def snapshot(self):
        with self.lock:
            return dict(samples=list(self.samples), frames=self.frames, chunks=self.chunks,
                        saturation=list(self.saturated), sums=list(self.sum_counts),
                        mode=self.mode, state=self.state, failure=self.failure,
                        stop_reason=self.stop_reason, target_status=dict(self.target_status),
                        fresh_seconds=None if self.last_rx is None else time.monotonic()-self.last_rx,
                        record_folder=str(self.record_folder), seq_gaps=self.seq_gaps)


class NetworkSession(ViewModel):
    def __init__(self, host, port, tag, *, mode=LIVE, seconds=60, folder=None):
        super().__init__()
        if not 0 <= seconds <= MAX_SECONDS or mode not in (LIVE, SIMULATED):
            raise ValueError("RUN_LIMIT_OR_MODE")
        self.host, self.port, self.tag = host, port, tag
        self.mode = "AWAITING_FRESH_HARDWARE" if mode == LIVE else "SIMULATED"
        self.wire_mode, self.seconds = mode, seconds
        self.folder = folder or BUILD / "runs" / (dt.datetime.now().strftime("%Y%m%d_%H%M%S_")+uuid.uuid4().hex[:8])
        self.start_request, self.stop_request = threading.Event(), threading.Event()
        self.abort_no_io = threading.Event()
        self.control_lock = threading.Lock()
        self.input_ready = threading.Event()
        if mode == SIMULATED:
            self.input_ready.set()
        self.thread = None

    def connect(self):
        if self.thread is not None:
            raise RuntimeError("NO_RECONNECT_OR_SECOND_SESSION")
        self.thread = threading.Thread(target=self._run, name="NET1-owner", daemon=True)
        self.thread.start()

    def start(self):
        if self.state != "READY" or self.start_request.is_set() or not self.input_ready.is_set():
            raise RuntimeError("NOT_READY_OR_SECOND_START")
        self.start_request.set()

    def stop(self, reason="USER_STOP"):
        self.stop_reason = self.stop_reason or reason
        self.stop_request.set()

    def abort(self, reason, *, latch=None):
        # A controller STOP is different from an ordinary user stop. Only close
        # the owned socket; never send another control after a STOP latch.
        with self.control_lock:
            if latch is not None:
                latch()
            self.failure = self.failure or reason
            self.abort_no_io.set()

    def send_control(self, sock, message):
        with self.control_lock:
            if self.abort_no_io.is_set():
                raise RuntimeError("STOP_NO_POST_STOP_CONTROL")
            sock.sendall(message.encode())

    def _run(self):
        recorder, sock = None, None
        started_at, stopping_at, ready_at = None, None, None
        target_end, start_sent = False, False
        expected, next_index = 0, 0
        try:
            self.state = "CONNECTING"
            recorder = Recorder(self.folder, "LIVE_STREAM" if self.wire_mode == LIVE else "SIMULATED")
            self.record_folder = recorder.folder
            sock = socket.create_connection((self.host, self.port), timeout=3)
            sock.settimeout(.5)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            decoder = Decoder(self.tag, self.wire_mode)
            last_packet, last_heartbeat = time.monotonic(), 0.
            hello = False
            while True:
                if self.abort_no_io.is_set():
                    raise RuntimeError("CONTROLLER_STOP_NO_MORE_NETWORK_IO:"+str(self.failure))
                now = time.monotonic()
                if now-self.ui_tick > 2:
                    self.stop("UI_WATCHDOG")
                if self.seconds and started_at and now-started_at >= self.seconds:
                    self.stop("MAX_SECONDS")
                if ready_at and not start_sent and now-ready_at > 600:
                    self.stop("READY_TIMEOUT")
                if self.stop_request.is_set() and stopping_at is None:
                    stopping_at = now
                    self.send_control(sock, Message(STOP, self.wire_mode, self.tag))
                    self.state = "STOPPING_WAIT_TARGET_RECEIPT"
                if stopping_at and now-stopping_at > 3:
                    raise TimeoutError("STOP_RECEIPT_TIMEOUT_TARGET_UNKNOWN")
                if self.start_request.is_set() and not start_sent and not stopping_at:
                    self.send_control(sock, Message(START, self.wire_mode, self.tag,
                                         payload=json_bytes({"max_seconds":self.seconds})))
                    start_sent = True
                    started_at = now
                    self.started.set()
                    self.state = "START_SENT_WAIT_FRESH_DATA"
                if hello and now-last_heartbeat >= .25 and not target_end:
                    self.send_control(sock, Message(HEARTBEAT, self.wire_mode, self.tag))
                    last_heartbeat = now
                readable, _, exceptional = select.select([sock], [], [sock], .02)
                if exceptional:
                    raise ConnectionError("TCP_EXCEPTION")
                if not readable:
                    timeout = 5 if started_at and expected == 0 else 2
                    if now-last_packet > timeout:
                        raise TimeoutError("STREAM_STALE")
                    continue
                data = sock.recv(16384)
                if not data:
                    raise ConnectionError("EOF_WITHOUT_TARGET_END")
                recorder.receive(data)
                last_packet = now
                for message in decoder.feed(data):
                    if self.abort_no_io.is_set():
                        raise RuntimeError("CONTROLLER_STOP_NO_MORE_NETWORK_IO")
                    if message.kind == HELLO:
                        if hello or start_sent:
                            raise ProtocolError("DUPLICATE_HELLO")
                        info = json.loads(message.payload)
                        validate_hello(info, self.wire_mode)
                        self.target_status = info
                        hello, ready_at, self.state = True, now, "READY"
                    elif message.kind == DATA:
                        if not start_sent or not hello or message.index != next_index or message.first != expected or message.frames != 32:
                            self.seq_gaps += 1
                            raise ProtocolError("SEQUENCE_INDEX_OR_STATE")
                        if self.wire_mode == LIVE:
                            validate_live_observation(self.target_status)
                        rows = decode_raw(message.payload, expected)
                        recorder.record(message)  # disk bound checked BEFORE display publication
                        self.append(rows)
                        expected += len(rows)
                        next_index += 1
                        self.mode = "LIVE_STREAM" if self.wire_mode == LIVE else "SIMULATED"
                        self.state = "RUNNING" if not stopping_at else "STOPPING_DRAINING"
                        continue
                    elif message.kind == HEARTBEAT:
                        self.target_status.update(json.loads(message.payload) if message.payload else {})
                    elif message.kind in (END, ERROR):
                        status = json.loads(message.payload)
                        self.target_status.update(status)
                        recorder.record(message)  # A failed target receipt is evidence too.
                        not_started = (not start_sent and expected == 0 and
                                       status.get("capture_started") is False)
                        if (message.kind == ERROR or (not not_started and (not status.get("gpio_revoked")
                                or not status.get("shutdown_confirmed"))) or status.get("frames") != expected
                                or status.get("core_errors", 0) or status.get("dma_errors", 0)
                                or status.get("ownership_uncertain", 0) or status.get("error", 0)
                                or status.get("raw_crc32") != f"{recorder.raw_crc:08x}"):
                            raise ProtocolError("TARGET_TERMINAL_NOT_CLEAN:"+str(status))
                        target_end = True
                        self.state = "CLOSED_NOT_STARTED_NO_GPIO_OBSERVATION" if not_started else "CLOSED_TARGET_QUIESCENT"
                        break
                    else:
                        raise ProtocolError("UNEXPECTED_TARGET_MESSAGE")
                    recorder.record(message)
                if target_end:
                    if decoder.pending:
                        raise ProtocolError("TRAILING_PARTIAL_MESSAGE")
                    break
        except BaseException as exc:
            self.failure = repr(exc)
            self.state = "FAULT_TARGET_STATE_UNCONFIRMED"
            # Close triggers the independent target lease watchdog. Never reconnect
            # or issue extra commands after the protocol has failed.
        finally:
            if sock is not None:
                sock.close()
            if recorder:
                try:
                    state = self.snapshot()
                    state.pop("samples")
                    recorder.close(state)
                except BaseException as exc:
                    self.failure = (self.failure or "") + "; FINAL_RECORD:" + repr(exc)
                    self.state = "FAULT_RECORD_CLOSE"
            self.done.set()


class ReplaySession(ViewModel):
    def __init__(self, path):
        super().__init__()
        self.path = Path(path).resolve(strict=True)
        if self.path.stat().st_size > 8*1024*1024 or self.path.stat().st_size % 96:
            raise ValueError("REPLAY_SIZE_BOUND")
        self.mode, self.state = "FILE_REPLAY", "SAVED_DATA_NOT_LIVE"
        self.raw = self.path.read_bytes()
        self.position = 0

    def advance(self):
        if self.position >= len(self.raw):
            self.state = "FILE_REPLAY_END_NO_LOOP"
            self.done.set()
            return
        blob = self.raw[self.position:self.position+3072]
        flags = RAW.unpack_from(blob)[-1]
        self.append(decode_raw(blob, self.position//96, continuous=flags == 0x103))
        self.position += len(blob)

    def stop(self, reason="USER_STOP_REPLAY"):
        self.state, self.stop_reason = "FILE_REPLAY_STOPPED", reason
        self.done.set()


class SimulatedPeer:
    """Loopback integration source, explicitly SIMULATED, never claims hardware."""
    def __init__(self, tag, *, seconds=60):
        self.tag, self.seconds = tag, seconds
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.port = self.listener.getsockname()[1]
        self.listener.listen(1)
        self.thread = threading.Thread(target=self.run, daemon=True, name="SIMULATED-loopback")
        self.thread.start()

    def run(self):
        sock = None
        try:
            self.listener.settimeout(10)
            sock, _ = self.listener.accept()
            self.listener.close()
            sock.settimeout(.5)
            decoder = Decoder(self.tag, SIMULATED)
            hello = dict(source_mode="SIMULATED", profile="CAP1_CONT1", channels=list(CHANNELS),
                         interval_ns=INTERVAL_NS, chunk_frames=32, time_basis="DESIGN_INTERVAL_NOT_MEASURED_ADC_TIME",
                         automatic_rearm=False, queue_high_water=0, dma_errors=0, core_errors=0,
                         discarded="SIMULATED_NOT_HARDWARE", recording="HOST_RAW_BEFORE_DISPLAY")
            sock.sendall(Message(HELLO, SIMULATED, self.tag, payload=json_bytes(hello)).encode())
            running, stopped, first, crc = False, False, 0, 0
            last_control, next_chunk, began = time.monotonic(), time.monotonic(), None
            while not stopped:
                now = time.monotonic()
                if now-last_control > 2:
                    raise TimeoutError("SIM_PEER_CONTROL_LEASE")
                if select.select([sock], [], [], .01)[0]:
                    data = sock.recv(16384)
                    if not data:
                        return
                    for m in decoder.feed(data):
                        last_control = now
                        if m.kind == START and not running:
                            running, began, next_chunk = True, now, now
                        elif m.kind == STOP:
                            stopped = True
                        elif m.kind != HEARTBEAT:
                            raise ProtocolError("SIM_CONTROL")
                if running and not stopped and now >= next_chunk:
                    raw = b"".join(RAW.pack(0x31444d45, 0x00600001, n, n, 0xc00000, 0xc00000,
                        *[int(250000*math.sin(n*.04+i)+i*100000) for i in range(16)], INTERVAL_NS, 0x103)
                        for n in range(first, first+32))
                    sock.sendall(Message(DATA, SIMULATED, self.tag, first//32, time.monotonic_ns(), first, 32, raw).encode())
                    crc = zlib.crc32(raw, crc)
                    first += 32
                    next_chunk += 32*INTERVAL_NS/1e9
                    if self.seconds and now-began >= self.seconds:
                        stopped = True
                elif not running and now-next_chunk > .25:
                    sock.sendall(Message(HEARTBEAT, SIMULATED, self.tag).encode())
                    next_chunk = now
            sock.sendall(Message(END, SIMULATED, self.tag, payload=json_bytes(
                dict(gpio_revoked=True, shutdown_confirmed=True, frames=first, raw_crc32=f"{crc:08x}",
                     simulated_receipt=True, reason="USER_STOP_OR_LIMIT"))).encode())
        except (OSError, TimeoutError, ProtocolError):
            pass
        finally:
            self.listener.close()
            if sock:
                sock.close()
