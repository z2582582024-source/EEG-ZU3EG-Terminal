"""Deterministic, read-only import of the frozen 1025-frame board-test CSV.

This is an offline format converter, not a target-access or acquisition tool.
The diagnostic bin is a hashed source anchor and is never read as EegFrameV1.
"""

from __future__ import annotations

import csv
import copy
import hashlib
import io
import json
import re
from pathlib import Path
from typing import Sequence

from .frame import EEG_FRAME_V1_BYTES, EegFrameV1, ErrorFlag, SourceType

IMPORT_SCHEMA = "eeg-board-internal-test-import-v1"
EVENT_ID = "internal_test_window_20261015_01"
FRAME_COUNT = 1025
DESIGN_SAMPLE_RATE_HZ = 244.140625
SAMPLE_INTERVAL_NS = 4_096_000
ENCODED_RATE_MILLIHZ = 244_141
CSV_HEADER = (
    "sequence", "token", "ads1_status_hex", "ads2_status_hex", "consume_confirmed",
    *(f"ch{channel}" for channel in range(1, 17)),
)
RUN_DIRECTORY = "build/milestone4j4k_internal_test_window_v1/run_20261015_01"
INPUTS = {
    "paired_csv": {
        "path": f"{RUN_DIRECTORY}/raw_paired_frames.csv", "bytes": 118842,
        "sha256": "464449705129fa3a75ddde011ab27a4f51f11448005812edbde5430f0430b20e",
    },
    "diagnostic_bin": {
        "path": f"{RUN_DIRECTORY}/sample_results.bin", "bytes": 82456,
        "sha256": "5510fda6fc293ae757fd9ec8a9dda7267f29112043a83f787612c59b345427de",
    },
    "source_manifest": {
        "path": "build/milestone4j4k_internal_test_window_v1/offline_demo_1025_20261015_v1/source_manifest.json",
        "bytes": 10440,
        "sha256": "ea8e9f0ef5b3646226df1db3a7160987121cdde45b069f225bc38d080a98f750",
    },
    "result_report": {
        "path": "docs/milestone_4j_4k_internal_test_window_20261015_01_result_report.md",
        "bytes": 20288,
        "sha256": "08432da32c04e6db05c73abfae07954932a2ab8fe90dd4ad59ac1f1991e45ed7",
    },
}
DEFAULT_OUTPUT = "data/replay/internal_test_1025_v1/internal_test_1025.eegbin"


class ImportValidationError(ValueError):
    """Input identity, structure or imported provenance is not the frozen capture."""


def json_bytes(document: object) -> bytes:
    return (json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True,
                       allow_nan=False) + "\n").encode("utf-8")


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def parse_paired_csv(data: bytes) -> tuple[EegFrameV1, ...]:
    """Validate the frozen CSV contract before doing the input hash comparison."""
    try:
        text = data.decode("utf-8", errors="strict")
        rows = list(csv.reader(io.StringIO(text, newline=""), strict=True))
    except (UnicodeError, csv.Error) as exc:
        raise ImportValidationError(f"invalid paired CSV: {exc}") from exc
    if not rows or tuple(rows[0]) != CSV_HEADER:
        raise ImportValidationError("paired CSV header/order does not match the 16-channel contract")
    if len(rows) - 1 != FRAME_COUNT:
        raise ImportValidationError(f"paired CSV needs {FRAME_COUNT} frames, got {len(rows) - 1}")
    frames = []
    for index, row in enumerate(rows[1:]):
        if len(row) != len(CSV_HEADER):
            raise ImportValidationError(f"CSV record {index}: expected {len(CSV_HEADER)} columns")
        if row[0] != str(index):
            raise ImportValidationError(f"CSV record {index}: abnormal sequence {row[0]!r}")
        if row[1] != str(index + 1) or row[4] != "True":
            raise ImportValidationError(f"CSV record {index}: token/consume_confirmed mismatch")
        if any(re.fullmatch(r"[0-9A-F]{6}", value) is None for value in row[2:4]):
            raise ImportValidationError(f"CSV record {index}: invalid 24-bit status")
        status1, status2 = (int(value, 16) for value in row[2:4])
        if status1 >> 20 != 0xC or status2 >> 20 != 0xC:
            raise ImportValidationError(f"CSV record {index}: ADS1299 status header mismatch")
        if any(re.fullmatch(r"0|-?[1-9][0-9]*", value) is None for value in row[5:]):
            raise ImportValidationError(f"CSV record {index}: samples must be signed integer counts")
        samples = tuple(int(value) for value in row[5:])
        if any(not -(1 << 23) <= value < (1 << 23) for value in samples):
            raise ImportValidationError(f"CSV record {index}: sample outside signed ADS1299 int24")
        frames.append(EegFrameV1(
            sequence=index, timestamp_ns=index * SAMPLE_INTERVAL_NS,
            sample_rate_millihz=ENCODED_RATE_MILLIHZ, source=SourceType.FILE_REPLAY,
            adc_present_mask=3, flags=ErrorFlag.NONE,
            adc1_status=status1, adc2_status=status2, samples=samples,
        ))
    return tuple(frames)


