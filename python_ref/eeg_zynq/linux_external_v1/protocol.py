"""Session-bound binary packets, fixed memory bounds, no recovery/resync/retry."""
from __future__ import annotations

from dataclasses import dataclass
import struct
import zlib

from eeg_zynq.emd1 import INTERVAL_NS
from .record import ExternalRecord as Emd1Record
from .profile import ADC_PROFILE

MAGIC = b'EQEXT3\0\0'
HEADER = struct.Struct('<8s4H32s4IQ2I')
INFO = struct.Struct('<16I')
STATUS = struct.Struct('<4Ii65I')
CHUNK_META = struct.Struct('<16I')
EMD1 = struct.Struct('<6I16i2I')
READY, DATA, END, ERROR = 1, 2, 3, 4
START, DISPLAY_ACK, STOP, FINISH = 16, 17, 18, 19
CHUNK_FRAMES, CHUNKS, FRAMES, RAW_BYTES = 32, 8, 256, 24576
MAX_PAYLOAD = CHUNK_META.size + CHUNK_FRAMES * EMD1.size
MAX_PACKET = HEADER.size + MAX_PAYLOAD
RATE = 460800
EXPECTED_INFO = (0x334C5845, 3, 64, 0x80000000, 0x80010000, 256, 96,
                 INTERVAL_NS, 32, 3072, 8, 0x444D0300, 0, 0, 0, 0)


class WireError(RuntimeError):
    pass


@dataclass(frozen=True)
class Packet:
    kind: int
    tag: bytes
    index: int = 0
    first: int = 0
    frames: int = 0
    target_ns: int = 0
    flags: int = 0
    payload: bytes = b''

    def pack(self):
        if len(self.tag) != 32 or self.flags or len(self.payload) > MAX_PAYLOAD:
            raise WireError('PACKET_IDENTITY_OR_BOUNDS')
        raw = HEADER.pack(MAGIC, 3, HEADER.size, self.kind, self.flags, self.tag,
                          self.index, self.first, self.frames, len(self.payload),
                          self.target_ns, zlib.crc32(self.payload) & 0xffffffff, 0)
        return raw[:-4] + struct.pack('<I', zlib.crc32(raw[:-4]) & 0xffffffff) + self.payload


class Parser:
    def __init__(self, tag):
        if len(tag) != 32:
            raise WireError('SESSION_TAG_32_BYTES_REQUIRED')
        self.tag, self.buffer, self.failure = tag, bytearray(), None

    def fail(self, reason):
        if self.failure is None:
            self.failure = str(reason)
        raise WireError(self.failure)

    def feed(self, incoming):
        if self.failure:
            self.fail(self.failure)
        if not isinstance(incoming, bytes) or len(incoming) > 4096:
            self.fail('NATIVE_READ_BOUND_4096')
        self.buffer.extend(incoming)
        result = []
        try:
            while len(self.buffer) >= HEADER.size:
                header = bytes(self.buffer[:HEADER.size])
                values = HEADER.unpack(header)
                if values[:3] != (MAGIC, 3, HEADER.size) or values[4] or values[5] != self.tag:
                    raise WireError('HEADER_OR_CURRENT_SESSION_IDENTITY')
                if values[3] not in (READY, DATA, END, ERROR, START, DISPLAY_ACK, STOP, FINISH):
                    raise WireError('UNKNOWN_PACKET_KIND')
                if zlib.crc32(header[:-4]) & 0xffffffff != values[-1]:
                    raise WireError('HEADER_CRC')
                size = values[9]
                if size > MAX_PAYLOAD:
                    raise WireError('UNBOUNDED_PAYLOAD_REJECTED')
                if len(self.buffer) < HEADER.size + size:
                    break
                payload = bytes(self.buffer[HEADER.size:HEADER.size+size])
                if zlib.crc32(payload) & 0xffffffff != values[-2]:
                    raise WireError('PAYLOAD_CRC')
                del self.buffer[:HEADER.size+size]
                result.append(Packet(values[3], values[5], values[6], values[7], values[8], values[10], values[4], payload))
            if len(self.buffer) > MAX_PACKET:
                raise WireError('PARSER_BOUND')
            return tuple(result)
        except (WireError, struct.error) as exc:
            self.fail(exc)

    def finish(self):
        if self.failure or self.buffer:
            self.fail(self.failure or 'TRUNCATED_PACKET_AT_EOF')


