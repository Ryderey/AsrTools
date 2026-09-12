# Full Windows package size audit — 2026-09-12

## Decision

Keep the current offline recognition, character alignment, online engine, GUI,
media support, and self-contained portable delivery. Start with narrowly scoped
data exclusions; investigate the FunASR import boundary for larger reductions.
Do not remove model weights or native inference libraries just because they are large.

This is an audit only. No application source, dependencies, build configuration,
models, existing artifacts, or Trellis task state were changed. No application,
inference, compilation, dependency installation, or download was run. This document
is the only added file. All proposed savings remain conditional on future validation.

## 1. Baseline and evidence boundaries

The only portable ZIP found under the current `dist/` tree is:

```text
dist/ASRTools-Windows-x64-v1.2.0/20260910-225952-617/
  ASRTools-Windows-x64-v1.2.0-portable.zip
```

| Measurement | Value |
|---|---:|
| ZIP file size | 737,541,352 bytes / **703.37 MiB** |
| Sum of uncompressed member sizes | 1,654,842,428 bytes / **1,578.18 MiB** |
| Sum of compressed member payloads | 735,590,676 bytes |
| ZIP headers and other archive overhead | 1,950,676 bytes / 1.86 MiB |
| Member count | 10,191 |
| Separate corresponding-source ZIP | 417,661 bytes |

Portable ZIP SHA-256, calculated during this audit:

```text
0b54e5516bd92bc4208deb75f9fdfc2fb44094e5a474f0149beaabe734dd6243
```

Read-only verification passed: every ZIP member passed the CRC check, and all
nine packaged model/config SHA-256 values matched `offline-models.json`. These
checks establish archive/input integrity, not functional runtime correctness.

MiB means 1,048,576 bytes. Uncompressed size is the sum of logical file lengths,
not filesystem allocation, memory consumption, or build workspace usage.
Compressed component values below exclude ZIP headers. They are measured current
contributions, not predictions of a rebuilt executable or a different compressor.

The corresponding-source ZIP contains byte-identical copies of the current
`scripts/build_release.ps1`, `scripts/release_payload.py`,
`bk_asr/offline_alignment.py`, and `requirements-release.lock`. Thus the principal
packaging and alignment evidence matches this artifact's accompanying source.
This does not independently prove the compiled executable's complete provenance.

The current tree no longer contains this release's compilation report or link map.
The historical [build status](windows-build-status.md) records compilation of
5,015 C files, followed by a successful ZIP collection and a manifest/checksum
finalization failure. The delivery directory still lacks `BUILD-MANIFEST.txt` and
`SHA256SUMS.txt`. Its `VALIDATION-REPORT.md` explicitly leaves runtime validation to
the user. This audit does not upgrade those release or functional acceptance states.

These measurements cover Windows x64 v1.2.0 only. There is no measured current
Linux/macOS full package here from which to infer equivalent savings.

## 2. Where the bytes are

Rows are disjoint; omitted components account for the remainder.

| ZIP member/group | Uncompressed MiB | Compressed MiB | Share of ZIP bytes |
|---|---:|---:|---:|
| `_runtime/models/` — nine pinned inputs | 388.20 | 295.72 | 42.04% |
| `_runtime/ASRTools-runtime.exe` | 439.68 | 153.10 | 21.77% |
| `_runtime/torch/` — CPU libraries and headers | 290.35 | 82.36 | 11.71% |
| `_runtime/ffmpeg.exe` | 97.18 | 35.42 | 5.04% |
| `_runtime/llvmlite/` | 84.52 | 28.80 | 4.09% |
| `_runtime/scipy/` | 63.36 | 23.51 | 3.34% |
| `_runtime/jieba/` | 29.58 | 17.25 | 2.45% |
| `_runtime/numpy.libs/` | 19.99 | 6.37 | 0.91% |
| `_runtime/scipy.libs/` | 19.22 | 6.30 | 0.90% |
| `_runtime/onnxruntime.dll` | 16.57 | 5.92 | 0.84% |
| `_runtime/sklearn/` | 9.64 | 3.45 | 0.49% |

Additional observations:

- Qt, counting `_runtime/PyQt5/` and root `qt5*.dll` together, occupies
  46.44 MiB uncompressed / 17.89 MiB compressed. GUI replacement is not the first target.
- `torch/lib/torch_cpu.dll` alone is 251,258,368 bytes / 72,668,464 compressed bytes.
  The lock already selects `torch==2.6.0+cpu` and `torchaudio==2.6.0+cpu`.
  No CUDA/cuDNN/cuBLAS runtime DLLs were found. A small Numba CUDA extension exists;
  its presence does not mean this is a CUDA PyTorch distribution.
