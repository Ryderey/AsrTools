# Full-video offline validation — 2026-09-06

## Result and scope

The user-supplied traction-beam video completes local SenseVoice recognition and
fa-zh character-boundary prediction. All 688 recognized spoken characters have
ordered start/end intervals, including the English abbreviation IDC. This is
functional evidence, **not human-verified character timing accuracy**.

The approved Bcut TXT remains unchanged. Normalized reference length is 689;
local output length is 688. Levenshtein distance is 10, CER 1.4514%. Normalization
uses NFKC, case folding, and removal of whitespace/punctuation only. This measures
agreement with the user-selected online reference, not independently measured
recognition accuracy. No reference text is supplied to offline ASR or alignment;
the aligner receives the local recognizer's own text.

## Measurements

Host: Intel Core Ultra 7 155H, 32 GB RAM, Windows 11 x64, Python 3.12.13.
ASR/alignment use four CPU threads; VAD uses one. CPU only, no GPU.

| Experiment | Audio | Measured processing | First text | Peak working set | Character coverage |
|---|---:|---:|---:|---:|---:|
| Final full-video run | 156.459 s | 15.503 s | 2.359 s | 1,306,947,584 bytes / 1.217 GiB | 688/688, 24 segments |
| Earlier four-repeat stress run | 625.835 s | 56.567 s | 4.126 s | 1,326,624,768 bytes / 1.236 GiB | 2753/2753, 95 segments |
| Three-second silence check | 3 s | 1.327 s | No text | 377,176,064 bytes | No speech; aligner not loaded |

Measured processing includes model initialization and inference inside the probe,
but excludes interpreter startup, video-to-WAV extraction, and subtitle export.
The final run additionally hashes all ASR/VAD inputs before native construction.
These are individual runs, not statistically established performance guarantees.
The target ninth-generation Intel/16 GB machine has not been tested.

The stress input is the same WAV repeated four times, not an independent long
video. All global character intervals are ordered without overlap. The repeated
opening phrase's first-character onset differs by at most 10.7 ms relative to
each repetition's origin. This limited self-consistency check is not an annotated
long-video drift measurement. The silence run used the spoken reference only to
exercise the pipeline; its CER is not a useful quality metric.

Cancellation after the first ASR segment exited with code 130 in 0.330 s and
created no output directory. Cancellation is cooperative at chunk/segment
boundaries; it cannot interrupt an active native inference or library import.

## Artifacts

- `example/offline-comparison-final/offline.txt`: local recognition text.
- `example/offline-comparison-final/offline.review.srt`: readable grouped cues.
- `example/offline-comparison-final/offline.review.ass`: same grouped cues in ASS.
- `example/offline-comparison-final/offline.characters.srt`: one spoken character
  per cue, for boundary inspection rather than normal viewing.
- `example/offline-comparison-final/offline.sentences.srt`: coarse padded VAD
  intervals, deliberately not presented as precise timing.
- `example/offline-comparison-final/report.json`: raw text, token mapping,
  intervals, hashes, dependency versions, timings, memory and differences.
- `build/offline-probe/stress-4/report.json`: repeated-audio stress evidence.

Punctuation has no fabricated spoken duration. Grouped cues retain display
punctuation and use the first/last predicted character boundaries. The original
video and model weights must not be committed with the small text artifacts.

## Why the abbreviation initially failed

FunASR's sentence postprocessor merges English letters into a word, reducing
the returned timestamp count. The probe captures tokens and intervals before
that merge and checks them against the punctuation-free target. It does not
split a word interval uniformly or infer missing durations. A regression test
covers the three-letter merge and restoration of the original function.

The capture temporarily patches a global function during a synchronous call.
It is **probe-only**, unsuitable for concurrent application workers. Production
must expose raw intervals through a local alignment adapter, without global
patching. Unknown tokens/count mismatches remain explicit failures; the probe
exports partial subtitles with a `.partial` suffix rather than hiding gaps.

## Reproduce

Use the already prepared `.venv-alignment`; the observed 70-package environment
is recorded in `requirements-alignment-probe.lock`. Both sherpa packages were
installed from the existing cache; no additional large download was needed.

```powershell
$env:PYTHONIOENCODING = 'utf-8'
$env:NUMBA_CACHE_DIR = Join-Path $PWD 'build/numba-cache'
.venv-alignment/Scripts/python.exe -m scripts.probe_video_offline `
  --audio build/b-reference/traction-beam.wav `
  --reference example/b-reference/traction-beam.bcut.txt `
  --output build/offline-probe/new-run `
  --cpu-name 'Intel(R) Core(TM) Ultra 7 155H'
.venv-alignment/Scripts/python.exe -m unittest discover -s scripts/tests -v
.venv/Scripts/python.exe -m unittest discover -s tests
```

Use a new/empty output directory. Add `--repeat 4` for synthetic stress or
`--cancel-file <sentinel-path>` for cooperative cancellation. The program does
not download models. Python socket connections are blocked during the run and
hub offline mode is set; this guard alone does not prove native-code network
isolation. Physical-disconnection/OS-enforced packaged testing remains pending.

## Next implementation boundary

Nine probe tests and 42 application regression tests pass; compilation passes.
Production GUI/API behavior and production dependencies are unchanged. Continue
with bounded decoding, one resident model owner, local error handling and manual
Bcut selection using the task design. Release acceptance still requires human
character-boundary labels, diverse real audio, minimum-spec measurements and
Windows distribution/licensing checks. None of those outstanding claims should
be silently marked passed by this experiment.
