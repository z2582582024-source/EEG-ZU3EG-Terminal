"""Deterministic CPU golden vectors for cross-language/RTL comparison."""

from __future__ import annotations

import hashlib
import json

from .ads1299 import build_ads1299_frame, parse_ads1299_frame
from .frame import EegFrameV1, ErrorFlag, SourceType

_ADS_CASES = (
    {
        "name": "all_zero",
        "status": 0xC00000,
        "samples": (0, 0, 0, 0, 0, 0, 0, 0),
        "raw_hex": "c00000000000000000000000000000000000000000000000000000",
    },
    {
        "name": "signed_boundaries_and_status_fields",
        "status": 0xC5A3F7,
        "samples": (0, 1, -1, 8_388_607, -8_388_608, 0x123456, -0x123456, 42),
        "raw_hex": "c5a3f7000000000001ffffff7fffff800000123456edcbaa00002a",
    },
)


def _vector_document() -> dict[str, object]:
    vectors: list[dict[str, object]] = []
    for case in _ADS_CASES:
        encoded = build_ads1299_frame(case["samples"], status=case["status"])
        if encoded.hex() != case["raw_hex"]:
            raise AssertionError(f"golden literal mismatch for {case['name']}")
        parsed = parse_ads1299_frame(encoded)
        vectors.append(
            {
                "name": case["name"],
                "status_hex": f"0x{parsed.status:06X}",
                "loff_statp_hex": f"0x{parsed.loff_statp:02X}",
                "loff_statn_hex": f"0x{parsed.loff_statn:02X}",
                "gpio_hex": f"0x{parsed.gpio:X}",
                "samples": list(parsed.channels),
                "raw_hex": encoded.hex(),
            }
        )
    return {
        "schema": "ads1299-golden-v1",
        "classification": "CPU/Python golden reference; not hardware capture",
        "wire_order": "MSB-first",
        "vectors": vectors,
    }


def golden_artifacts() -> dict[str, bytes]:
    vector_bytes = (
        json.dumps(_vector_document(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")

    samples = _ADS_CASES[1]["samples"] + tuple(reversed(_ADS_CASES[1]["samples"]))
    record = EegFrameV1(
        sequence=7,
        timestamp_ns=28_000_000,
        sample_rate_millihz=250_000,
        source=SourceType.SIMULATED,
        adc_present_mask=0x03,
        flags=ErrorFlag.NONE,
        adc1_status=0xC5A3F7,
        adc2_status=0xC00000,
        samples=samples,
    ).pack()
    primary = {
        "ads1299_vectors_v1.json": vector_bytes,
        "eeg_frame_v1.bin": record,
    }
    manifest = {
        "schema": "eeg-golden-manifest-v1",
        "classification": "software-generated golden data; not hardware capture",
        "files": {
            name: {"bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}
            for name, content in sorted(primary.items())
        },
    }
    manifest_bytes = (
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    return {**primary, "manifest_v1.json": manifest_bytes}