- The EXE's PE `.text` section is 394,629,632 bytes on disk; `.rdata` is
  62,682,624 bytes. Most EXE size is code, rather than a hidden duplicate model archive.
  The available section table cannot assign these bytes to individual Python packages.
- SenseVoice's already-INT8 ONNX is 239,233,841 bytes, and `fa-zh/model.pt` is
  158,469,618 bytes. ZIP compresses the latter to 147,135,574 bytes: ordinary
  archive tuning cannot be assumed to shrink it dramatically.
- There are no packaged `test/` or `tests/` member directories, and no members
  matching PDB files, `test.mp3`, `test_wavs`, `ffprobe`, or `ffplay`.
  Local virtual environments, wheel caches and historical builds are not the
  measured portable package. Cleaning them would reclaim developer disk space,
  not reduce this customer's ZIP.

## 3. Prioritized opportunities

### P1 — Exclude PyTorch development headers from delivery

**Measured candidate:** `_runtime/torch/include/`: 8,686 files,
28,567,050 bytes / **27.24 MiB** uncompressed,
6,344,017 bytes / **6.05 MiB** compressed.

The installed Nuitka 4.1.3 package configuration explicitly collects Torch's
`bin` and `include` directories. This accounts for the headers without requiring
an application feature that consumes them. The production adapter uses
`torch.inference_mode()` and prebuilt CPU operators; repository searches found no
`torch.compile` or C++ extension compilation calls. The build already specifies
`--module-parameter=torch-disable-jit=yes`.

**Proposal:** exclude precisely `torch/include/**` from the staged deliverable,
preferably through an explicit build exclusion. Preserve the build environment
and `torch/lib` unchanged. Removing 85% of archive members also reduces file-count
overhead, although extraction-time improvement was not measured.

**Acceptance:** prove that cold model initialization and real packaged inference
never request these headers, including repeated files and a clean machine without
a compiler. Torch JIT being disabled is supporting evidence, not proof that every
possible runtime code-generation mechanism is inactive.

### P1 — Exclude jieba's unused Paddle model data

**Measured candidate:** `_runtime/jieba/lac_small/`: 22 files,
12,971,081 bytes / **12.37 MiB** uncompressed,
12,020,103 bytes / **11.46 MiB** compressed.

`scripts/build_release.ps1:240` requests all jieba package data. In the installed
jieba source, `lac_small` is reached through its Paddle mode. No application
Paddle-mode call was found. The selected fa-zh `CharTokenizer` reads its own
`models/fa-zh/seg_dict`; it does not use jieba's Paddle model.

**Proposal:** omit only `jieba/lac_small/**` in the first pass. Retain jieba's
ordinary dictionary, finalseg, posseg and analysis data until their import/data
dependencies are separately resolved. FunASR's optional punctuation implementation
imports jieba, so deleting the entire package is a different, higher-risk change.

**Acceptance:** cold FunASR import and the complete selected model registration
must succeed; offline text/timing and explicit online mode must match baseline.

**Combined P1 candidate:** 8,708 files, **39.61 MiB uncompressed / 17.51 MiB
compressed payload**, about **2.49% of the current ZIP**, plus removed ZIP headers.
This is a bounded, identifiable candidate set. It is not an implemented or
functionally verified reduction, and it will not halve the package.

### P2 — Narrow FunASR's production import boundary

**Evidence:** `scripts/build_release.ps1:236` includes all of `funasr`.
The installed `funasr/__init__.py` recursively walks and imports its submodules.
`bk_asr/offline_alignment.py:65` enters through `from funasr import AutoModel`.
This reaches far more than the selected alignment model, including datasets,
training helpers, speaker clustering, alternate models and export utilities.

Current selected registrations include `MonotonicAligner`, `SANMEncoder`,
`CifPredictorV3`, `WavFrontend`, `CharTokenizer`, and `SpecAugLFR`, as established
by `models/fa-zh/config.yaml`. Even augmentation can be instantiated from this
configuration before evaluation mode; names that sound training-only are not
automatically removable.

The concrete dependency chain preventing blind deletion is:

```text
application load_aligner / character_intervals
  -> FunASR AutoModel and extract_fbank
  -> funasr.utils.load_utils -> librosa
  -> WavFrontend module -> eend_ola_feature -> librosa
  -> librosa filters/utilities -> numba -> llvmlite
                              -> scipy

AutoModel / recursive package discovery
  -> campplus clustering -> sklearn + scipy
```