def _reconstructed_csv(frames: Sequence[EegFrameV1]) -> bytes:
    """Reconstruct all original source fields, including the source's CRLF bytes."""
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\r\n")
    writer.writerow(CSV_HEADER)
    for frame in frames:
        writer.writerow((frame.sequence, frame.sequence + 1,
                         f"{frame.adc1_status:06X}", f"{frame.adc2_status:06X}",
                         "True", *frame.samples))
    return stream.getvalue().encode("utf-8")


def build_import_metadata(
    frames: Sequence[EegFrameV1], electrode_mapping: dict[str, object],
) -> dict[str, object]:
    payload = b"".join(frame.pack() for frame in frames)
    return {
        "schema": IMPORT_SCHEMA,
        "converter": "eeg_zynq.internal_test_import-v1",
        "classification": "real board internal-test file replay; non-real-time; not human EEG",
        "display_labels": ["FILE_REPLAY", "真实板测内部测试信号", "非实时、非真人脑电"],
        "event_id": EVENT_ID,
        "current_replay_source": "FILE_REPLAY",
        "historical_transport": "PS AXI-Lite snapshot/consume; not DMA or Linux acquisition",
        "new_acquisition": False,
        "frame_format": {"name": "EegFrameV1", "version": 1, "record_bytes": 112,
                         "byte_order": "little-endian project record"},
        "record_count": FRAME_COUNT,
        "sequence": {"first": 0, "last": 1024, "step": 1},
        "sample_rate_millihz": ENCODED_RATE_MILLIHZ,
        "timebase": {
            "basis": "design-derived; no independent per-frame measured timestamps",
            "design_sample_rate_sps": DESIGN_SAMPLE_RATE_HZ,
            "design_sample_rate_rational": {"numerator": 15625, "denominator": 64},
            "sample_interval_ns": SAMPLE_INTERVAL_NS,
            "timestamp_formula": "(sequence - first_sequence) * 4096000 ns",
            "timestamp_origin": "relative zero, not UTC or PS timer",
            "external_sample_timestamps": False,
            "encoded_rate_sps": 244.141,
            "encoded_rate_error_sps": 0.000375,
            "encoded_rate_error_ppm": 1.536,
            "first_last_span_seconds": 4.194304,
            "sample_count_playback_seconds": 4.1984,
            "playback_clock": "exact design rate; GUI refresh scheduling is not acquisition timing",
        },
        "sample_representation": "signed int24 raw counts sign-extended to int32; unchanged offsets/amplitudes",
        "processing": {"normalized": False, "smoothed": False, "interpolated": False,
                       "filled_frames": 0},
        "electrode_mapping": electrode_mapping,
        "inputs": copy.deepcopy(INPUTS),
        "source_records": [
            {"sequence": frame.sequence, "token": frame.sequence + 1, "consume_confirmed": True}
            for frame in frames
        ],
        "integrity": {"algorithm": "SHA-256", "scope": "entire eegbin file in byte order",
                      "data_bytes": len(payload), "data_sha256": _hash(payload),
                      "reconstructed_source_csv_sha256": _hash(_reconstructed_csv(frames))},
    }


