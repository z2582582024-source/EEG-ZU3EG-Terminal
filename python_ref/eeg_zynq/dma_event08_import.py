"""Lossless offline conversion of the closed event08 DMA evidence to Qt replay.

Independent of the old 1025-frame snapshot/consume importer. Imports never
access a target. Source and output files are never overwritten.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
from pathlib import Path
from typing import Sequence

from .emd1 import (DESIGN_RATE_HZ, ENCODED_RATE_MILLIHZ, FRAME_COUNT,
                   INTERVAL_NS, Emd1Record, parse_finite_emd1)
from .frame import EegFrameV1, SourceType

IMPORT_SCHEMA = "eeg-real-finite-dma-file-import-v1"
EVENT_ID = "axis_dma_ddr_finite_internal_test_20261017_08"
DEFAULT_OUTPUT = "data/replay/real_dma_event08_v1/event08_dma.eegbin"
SOURCE_DIR = "build/axis_dma_ddr_pl_deadline_v1/board_test_20261017_08/dma_export"
_SOURCES = {
    "raw_dma.bin": (24576, "33d9f9bd7b3def775588a1724b52cb2b0d2c57f7405e358c151ef99e419b148b"),
    "raw_dma_frames.csv": (30929, "914f532e12313cd2030dc6f7359a196670ae25ca2cb1694a11602b1675df6728"),
    "diagnostics.bin": (784, "e59cac697a00b196389dd5640821f1fc0581ff89a18e1b759506acfead51b110"),
    "source_manifest.json": (1653, "95823e67c8553735cdee8a597969d8c8028314f2167615c1394357bb766defc8"),
}
REPORT = "docs/axis_dma_ddr_finite_internal_test_20261017_08_result.md"
REPORT_SHA = "cdf6facbcf14db71092dd175926a540c9a54d423ed2f626e1aba01cdf5cab142"
CSV_HEADER = ("sequence", "batch_index", "relative_ns", "ads1_status_hex",
              "ads2_status_hex", *(f"ads{a}_ch{c}" for a in (1, 2) for c in range(1, 9)))


def json_bytes(document: object) -> bytes:
    return (json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True,
                       allow_nan=False) + "\n").encode("utf-8")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def csv_bytes(records: Sequence[Emd1Record]) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\r\n")
    writer.writerow(CSV_HEADER)
    for r in records:
        writer.writerow((r.sequence, r.batch_index, r.batch_index * INTERVAL_NS,
                         f"{r.status1:06X}", f"{r.status2:06X}", *r.samples))
    return stream.getvalue().encode("utf-8")


def build_metadata(frames: Sequence[EegFrameV1], mapping: dict) -> dict:
    payload = b"".join(f.pack() for f in frames)
    return {
        "schema": IMPORT_SCHEMA, "event_id": EVENT_ID,
        "converter": "eeg_zynq.dma_event08_import-v1",
        "classification": "real finite DMA board internal-test file replay; not human EEG",
        "display_labels": ["FILE_REPLAY", "真实DMA板测内部测试信号", "非实时、非真人脑电"],
        "current_replay_source": "FILE_REPLAY", "realtime": False,
        "historical_transport": "PL_AXIS_DMA_S2MM_HP0_THEN_DEBUGGER_FILE_EXPORT",
        "new_acquisition": False, "record_count": FRAME_COUNT,
        "frame_format": {"name": "EegFrameV1", "version": 1, "record_bytes": 112},
        "sample_rate_millihz": ENCODED_RATE_MILLIHZ,
        "timebase": {
            "design_sample_rate_sps": DESIGN_RATE_HZ, "rational_sps": [15625, 64],
            "sample_interval_ns": INTERVAL_NS,
            "basis": "DESIGN_DERIVED_NO_INDEPENDENT_PER_FRAME_MEASUREMENTS",
            "first_last_span_ns": (FRAME_COUNT - 1) * INTERVAL_NS,
            "sample_count_duration_ns": FRAME_COUNT * INTERVAL_NS,
            "encoded_rate_sps": 244.141, "encoded_error_ppm": 1.536,
        },
        "channel_order": "ADS1_CH1..8,ADS2_CH1..8",
        "electrode_mapping": mapping,
        "sources": [{"path": f"{SOURCE_DIR}/{name}", "bytes": size, "sha256": sha}
                    for name, (size, sha) in _SOURCES.items()],
        "result_report": {"path": REPORT, "bytes": 14079, "sha256": REPORT_SHA},
        "integrity": {"algorithm": "SHA-256", "data_sha256": _sha(payload)},
        "finite_end": "STOP; restart only by explicit user action",
        "limitations": ["not Linux or Qt realtime acquisition", "not continuous capture",
                        "not external input or human safety evidence",
                        "no independent per-beat TLAST/TKEEP or per-frame timestamp measurement"],
    }


def validate_import_metadata(document: dict, frames: Sequence[EegFrameV1],
                             data_sha256: str, mapping: dict) -> None:
    if len(frames) != FRAME_COUNT:
        raise ValueError("event08 needs exactly 256 frames")
    records = tuple(Emd1Record(f.sequence, i, f.adc1_status, f.adc2_status, f.samples)
                    for i, f in enumerate(frames))
    raw = b"".join(r.pack() for r in records)
    parse_finite_emd1(raw)
    for f, r in zip(frames, records):
        if f != r.to_frame(SourceType.FILE_REPLAY):
            raise ValueError("event08 replay frame metadata differs from EMD1 contract")
    if _sha(raw) != _SOURCES["raw_dma.bin"][1]:
        raise ValueError("reconstructed event08 raw DMA identity mismatch")
    if _sha(csv_bytes(records)) != _SOURCES["raw_dma_frames.csv"][1]:
        raise ValueError("reconstructed event08 CSV identity mismatch")
    expected = build_metadata(frames, mapping)
    if json_bytes(document) != json_bytes(expected) or data_sha256 != expected["integrity"]["data_sha256"]:
        raise ValueError("event08 provenance/timebase/output identity mismatch")


def import_event08(root: Path, output: Path | None = None) -> Path:
    from .recording import _electrode_mapping_document, metadata_path_for

    root = root.resolve()
    inputs = {}
    for name, (size, sha) in _SOURCES.items():
        data = (root / SOURCE_DIR / name).read_bytes()
        if len(data) != size or _sha(data) != sha:
            raise ValueError(f"event08 source identity mismatch: {name}")
        inputs[name] = data
    report = (root / REPORT).read_bytes()
    if len(report) != 14079 or _sha(report) != REPORT_SHA:
        raise ValueError("event08 report identity mismatch")
    source = json.loads(inputs["source_manifest.json"])
    if (source["event_id"] != EVENT_ID or source["contract"] != "EMD1_V1_96_LE"
            or source["realtime"] is not False or not all(source["checks"].values())):
        raise ValueError("event08 source manifest contract mismatch")
    records = parse_finite_emd1(inputs["raw_dma.bin"])
    if csv_bytes(records) != inputs["raw_dma_frames.csv"]:
        raise ValueError("event08 raw DMA and CSV differ")
    frames = tuple(r.to_frame(SourceType.FILE_REPLAY) for r in records)
    payload = b"".join(f.pack() for f in frames)
    metadata = build_metadata(frames, _electrode_mapping_document())
    validate_import_metadata(metadata, frames, _sha(payload), _electrode_mapping_document())
    path = (output or root / DEFAULT_OUTPUT).resolve()
    sidecar = metadata_path_for(path)
    if path.exists() or sidecar.exists():
        if path.read_bytes() != payload or sidecar.read_bytes() != json_bytes(metadata):
            raise FileExistsError("existing replay output differs; refusing overwrite")
        return path
    # New directory is exclusively claimed; partial outputs are retained on I/O failure.
    path.parent.mkdir(parents=True, exist_ok=False)
    with path.open("xb") as stream:
        stream.write(payload)
    with sidecar.open("xb") as stream:
        stream.write(json_bytes(metadata))
    return path
