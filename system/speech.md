# Speech Runtime

## Current board delta: 2026-10-24

Chinese now uses the operator-approved offline Chinese Piper voice with sherpa-onnx 1.13.8; English retains the original eSpeak path and defaults. The installed production CLI and actual USB playback passed, the operator confirmed both languages, and the English reference WAV is byte-identical before/after. The resident provider does not play sound or touch ADC/DRM. It is enabled and survived an actual service restart; this new service has not yet been tested through an additional full board reboot.

The source files are `eeg_tts_server.cpp`, `eeg-chinese-tts.service`, `eeg_speech.py` and `speech_route_recovery.py`. The matching C header, exact model/runtime archives, notices, complete deployment bundle, plan/apply evidence, measured latency and recovery instructions are indexed by `docs/chinese_speech_delivery_20261024.md`. Chinese requires the additive model/runtime deployment; copying this wrapper alone is insufficient. Missing/unready Chinese service returns an explicit error instead of silently using old eSpeak Mandarin. The project use is explicitly non-commercial; retain the model card's dataset restriction and upstream runtime notices.

Measured model initialization is about 17 seconds; warm Chinese examples took roughly 4–13 seconds depending on text. English reference synthesis remains about 0.4 seconds. Startup performs no automatic playback. The existing `--wav`, `--play`, PCM selection, speed/amplitude bounds and no-overwrite behavior are retained. These are real speech tests, not evidence of successful EEG letter selection.

## Original overlay 11 (historical provenance)

The task-scoped AArch64 overlay is produced as `speech-runtime-overlay_11.tar.gz` under the matching build's `system/speech_runtime_01` directory. Its SHA256 and per-file inventory are in `speech_runtime_manifest_11.json`. `speech-source-pack_11.tar.gz` contains the exact retained Yocto sources, two Yocto patches, wrapper and build/verifier scripts.

The overlay provides ALSA `aplay`/`arecord`/`amixer`, eSpeak 1.48.04 and its English/Mandarin data, plus `eeg-speak` and `eeg-audio`. It is built against the existing persistent-root ALSA, glibc and Python runtime; the overlay must be merged with that exact rootfs. eSpeak generates finite WAV data and ALSA playback is a separate explicit action.

After installing the matched system, list detected cards and PCM names with `eeg-audio list`. Select one listed output PCM, for example `eeg-audio select plughw:CARD=USB,DEV=0`, then inspect the saved selection with `eeg-audio status`. Playback is explicit: `eeg-speak --lang zh --play '脑电示波器已启动'` or `eeg-speak --lang en --play 'Recording started'`. Use `--device PCM` for a one-time override. To synthesize without playing, use `eeg-speak --lang zh --wav /opt/eeg/data/message.wav '你好'`.

Text is limited to 4096 characters per invocation. Language, voice speed and amplitude have bounded arguments; WAV output refuses to overwrite an existing file and is created atomically. No card or PCM is selected automatically, and startup does not speak or play anything.

Build-time QEMU verified ALSA tool help, target Python wrapper help/status, and finite non-silent 22.05 kHz mono WAVs in both languages. The wrapper's QEMU English and Mandarin WAVs are hashed in the producer manifest, with a spy confirming synthesis without `--play` never invokes `aplay`. The physical USB sound card was not enumerated or played on the board; those remain `NOT_EXERCISED` until the merged SD rootfs boots with the speaker attached.