The selected `WavFrontend` uses `torchaudio.compliance.kaldi.fbank`, while the
same source module imports feature code for another frontend. This makes a
narrow production boundary worth investigating without replacing the actual
feature extraction or timestamp algorithm.

**Proposal:** determine the transitive closure of the pinned selected model and
its registration/helpers, then restrict unnecessary eager imports and packaging.
Preserve the exact weights, frontend math, encoder/predictor calls, token mapping,
and timestamp helper. Merely changing to `from funasr.some_module import ...`
still initializes the parent package; merely removing `--include-package=funasr`
can break dynamic registration. A controlled adapter or pinned dependency patch
must address both initialization and the build inclusion rules.

**Measured opportunity pool, not a savings estimate:** llvmlite + scipy +
scipy.libs + sklearn currently total **176.73 MiB uncompressed / 62.05 MiB
compressed**. Some or all may remain required after the closure is established.
Do not add this entire pool to the P1 savings forecast. Reducing compiled modules
could also shrink the 439.68 MiB EXE, but no trustworthy module-to-byte attribution
or numeric forecast is available without a fresh compilation report and rebuild.

**Risk and acceptance:** high integration risk relative to P1. Numba JIT is
explicitly enabled by the current build. Deleting LLVM or disabling Numba in
isolation can break decorators/imports or change performance. Keep current CPU
runtime versions during the first experiment, compare features and character
intervals against baseline, and run the full packaged acceptance matrix below.

### P2 — Investigate the two confirmed duplicate DLL pairs

SHA-256 comparison confirms byte-identical copies:

| Paths, relative to `_runtime/` | One redundant copy, bytes | Compressed bytes |
|---|---:|---:|
| `asmjit.dll` and `torch/lib/asmjit.dll` | 358,912 | 180,824 |
| `fbgemm.dll` and `torch/lib/fbgemm.dll` | 4,961,280 | 1,021,627 |

Total candidate: **5.07 MiB uncompressed / 1.15 MiB compressed**.

**Proposal:** inspect native import tables and actual DLL load paths before
consolidating. Torch's explicit library-directory loading and dependent DLL
resolution may require a particular location. Hash equality alone does not
authorize deleting either location; preserve standalone operation without PATH.
Portable ZIP extraction cannot be assumed to preserve hard links.

The NumPy and SciPy OpenBLAS DLLs have different names and sizes, including a
`64_` variant for NumPy. They are not established duplicates and must not be
merged on the basis of their similar names. Likewise retain differently versioned
OpenSSL libraries until each consumer and ABI requirement is mapped.

### P3 — Archive settings, metadata and Qt pruning

| Candidate | Finding | Recommendation |
|---|---|---|
| ZIP compression | `release_payload.py:63` uses DEFLATE without an explicit level | Benchmark stronger DEFLATE first, in a separate future artifact. Compare size and packaging time. No recompression experiment was run here; no percentage gain is claimed. Uncompressed footprint would remain unchanged. |
| Distribution metadata | All `.dist-info` members total 4.81 MiB raw / 1.79 MiB compressed; build script copies every environment `.dist-info` tree | Low priority. Preserve versions, entry points, resources and notices used at runtime. The app explicitly queries FunASR/Torch/sherpa-onnx versions; keeping only those three is not yet proven sufficient. |
| FunASR source copy | 2.71 MiB raw / 0.59 MiB compressed, despite a compiled copy | Low return. Both packaged registry patch and copied source exist. Prior inspect/registration failures make blind removal inappropriate; re-evaluate with the narrowed import boundary. |
| Qt modules/plugins | QML/Quick and multimedia DLLs are present alongside QWidget code | Map transitive qfluentwidgets imports and plugin/native dependencies first. Its public initializer imports all components. Do not remove DLLs solely because the GUI has no direct import. Preserve native dialogs, images, theme and high-DPI behavior. |

Changing archive format or delivering a self-extracting EXE is not the first
recommendation: the existing delivery contract specifies a portable ZIP and has
prior onefile quarantine history. Any future format change needs its own supported
extraction and distribution decision.

## 4. Preserve these capabilities and inputs

- **Keep all nine pinned model/config files.** They are already whitelisted by
  `release-assets/offline-models.json`; sample WAVs and conversion tools are absent.
  External model downloads would change first-use offline capability and transfer
  bytes elsewhere, not remove the full installation requirement.
- **Keep the existing alignment algorithm and CPU PyTorch path.** Removing
  fa-zh/Torch, replacing acoustic alignment with interpolation, quantizing weights,
  or exporting a new ONNX aligner is not established capability-preserving trimming.
  Such changes require separate accuracy, operator and performance evidence.
