# Offline ASR model inputs

Current checkpoint: all listed manually downloaded models and large runtime
archives are present and verified. The isolated alignment environment is ready;
see [CPU alignment results](offline-alignment-feasibility.md). Historical setup
notes below explain the download sequence; no further downloads are needed for
the current sample experiment.

The first feasibility candidate is SenseVoice Small INT8 (2024-07-17) with
Silero VAD, running through sherpa-onnx on CPU. The target is a 9th-generation
Intel CPU and 16 GB RAM; this is a validation target, not a measured guarantee.

## Downloads and layout

- [INT8-only SenseVoice archive](https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17.tar.bz2)
- [Silero VAD](https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/silero_vad.onnx)
- [Official model documentation](https://k2-fsa.github.io/sherpa/onnx/sense-voice/pretrained.html)

Extract the SenseVoice directory and rename it as below. Preserve its license,
README, and test WAVs. Do not rename the ONNX or token files.

```text
models/
  sensevoice-small-int8/
    model.int8.onnx
    tokens.txt
    LICENSE
    README.md
    test_wavs/zh.wav
  silero-vad/
    silero_vad.onnx
```

Models are ignored by Git and distributed separately. The application has not
yet been integrated with these files. No automatic downloads or online fallback
are allowed for the planned offline engine.

## Input checksums

These unpacked ASR hashes record the user-supplied local inputs; they are not
independently authenticated against the release archive. The VAD checksum was
matched to the official release metadata. Verify the archive hash too if the
original download is available.

| Input | Bytes | SHA-256 |
|---|---:|---|
| `sensevoice-small-int8/model.int8.onnx` | 239233841 | `c71f0ce00bec95b07744e116345e33d8cbbe08cef896382cf907bf4b51a2cd51` |
| `sensevoice-small-int8/tokens.txt` | 315894 | `f449eb28dc567533d7fa59be34e2abca8784f771850c78a47fb731a31429a1dc` |
| `silero-vad/silero_vad.onnx` | 643854 | `9e2449e1087496d8d4caba907f23e0bd3f78d91fa552479bb9c23ac09cbb1fd6` |

Official release metadata for the INT8 archive: 163002883 bytes,
SHA-256 `7d1efa2138a65b0b488df37f8b89e3d91a60676e416f515b952358d83dfd347e`.

## Reproduce the CPU probe

Create an isolated Python 3.12 environment and install the pinned probe dependencies:

```powershell
uv venv --python .venv/Scripts/python.exe .venv-sensevoice
uv pip install --python .venv-sensevoice/Scripts/python.exe -r requirements-offline-probe.txt
.venv-sensevoice/Scripts/python.exe scripts/probe_offline_asr.py --output build/offline-probe/report.json
```

Run inference with networking unavailable after dependencies are installed.
The probe additionally blocks Python socket connections; this alone is not an
OS-level guarantee against native-library network access. It loads local model
paths, uses CPU, runs ITN off/on, tests VAD, and records raw token timestamps,
durations, wall time, memory, dependency versions and input checksums.

Token timestamps are not automatically precise character boundaries. Check
numeric normalization, punctuation, English subwords, missing durations, and
manually annotated start/end errors before claiming word/character alignment.
Use representative videos and a real minimum-spec PC before publishing speed
or quality guarantees.

See [phase-one findings and alignment gate](offline-feasibility.md).
Probe-only input checks (separate from production dependencies):

```powershell
.venv-sensevoice/Scripts/python.exe -m unittest discover -s scripts/tests -p test_offline_probe.py
```

Model weights and converted artifacts have their own licensing terms. Preserve
the supplied notices and verify the exact weight license before distribution;
the runtime source-code license does not replace the model license.

## Manual-only optional alignment download

The user downloads all large model files manually. Do not automatically fetch
weights. Existing SenseVoice/Silero files do not need downloading again.
For the separate fa-zh feasibility experiment, use revision
`d7701644d6e336f093501241beca010519c0986f`, not a floating main branch.
Place these files directly in `models/fa-zh/`, retaining their exact names:

- [model.pt](https://huggingface.co/funasr/fa-zh/resolve/d7701644d6e336f093501241beca010519c0986f/model.pt?download=true)
- [seg_dict](https://huggingface.co/funasr/fa-zh/resolve/d7701644d6e336f093501241beca010519c0986f/seg_dict?download=true)
- [am.mvn](https://huggingface.co/funasr/fa-zh/resolve/d7701644d6e336f093501241beca010519c0986f/am.mvn?download=true)
- [tokens.json](https://huggingface.co/funasr/fa-zh/resolve/d7701644d6e336f093501241beca010519c0986f/tokens.json?download=true)
- [config.yaml](https://huggingface.co/funasr/fa-zh/resolve/d7701644d6e336f093501241beca010519c0986f/config.yaml?download=true)
- [configuration.json](https://huggingface.co/funasr/fa-zh/resolve/d7701644d6e336f093501241beca010519c0986f/configuration.json?download=true)
- [README.md](https://huggingface.co/funasr/fa-zh/resolve/d7701644d6e336f093501241beca010519c0986f/README.md?download=true)

The six runtime files total approximately 167 MB; README preserves source context.
No need to download `example/` for the existing sample experiment. This candidate
is not selected for production and requires a separate runtime investigation.
Keep exact model license terms alongside any eventual redistributed package.

### Alignment CPU runtime prerequisites

fa-zh assets are now present. model.pt SHA-256 matches official fixed-revision
metadata: `f34ede558af831fb504206b25f1c2f27ca2f77753c26c4dd38d03323153b6f73`.
The browser-added seg_dict.txt extension was removed to match the configuration.
No alignment inference has run yet: PyTorch/FunASR are absent from both local
virtual environments. These proposed probe wheels target Windows x64 / Python
3.12 and are not part of the production dependency lock:

- [torch 2.6.0+cpu](https://download-r2.pytorch.org/whl/cpu/torch-2.6.0%2Bcpu-cp312-cp312-win_amd64.whl):
  206,493,444 bytes; SHA-256 `4027d982eb2781c93825ab9527f17fbbb12dbabf422298e4b954be60016f87d8`.
- [torchaudio 2.6.0+cpu](https://download-r2.pytorch.org/whl/cpu/torchaudio-2.6.0%2Bcpu-cp312-cp312-win_amd64.whl):
  2,438,883 bytes; SHA-256 `75266c25d394bb5d70f83a38b1b4d858c074a767c18f7ff87443bdf193c1b236`.

Download manually into `build/offline-wheels/`, preserving complete .whl filenames.
Sizes and hashes were checked against the official PyTorch CPU indexes and HEAD
responses. FunASR and transitive packages still require a separate size/resolution
check; these two wheels are not a complete offline installation bundle.

### Remaining large runtime packages

The two CPU wheels passed SHA-256 verification and were installed locally in
`.venv-alignment`. NumPy 2.2.6 and psutil 7.0.0 were installed from the existing
local cache. FunASR 1.2.6 itself is installed without dependencies (about 0.7 MB).
The environment is incomplete and alignment inference has not run.

Dependency dry-run with requirements-alignment-probe.txt resolved 67 packages.
Using a conservative 10 MB per-file threshold, these additional large files were
identified. Download into `build/offline-wheels/` without renaming or extracting:

- [SciPy 1.15.3, 40.97 MB](https://files.pythonhosted.org/packages/e6/eb/3bf6ea8ab7f1503dca3a10df2e4b9c3f6b3316df07f6c0ded94b281c7101/scipy-1.15.3-cp312-cp312-win_amd64.whl)
  SHA-256: `52092bc0472cfd17df49ff17e70624345efece4e1a12b23783a1ac59a1b728ed`.
- [llvmlite 0.44.0, 30.33 MB](https://files.pythonhosted.org/packages/e2/3b/a9a17366af80127bd09decbe2a54d8974b6d8b274b39bf47fbaedeec6307/llvmlite-0.44.0-cp312-cp312-win_amd64.whl)
  SHA-256: `eae7e2d4ca8f88f89d315b48c6b741dcb925d6a1042da694aa16ab3dd4cbd3a1`.
- [jieba 0.42.1, 19.21 MB](https://files.pythonhosted.org/packages/c6/cb/18eeb235f833b726522d7ebed54f2278ce28ba9438e3135ab0278d9792a2/jieba-0.42.1.tar.gz)
  SHA-256: `055ca12f62674fafed09427f176506079bc135638a14e23e25be909131928db2`.

No additional large packages were downloaded by the agent. Some other PyPI
metadata requests failed with transport EOF, so do not claim the entire dependency
size audit or offline bundle is complete. Check remaining package sizes before
installation; additional large downloads must remain manual. No model auto-download.
