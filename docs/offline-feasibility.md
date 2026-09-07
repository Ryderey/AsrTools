# SenseVoice CPU feasibility — initial checkpoint, 2026-09-06

Follow-up: [fa-zh CPU experiment and punctuation findings](offline-alignment-feasibility.md).
The report below records the earlier native SenseVoice investigation.

## Status

Branch `codex/offline-sensevoice` is based on `business` at
`54c2e76db0c51c44fd7c02bc4cfc0a3b90fde01d`; merge target is `business`.
Trellis task `.trellis/tasks/09-06-offline-sensevoice` remains `planning`.
PRD, design, implementation steps and research are local under the existing
Trellis ignore policy. No production engine, GUI, API, export or release lock
was changed. No extra alignment model was downloaded.

## CPU smoke test

Use [model inputs and checksums](offline-models.md) and
`scripts/probe_offline_asr.py`. Raw local report: `build/offline-probe/report.json`.
Dependencies were installed separately before inference. The probe ran in the
network-restricted execution sandbox with an additional Python socket guard and
local files only. Physical-disconnection and packaged network-capture acceptance
remain outstanding; the socket guard alone does not block native-library sockets.

- CPU: Intel Core Ultra 7 155H, verified from the Windows processor registry.
- RAM: 33,945,935,872 OS-reported bytes (32 GB installed); Windows 11.
- Python 3.12.13; sherpa-onnx / sherpa-onnx-core 1.13.7;
  NumPy 2.2.6; psutil 7.0.0. ASR CPU threads: 4; VAD: 1.
- Input: supplied `test_wavs/zh.wav`, mono PCM16, 16 kHz, 5.592 seconds.
- Audio SHA-256: `b77f1794fe374a0ba1ee1dc458bfaf9349496cbbfc32780c50ba3c5a7ad8e373`.
- ASR loads: 1.095 / 1.074 seconds for ITN off / on.
- Two decodes per loaded model: 0.140 / 0.136 / 0.149 / 0.143 seconds.
- Total timed probe: 3.110 seconds (hashes, two loads, four decodes and VAD;
  excludes interpreter/import startup and report writing).
- Process peak working set: 380,010,496 bytes (approximately 362.4 MiB).
- VAD processing: 0.025 seconds; speech span: 0.742–5.126 seconds.

These short-sample host measurements do not establish performance on a
9th-generation Intel CPU, long videos or an extra alignment model.

## Native token timing

ITN off: `开饭时间早上九点至下午五点` (13 tokens).
ITN on: `开饭时间早上9点至下午5点。` (14 tokens).

Both settings return these first 13 time points:
`0.72, 0.96, 1.26, 1.50, 1.92, 2.10, 2.58, 2.82, 3.30, 3.90, 4.26, 4.62, 4.80`.
ITN adds punctuation at 5.52 seconds. Both return `durations: []`.
Counts match, timestamps are monotonic and inside the audio, and repeated
decodes agree. These are structural checks, not character accuracy measurements.

The simple 九→9 and 五→5 replacements preserve positions here. Multi-character
numbers, dates, decimals, English subwords and punctuation placement remain
untested. Punctuation has no spoken pronunciation boundary.

The pinned [sherpa-onnx source](https://github.com/k2-fsa/sherpa-onnx/blob/v1.13.7/sherpa-onnx/csrc/offline-recognizer-sense-voice-impl.h)
converts CTC frame indices using frame shift and model subsampling. Such time
points do not establish precise character start/end boundaries. Using the next
token onset as an end, or uniformly dividing a sentence, does not meet this task.

## Separate alignment candidate — not selected

[FunASR fa-zh](https://huggingface.co/funasr/fa-zh) accepts an audio/text pair for
timestamp prediction; its card lists 38M parameters and Mandarin training. This
is a candidate for a separate forced-alignment experiment, not a sherpa-onnx
feature or a proven drop-in replacement.

The [official asset listing](https://huggingface.co/api/models/funasr/fa-zh/tree/main?recursive=true&expand=false)
reports `model.pt`: 158,469,618 bytes; `seg_dict`: 8,287,834 bytes; plus small
config/token/normalization files, approximately 167 MB before runtime dependencies.
This is a PyTorch artifact, not a verified INT8 ONNX package. CPU latency, peak
RAM, runtime download footprint and Windows packaging are unmeasured. Model size
is not resident memory, and the existing sherpa-onnx runtime cannot be assumed
to load this artifact.

The card uses a custom model license. The current
[FunASR agreement](https://github.com/modelscope/FunASR/blob/main/MODEL_LICENSE)
requires attribution and retention of model names and contains additional terms.
Archive the exact applicable license and asset revision and review redistribution
conditions before packaging; a source-code license is insufficient. This inventory
is not legal clearance for commercial distribution.

## Integration gate

1. Collect representative annotated Mandarin clips: numbers, English, pauses,
   music and long-video boundaries. Agree numeric error thresholds before acceptance.
2. Compare a separate aligner against native timing; verify text normalization,
   punctuation handling and transcript/audio mismatch behavior.
3. Measure CPU/RAM and full dependency footprint before selecting/downloading an
   additional model and updating the design. No automatic downloads are added.
4. After alignment feasibility converges, finalize chunking, resident-model
   ownership, cancellation, cache isolation and Windows distribution, then switch
   to `in_progress`. Preserve offline default and manual Bcut, without auto-upload.
5. Validate packaged offline operation, Chinese paths, silence, music, long video,
   cancellation, missing models, online compatibility and real minimum-spec hardware.

Character boundary errors and long-video accumulated drift are **unmeasured**.
Precise alignment has not passed acceptance; the planning gate remains open.