def validate_import_metadata(
    document: dict[str, object], frames: Sequence[EegFrameV1],
    data_sha256: str, electrode_mapping: dict[str, object],
) -> None:
    if len(frames) != FRAME_COUNT:
        raise ImportValidationError(f"import metadata requires {FRAME_COUNT} frames, got {len(frames)}")
    for index, frame in enumerate(frames):
        if frame.sequence != index or frame.timestamp_ns != index * SAMPLE_INTERVAL_NS:
            raise ImportValidationError(f"import sequence/time mismatch at record {index}")
        if (frame.source is not SourceType.FILE_REPLAY or frame.adc_present_mask != 3
                or frame.flags != ErrorFlag.NONE
                or frame.sample_rate_millihz != ENCODED_RATE_MILLIHZ):
            raise ImportValidationError(f"import source/mask/flags/rate mismatch at record {index}")
    reconstructed = _reconstructed_csv(frames)
    if _hash(reconstructed) != INPUTS["paired_csv"]["sha256"]:
        raise ImportValidationError("replay samples/status/sequence do not match frozen source CSV SHA-256")
    expected = build_import_metadata(frames, electrode_mapping)
    if expected["integrity"]["data_sha256"] != data_sha256:
        raise ImportValidationError("import output SHA-256 mismatch")
    # Canonical JSON equality also rejects bool-as-int, extra fields and changed types.
    if json_bytes(document) != json_bytes(expected):
        raise ImportValidationError("import provenance metadata does not match the strict v1 frozen-source contract")


def verify_inputs(project_root: Path, csv_path: Path | None = None) -> tuple[EegFrameV1, ...]:
    blobs = {}
    for role, expected in INPUTS.items():
        path = csv_path if role == "paired_csv" and csv_path is not None else project_root / expected["path"]
        try:
            blobs[role] = path.read_bytes()
        except OSError as exc:
            raise ImportValidationError(f"cannot read {role} {path}: {exc}") from exc
    frames = parse_paired_csv(blobs["paired_csv"])
    for role, expected in INPUTS.items():
        if len(blobs[role]) != expected["bytes"] or _hash(blobs[role]) != expected["sha256"]:
            raise ImportValidationError(f"{role} frozen source identity mismatch (bytes/SHA-256)")
    manifest = json.loads(blobs["source_manifest"].decode("utf-8"))
    required = {
        "schema": "offline-internal-test-demo-source-manifest-v1",
        "event_id": EVENT_ID, "result_version": 2, "result_bytes": 82456,
        "frame_count": FRAME_COUNT, "frame_offset": 392, "frame_stride": 80,
        "design_sample_rate_sps": DESIGN_SAMPLE_RATE_HZ,
        "external_sample_timestamps": False, "replay_only_no_target": True,
    }
    for key, expected in required.items():
        if manifest.get(key) != expected:
            raise ImportValidationError(f"source_manifest {key} mismatch")
    for role in ("paired_csv", "diagnostic_bin", "result_report"):
        expected = INPUTS[role]
        matches = [item for item in manifest["inputs"]
                   if item["path"].replace("\\", "/").endswith("/" + expected["path"])]
        if (len(matches) != 1 or matches[0]["bytes"] != expected["bytes"]
                or matches[0]["sha256"].lower() != expected["sha256"]):
            raise ImportValidationError(f"source_manifest {role} identity mismatch")
    return frames


def import_capture(
    project_root: str | Path, output: str | Path | None = None, *, csv_path: str | Path | None = None,
):
    """Create a deterministic data/sidecar pair, or verify an identical existing pair.

    Existing files are never overwritten, including partial/failed conversions.
    """
    from .recording import _electrode_mapping_document, inspect_recording, metadata_path_for

    root = Path(project_root).resolve()
    path = Path(output).resolve() if output is not None else root / DEFAULT_OUTPUT
    frames = verify_inputs(root, Path(csv_path) if csv_path is not None else None)
    payload = b"".join(frame.pack() for frame in frames)
    metadata = build_import_metadata(frames, _electrode_mapping_document())
    validate_import_metadata(metadata, frames, _hash(payload), _electrode_mapping_document())
    sidecar = metadata_path_for(path)
    metadata_bytes = json_bytes(metadata)
    if path.exists() or sidecar.exists():
        if (not path.is_file() or not sidecar.is_file() or path.read_bytes() != payload
                or sidecar.read_bytes() != metadata_bytes):
            raise ImportValidationError(f"output already exists and differs; refusing overwrite: {path}")
        return inspect_recording(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(payload)
    with sidecar.open("xb") as stream:
        stream.write(metadata_bytes)
    return inspect_recording(path)