- **Keep FFmpeg's supported input/output coverage.** The GUI accepts MP3, WAV,
  OGG, FLAC, AAC, M4A, WMA, MP4, AVI, MOV, TS, MKV, WMV, FLV, WebM and RMVB via
  its drag/drop filter. Offline conversion requires 16 kHz mono PCM; online
  conversion uses MP3. Container extensions alone do not describe every codec.
  A custom smaller FFmpeg build is a separate investigation with codec/protocol
  coverage and maintenance cost, not a safe deletion in this audit.
- **Keep licenses, source-delivery material and checksums.** `docs/` is only
  1.49 MiB raw / 0.43 MiB compressed. It is not a meaningful reduction target.
- **Keep GUI, online selection, batch controls, cancellation, exports and native
  import order.** Scope is current ASRTools product behavior, not every training
  or model-export API offered by its upstream dependencies.

## 5. Future implementation and acceptance order

No items in this section were executed in this audit.

1. Preserve the baseline ZIP by its hash. Restore complete build provenance and
   retain the new Nuitka report in any future candidate build. Record ZIP size,
   uncompressed size, member count and per-group compressed bytes for comparison.
2. Implement P1 exclusions independently, retaining model/DLL bytes. Validate each
   candidate before combining them. Keep the original artifact and build inputs.
3. If the remaining size is unacceptable, investigate the P2 FunASR boundary in
   isolation. Record inclusion reasons and the selected registration closure;
   avoid changing runtime versions and architecture in the same experiment.
4. Consider duplicate-DLL consolidation and smaller P3 opportunities only when
   the expected savings justify their verification cost.

Required evidence for a future capability-preserving claim:

| Area | Acceptance evidence |
|---|---|
| Artifact integrity | Correct portable root layout; new SHA-256; all nine model hashes unchanged; preserved notices and source package; explicitly allowed inventory differences only |
| Clean startup | Windows 10+ x64 without Python/compiler/system FFmpeg; Qt starts; bundled native libraries resolve; no external model retrieval |
| Offline output | Same representative corpus, recognized text, token/display-index mapping and valid character intervals; compare baseline precision, silence, mixed text, unknown-character failures and segment-boundary behavior |
| Batch stability | Existing real GUI video batch checks, including injected failure; subsequent valid files succeed; one resident model/thread; cancellation and shutdown remain correct |
| Online and exports | Explicit Bcut workflow, existing retry/rate-limit behavior, SRT/TXT/ASS and Chinese paths; no unintended fallback from offline |
| Media conversion | Supported audio/container/codec corpus, stereo and different sample rates; bundled FFmpeg only; PCM and MP3 outputs |
| Performance | Cold start, first model load, sustained CPU transcription speed, peak RAM and extraction time on the same machine; no unaccepted regression hidden by a size reduction |
| Distribution | Existing exact-artifact clean-machine/antivirus acceptance, including launcher argument and exit-code propagation |

Existing relevant checks include `tests/test_offline_engine.py`,
`tests/test_offline_gui.py`, the prior Bcut suite,
`scripts/check_gui_batch.py --videos`, and its `--inject-failure` variant.
Unit/mocked checks alone cannot establish native packaged equivalence. Runtime
verification remains user-owned under the current release contract.

## 6. Reproduce the size inventory without changing the package

Run from the repository root with Python's standard library:

```python
from collections import defaultdict
from pathlib import Path
from zipfile import ZipFile

archive = Path("dist/ASRTools-Windows-x64-v1.2.0/20260910-225952-617/"
               "ASRTools-Windows-x64-v1.2.0-portable.zip")
groups = defaultdict(lambda: [0, 0, 0])
with ZipFile(archive) as source:
    for member in source.infolist():
        parts = member.filename.split("/")
        key = "/".join(parts[:2]) if parts[0] == "_runtime" else parts[0]
        values = groups[key]
        values[0] += member.file_size
        values[1] += member.compress_size
        values[2] += 1
print("ZIP bytes:", archive.stat().st_size)
for group, values in sorted(groups.items(), key=lambda item: item[1][0], reverse=True):
    print(group, "raw bytes / compressed bytes / count:", *values)
```

Primary evidence was local: the ZIP central directory and selected member contents,
the EXE PE section table, current matching build sources, installed pinned upstream
sources, and the project's release/offline contracts. No internet size estimate,
dependency-wheel size or developer-cache size was substituted for shipped bytes.