def decode_chunk(payload, index):
    if len(payload) != MAX_PAYLOAD or not 0 <= index < CHUNKS:
        raise WireError('CHUNK_SIZE_OR_INDEX')
    meta = CHUNK_META.unpack(payload[:64])
    if meta[:7] != (0x334B5845, 3, MAX_PAYLOAD, index, index*32, 32, 3072):
        raise WireError('CHUNK_METADATA')
    if meta[8:11] != (1, 0, 1) or meta[14:] != (0, 0) or meta[11] != int(index<7):
        raise WireError('CHUNK_CPU_OWNERSHIP_OR_GUARDS')
    raw = payload[64:]
    if zlib.crc32(raw) & 0xffffffff != meta[7]:
        raise WireError('CHUNK_DRIVER_CRC')
    records = []
    for offset, values in enumerate(EMD1.iter_unpack(raw)):
        first = index*32+offset
        if values[:4] != (0x31444D45, 0x00600001, first, first):
            raise WireError('EMD1_SEQUENCE_OR_IDENTITY')
        if any(not 0 <= value <= 0xffffff or value >> 20 != 0xc for value in values[4:6]):
            raise WireError('ADS_STATUS_HEADER')
        if any(not -(1<<23) <= value < (1<<23) for value in values[6:22]):
            raise WireError('INT24_SIGN_EXTENSION')
        if values[22:] != (INTERVAL_NS, 2):
            raise WireError('DESIGN_TIME_OR_EXTERNAL_CONFIG_FLAGS')
        records.append(Emd1Record(first, first, *values[4:6], tuple(values[6:22])))
    return tuple(records), raw, meta


def validate_status(payload, *, complete):
    if len(payload) != STATUS.size:
        raise WireError('STATUS_BYTES')
    s = STATUS.unpack(payload)
    if s[:3] != (0x33535845, 3, STATUS.size) or s[32:35] != (32, 3072, 8) or (not complete and any(s[59:])):
        raise WireError('STATUS_IDENTITY')
    if not complete:
        if s[3] != 0 or s[4] or s[6] or s[8] or any(s[35:41]) or s[56] or s[57] or s[58]:
            raise WireError('READY_MUST_BE_IDLE_UNATTEMPTED_NO_DMA')
        return s
    if s[3] != 2 or s[4] or s[6:12] != (63, 1, 0, 1, 1, RAW_BYTES):
        raise WireError('TERMINAL_INTEGRITY_OR_OWNERSHIP')
    if (s[13] != 256 or s[14] != 0x0b or not s[15] & 2 or s[15] & 0x770
            or s[16] != 3072 or s[18] or s[20] or s[22]
            or s[19] & 0x1fb != 0x31 or s[17] & 0xc0 != 0xc0 or s[17] & 0x600 != 0x200):
        raise WireError('TERMINAL_DMA_OR_CORE_STATE')
    if s[35:37] != (8, 8) or s[37] != 8 or s[38:41] != (255, 0, 0) or s[57] or s[58]:
        raise WireError('CHUNK_COUNTS_OR_OWNERSHIP_UNKNOWN')
    if s[59:64] != (0x00200000, 0x00200000, 0x7fff, 0x7fff, 0x45585431):
        raise WireError('EXTERNAL_REFERENCE_BIAS_READBACK_NOT_PROVEN')
    if any(s[i] & 0xfffeffff != 0x00e0c096 for i in (64,67)) or any(s[i] for i in (65,66,68,69)):
        raise WireError('EXTERNAL_NORMAL_INPUT_CONFIG_READBACK_NOT_PROVEN')
    # ACK is sent only after the first 32 frames have actually rendered. The
    # worker then observes a fresh held DATA state with <256 paired frames.
    if s[42:44] != (1, 1) or not 1 <= s[44] < 8 or not 32 <= s[45] < 256 or s[46] != 1:
        raise WireError('DISPLAY_DURING_ACQUISITION_NOT_PROVEN')
    return s


def observed_adc_configuration(payload):
    """Decode actual RREG bytes, preserving read-only bits and expected separately.

    The valid mask is emitted only after the driver's one terminal snapshot and
    comparisons. An absent snapshot is UNKNOWN, never an all-zero measurement.
    """
    if payload is None:
        return dict(available=False, reason='NO_TERMINAL_STATUS', devices=[])
    if len(payload)!=STATUS.size:
        raise WireError('STATUS_BYTES')
    status=STATUS.unpack(payload)
    if status[:3]!=(0x33535845,3,STATUS.size):
        raise WireError('STATUS_IDENTITY')
    if not status[6] & (1<<5):
        return dict(available=False, reason='CONFIG_READBACK_VALID_BIT_NOT_SET', devices=[])
    devices=[]
    for device,extra,base,bitmap in ((1,59,64,61),(2,60,67,62)):
        low=sum(status[base+n]<<(32*n) for n in range(3))
        packed=low | (status[extra]<<88)
        registers=[]
        for expected in ADC_PROFILE['registers']:
            observed=(packed>>(8*expected['index']))&255
            registers.append(dict(address=expected['address'], observed=observed,
                expected=expected['value'], mask=expected['mask'],
                matches=(observed&expected['mask'])==(expected['value']&expected['mask'])))
        devices.append(dict(adc=device, pass_bitmap=status[bitmap], registers=registers))
    return dict(available=True, evidence='TERMINAL_DRIVER_STATUS_ACTUAL_ADC_RREG_BYTES',
        profile_id=f'{status[63]:08x}', devices=devices)
