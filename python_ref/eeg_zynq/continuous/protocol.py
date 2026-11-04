"""NET1: one TCP stream, bounded framing, CRCs, session identity; no resync."""
from __future__ import annotations

from dataclasses import dataclass
import json
import struct
import zlib

MAGIC = b"EEGNET1\0"
HEADER = struct.Struct("<8sHHII16sIQIIII")
RAW = struct.Struct("<6I16i2I")
HELLO, START, DATA, HEARTBEAT, STOP, END, ERROR = range(1, 8)
LIVE, SIMULATED, FILE_REPLAY = 1, 2, 4
MAX_PAYLOAD = 16384
CHUNK = 32
INTERVAL_NS = 4096000
RATE = 1e9 / INTERVAL_NS
CHANNELS = ("FC3", "FC4", "C3", "Cz", "C4", "CP3", "CPz", "CP4",
            "PO7", "PO3", "POz", "PO4", "PO8", "O1", "Oz", "O2")


class ProtocolError(ValueError):
    pass


@dataclass(frozen=True)
class Message:
    kind: int
    mode: int
    tag: bytes
    index: int = 0
    target_ns: int = 0
    first: int = 0
    frames: int = 0
    payload: bytes = b""

    def encode(self):
        if len(self.tag) != 16 or len(self.payload) > MAX_PAYLOAD:
            raise ProtocolError("TAG_OR_SIZE")
        header = HEADER.pack(MAGIC, 1, self.kind, self.mode, len(self.payload), self.tag,
                             self.index, self.target_ns, self.first, self.frames,
                             zlib.crc32(self.payload), 0)
        return header[:-4] + struct.pack("<I", zlib.crc32(header[:-4])) + self.payload


class Decoder:
    def __init__(self, tag: bytes, mode: int):
        self.tag, self.mode = tag, mode
        self.pending = bytearray()

    def feed(self, data):
        # A socket read is bounded to 16 KiB. Process coalesced messages promptly.
        if len(data) > 65536 or len(self.pending) + len(data) > 2 * (MAX_PAYLOAD + HEADER.size):
            raise ProtocolError("RX_BOUND")
        self.pending.extend(data)
        messages = []
        while len(self.pending) >= HEADER.size:
            h = HEADER.unpack_from(self.pending)
            if (h[0] != MAGIC or h[1] != 1 or h[2] not in range(1, 8)
                    or h[3] != self.mode or h[5] != self.tag or h[4] > MAX_PAYLOAD
                    or zlib.crc32(self.pending[:60]) != h[11]):
                raise ProtocolError("HEADER_IDENTITY_LENGTH_OR_CRC")
            end = HEADER.size + h[4]
            if len(self.pending) < end:
                break
            payload = bytes(self.pending[HEADER.size:end])
            if zlib.crc32(payload) != h[10]:
                raise ProtocolError("PAYLOAD_CRC")
            messages.append(Message(h[2], h[3], h[5], h[6], h[7], h[8], h[9], payload))
            del self.pending[:end]
        return messages


def json_bytes(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def decode_raw(blob, first, *, continuous=True):
    if not blob or len(blob) % RAW.size or len(blob) > CHUNK * RAW.size:
        raise ProtocolError("RAW_SIZE")
    samples = []
    for i, row in enumerate(RAW.iter_unpack(blob)):
        if (row[:2] != (0x31444D45, 0x00600001) or row[2:4] != (first+i, first+i)
                or row[4] >> 20 != 12 or row[5] >> 20 != 12
                or row[-2] != INTERVAL_NS or row[-1] != (0x103 if continuous else 3)
                or any(not -8388608 <= x <= 8388607 for x in row[6:22])):
            raise ProtocolError("RAW_SEQUENCE_HEADER_SIGN_OR_SOURCE")
        samples.append(row[6:22])
    return samples


def validate_hello(value, mode):
    if (value.get("source_mode") != {LIVE: "LIVE_STREAM", SIMULATED: "SIMULATED"}[mode]
            or value.get("profile") != "CAP1_CONT1" or value.get("channels") != list(CHANNELS)
            or value.get("interval_ns") != INTERVAL_NS or value.get("chunk_frames") != CHUNK
            or value.get("time_basis") != "DESIGN_INTERVAL_NOT_MEASURED_ADC_TIME"
            or value.get("automatic_rearm") is not False):
        raise ProtocolError("HELLO_CONTRACT")


def validate_live_observation(value):
    expected = [0x3030c096, 0x30303030, 0x00ff3030]
    extras = value.get("external_config", [])
    if (value.get("configuration_observed") is not True or not value.get("valid", 0) & 32
            or value.get("adc1_words") != expected or value.get("adc2_words") != expected
            or len(extras) != 5 or extras[0] & 0xfeffffff != 0xec002000
            or extras[1] & 0xfeffffff != 0xe8002000 or extras[2:] != [0x7fff, 0x7fff, 0x434e5431]):
        raise ProtocolError("LIVE_ADC_OBSERVATION_MISSING_OR_MISMATCH")
