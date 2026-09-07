# fa-zh CPU alignment checkpoint — 2026-09-06

## Outcome

The manually supplied fa-zh model now loads and produces character start/end
intervals for the supplied Chinese WAV on CPU. This passes a local functionality
smoke test, **not** precise-alignment acceptance. Task `offline-sensevoice` remains
`planning`; no production GUI/API or release dependency lock was changed.

All five manually supplied runtime archives passed their documented SHA-256
checks. The remaining package sizes were checked against PyPI before installation.
Large files were installed locally; small dependencies were installed into
`.venv-alignment`, independently of `.venv` and `.venv-sensevoice`.
The installed dependency graph passes `uv pip check` (68 packages). The exact
observed versions are in `requirements-alignment-probe.lock`.

## Experiment and resource use

CPU: Intel Core Ultra 7 155H; Windows 11; 32 GB RAM; four inference threads;
Python 3.12.13, torch/torchaudio 2.6.0+cpu, FunASR 1.2.6.
Audio: `models/sensevoice-small-int8/test_wavs/zh.wav`, 16 kHz mono PCM16,
5.592 seconds, SHA-256
`b77f1794fe374a0ba1ee1dc458bfaf9349496cbbfc32780c50ba3c5a7ad8e373`.

Two separate process executions ran three transcript variants twice each:
spoken Chinese numerals, digits without punctuation, and digits with punctuation.
No custom media or human-labelled timing reference was available.

| Metric | First process | Second process |
|---|---:|---:|
| Total timed probe, including imports and six alignments | 38.072 s | 22.096 s |
| Library import/setup portion | Not separately measured | 20.424 s |
| Model construction and weight load | 0.533 s | 0.536 s |
| Individual alignment range | 0.113–0.214 s | 0.151–0.171 s |
| Process peak working set | 937,037,824 bytes | 903,958,528 bytes |

Peak memory is approximately 894 / 862 MiB and includes the broad FunASR runtime,
not only its model weights. The alignment environment contains approximately
1.67 GB of file contents (including installed dependencies/bytecode, excluding
the separately stored models and wheelhouse). This is not a packaged release
size or a combined SenseVoice-plus-aligner peak-memory measurement.
Total probe time excludes interpreter startup and final JSON serialization.
Do not extrapolate this short test to ninth-generation Intel hardware or long video.
The measured import delay must be addressed explicitly in any later app design;
warm per-segment times alone would hide poor first-use responsiveness.

## Character mapping and punctuation failure

`开饭时间早上九点至下午五点` produces 13 known tokens and 13 non-overlapping
start/end intervals. Replacing 九/五 with 9/5 also produces 13 known tokens and
13 intervals. Examples from the first process (milliseconds):

| Token | Start | End |
|---|---:|---:|
| 开 | 790 | 1030 |
| 九 / 9 | 2630 | 2870 |
| 最后一个 点 | 4870 | 5265 |

These are model predictions, not human-verified boundaries.
The raw transcript `开饭时间早上9点至下午5点。` instead tokenizes into 14 tokens,
including one `<unk>` for punctuation. Output postprocessing drops the unknown
token and returns only 13 intervals, while the timing distribution has already
changed. For example, the final 点 ends at 4850 ms instead of 5265 ms in the
first process. The 415 ms difference is a comparison between model outputs,
**not** a measured error against the true audio boundary.

This cannot be fixed by simply deleting punctuation from the returned text:
punctuation must be excluded from the alignment target before inference while
preserving an explicit mapping back to display text. Unknown spoken tokens and
unmapped normalized numerals must fail alignment validation rather than silently
being presented as accurately aligned subtitles. Longer numbers, dates and
English subword mappings remain untested.

## Why this is still an acceptance gate

In the installed FunASR 1.2.6 source, `MonotonicAligner.inference` computes
`text_lengths` from token counts, and passes that count with acoustic encoder
outputs into `calc_predictor_timestamp`. Text identities are attached later to
the resulting intervals. This is a length-conditioned timestamp predictor, not
evidence that a phonetic/text mismatch was detected. A well-formed timestamp
array does not establish that the supplied transcript actually matches speech.
Source: `funasr/models/monotonic_aligner/model.py` in the installed release;
token conversion is in `funasr/tokenizer/char_tokenizer.py`.

Neither human start/end error nor long-video accumulated drift was measured.
The next experiment needs representative video clips, verified transcripts and
human character-boundary annotations, with agreed error thresholds. It must
cover punctuation, multi-character numerals, missing/extra recognized words,
background music and silence before selecting this method for production.

## Offline controls and reproduction

The model is constructed from explicit local YAML/token/CMVN/weight paths with
the weight checksum verified, CPU forced, update checks disabled and remote code
disabled. Python connection APIs are blocked before importing FunASR. Inference
ran in the network-restricted execution sandbox. This is not yet an OS firewall
or physically disconnected packaged-app acceptance test.

```powershell
$env:NUMBA_CACHE_DIR = Join-Path $PWD 'build/numba-cache'
.venv-alignment/Scripts/python.exe -m scripts.probe_alignment --cpu-name 'Intel(R) Core(TM) Ultra 7 155H' --output build/offline-probe/alignment-repeat.json
.venv-alignment/Scripts/python.exe -m unittest discover -s scripts/tests -v
uv pip check --python .venv-alignment/Scripts/python.exe
```

The probe uses fixed transcripts for the supplied zh.wav only; `--audio` is not
a general transcription interface. Raw evidence remains in the ignored build
directory: `alignment.json` and `alignment-repeat.json`. Use the latter for the
installed dependency snapshot: the first report's unrestricted metadata scan
also included setuptools-vendored distributions after library import.

Validation: four probe tests and 42 production regression tests passed;
`py_compile`, `git diff --check` and Trellis context validation passed. No project
lint/type-check configuration is defined for these standalone experiment scripts.

Redistribution review and Windows packaging remain open. This experiment does
not grant model-license clearance or select the full PyTorch stack for release.
