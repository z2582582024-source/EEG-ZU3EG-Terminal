"""Deterministic milestone-2 metadata and CPU/DSP golden artifacts."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict

from .dsp import DspConfig, filter_coefficients, filter_samples, one_sided_spectrum
from .electrodes import electrode_mapping_document


def _rounded(value: float) -> float:
    return round(value, 12)


def _dsp_vector_document() -> dict[str, object]:
    config = DspConfig()
    composite_input = tuple(
        int(
            round(
                100_000.0
                + 20_000.0 * math.sin(2.0 * math.pi * 0.25 * index / config.sample_rate_hz)
                + 10_000.0 * math.sin(2.0 * math.pi * 10.0 * index / config.sample_rate_hz)
                + 5_000.0 * math.sin(2.0 * math.pi * 50.0 * index / config.sample_rate_hz)
            )
        )
        for index in range(config.fft_size)
    )
    filtered = filter_samples(composite_input, config)

    fft_bin = 16
    fft_amplitude = 10_000.0
    fft_input = tuple(
        fft_amplitude * math.sin(2.0 * math.pi * fft_bin * index / config.fft_size)
        for index in range(config.fft_size)
    )
    spectrum = one_sided_spectrum(fft_input, config)
    return {
        "schema": "eeg-dsp-golden-v1",
        "classification": "CPU/Python golden reference; not hardware capture or validation",
        "config": config.assumptions_dict(),
        "cascade_order": ["highpass", "notch", "lowpass"],
        "coefficients": [
            {name: _rounded(value) for name, value in asdict(coefficients).items()}
            for coefficients in filter_coefficients(config)
        ],
        "filter_vector": {
            "input_counts": list(composite_input),
            "output_counts": [_rounded(value) for value in filtered],
        },
        "fft_vector": {
            "input": "10000-count peak sine at exact bin 16",
            "expected_peak_bin": fft_bin,
            "expected_peak_frequency_hz": spectrum.frequencies_hz[fft_bin],
            "frequencies_hz": [_rounded(value) for value in spectrum.frequencies_hz],
            "peak_amplitudes_counts": [_rounded(value) for value in spectrum.amplitudes],
        },
    }


def dsp_golden_artifacts() -> dict[str, bytes]:
    primary_documents = {
        "dsp_vectors_v1.json": _dsp_vector_document(),
        "electrode_mapping_v1.json": electrode_mapping_document(),
    }
    primary = {
        name: (json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
            "utf-8"
        )
        for name, document in primary_documents.items()
    }
    manifest = {
        "schema": "eeg-dsp-golden-manifest-v1",
        "classification": "software-generated golden data; not hardware capture or validation",
        "files": {
            name: {"bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}
            for name, content in sorted(primary.items())
        },
    }
    manifest_bytes = (
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    return {**primary, "manifest_dsp_v1.json": manifest_bytes}
