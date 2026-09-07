# Offline application integration — 2026-09-07

## Implemented

- GUI and `ASRAPI` default to local SenseVoice. Bcut requires explicit selection;
  local failures never select or call it automatically.
- Each GUI controller/API instance owns one resident `OfflineASR`. Local batches
  execute one file at a time regardless of the online concurrency preference.
  Ordinary file failure preserves prior exports and continues the remaining queue.
- Verified local model paths, lazy imports, CPU-only inference, VAD reset per file,
  fresh recognition streams, character mapping and raw acoustic interval validation.
- The application calls fa-zh's feature/encoder/predictor helpers directly. It does
  not use the experimental global sentence-postprocessor monkeypatch.
- FFmpeg writes into an owned temporary directory. PCM is read in 512-sample VAD
  windows; recognition reads at most 15 seconds per padded segment. Only one pending
  VAD span is retained for boundary lookahead, not the full waveform. Text/results
  still scale with transcript length and temporary disk use scales with audio length.
- Loading/conversion/scanning/recognition/alignment progress appears in the GUI
  notice and status tooltip. The existing `处理中` state is preserved for menu logic.
- Cooperative cancellation checks guard hash/read/inference boundaries and atomic
  export. Only the owned FFmpeg process/temp directory are cleaned up. An offline
  cancellation must not delete a same-stem MP3 belonging to the user.
- Separate offline cache includes decoded-audio SHA-256, model/token/VAD/alignment
  hashes, runtime versions, revision, thread count and normalization/segmentation
  settings. Cache hits are validated and retain character data. Failed/partial jobs
  are not cached. Cache write errors do not invalidate a successful transcription.

The small native sentencepiece extension is primed before Qt imports on Windows;
torch/FunASR and all weights still load in the background. This fixes an observed
native import-order hang (details in the Trellis research record).

## Run this checkout

The existing `.venv-alignment` now includes the seven GUI packages installed from
the machine's offline cache. No new model or large runtime download was performed.
The verified existing FFmpeg binary was copied to the ignored repository-root
`ffmpeg.exe`, where the source runtime expects it. Neither that binary nor models
should be committed.

```powershell
.venv-alignment/Scripts/python.exe asr_gui.py
```

For another source environment, use `requirements-offline.txt` with the manually
provided CPU wheels and local model layout in `docs/offline-models.md`. This source
dependency list is **not** a hashed/validated production release lock. The original
minimal GUI `.venv` does not contain the inference libraries; use the combined
environment above for offline validation.

```python
from API.asr_api import ASRAPI

api = ASRAPI()  # offline; keeps models across calls
api.batch_process(["first.mp4", "second.mp4"], output_dir="subtitles")
# Explicit online use uploads audio:
online = ASRAPI(engine="bcut", max_workers=3)
```

CLI: `python API/asr_api.py <input> --engine offline --models <directory>`.
Use `--engine bcut` only when online upload is intended. GUI models default to
`<application resource directory>/models`; source root `models/`, portable
`_runtime/models/`. GUI custom model-directory selection is not added yet.

## Verification

The actual API processed all six user benchmark videos, with a missing input
inserted between successful jobs: six successes, one failure, no pause. Actual
constructor/helper counts: SenseVoice 1, Silero 1, fa-zh 1. Full batch wall time
including video extraction/export was 66.842 seconds, peak working set
1,336,987,648 bytes, on Core Ultra 7 155H. This is a current-host reference only.
The generated SRTs are under `example/application-integration-2026-09-07/`.

The real Qt worker chain processed Chinese-path audio, then a corrupt WAV, then
another valid audio: statuses `已处理`, `错误`, `已处理`. A subsequent cache hit was
checked with native decoding replaced by a failing assertion: no decode occurred,
and the result retained 13 character intervals. Tests used local assets/socket
guards; no online Bcut call was made.

Application tests: 55 pass in the combined runtime (including the fresh-process
native-import regression); probe tests: nine pass. Python compilation and dependency
compatibility checks pass. Existing Bcut concurrency/rate-limit tests explicitly
select `engine="bcut"` after the default change.

## Deliberate remaining limits

- Cache lookup currently requires resident alignment identity; first speech after
  a fresh process still runs inference, and cache hits still perform conversion.
  Silence avoids loading the aligner. Model files are treated as immutable while
  resident; restart after replacing them.
- Cancelling cannot interrupt a native inference/import halfway through. Large
  model hashing in the alignment loader is also a bounded-file operation, not an
  instantaneous cancel guarantee.
- TXT currently follows the same validated-alignment path as timed export. If a
  spoken token cannot be aligned, that file fails rather than exporting an apparently
  complete but inaccurate timeline. Text-only recovery is not implemented yet.
- Human character timing accuracy, repeat-boundary variability, background-music
  coverage, long continuous-audio stress and packaged offline validation remain open.
  The model frontend's default dither was found during inspection but has **not**
  been changed or established as the cause of timing variability in this patch.
- No Windows release was built, no new distribution license approval is implied,
  and no commit/push was performed. Packaging and release-lock work remain next.
# Windows acceptance checkpoint (2026-09-07)

Windows is the only required platform this iteration; Linux/macOS packaging and
validation are deferred by user direction. This does not turn source checks into
packaged acceptance or prove human character-boundary accuracy.

- The combined `.venv-alignment` environment now contains its own GUI dependencies;
  tests no longer need another environment's site-packages. `uv pip check` passed
  for all 77 runtime packages before adding Nuitka 4.1.3 for build preparation.
- 56 application tests and nine probe tests pass. Python compilation, PowerShell
  parsing, UTF-8 BOM preservation and `git diff --check` pass.
- Real source GUI release check passed with a Chinese-path WAV, producing a
  non-empty SRT and task status `已处理`. Evidence: `build/windows-offline-check/report.json`.
- `--release-check offline-workflow --models <directory>` exercises the actual
  local GUI worker. Existing `workflow` now explicitly selects Bcut, regardless
  of the application's new default. Routing has a regression test.
- `requirements-release.lock` now includes the pinned CPU/offline stack and Nuitka.
  Hash generation queried dependency metadata; large torch wheels came from the
  supplied local directory. Build dependency sync is offline and hash-checked.
  Corresponding-source packaging includes the nested requirement inputs and docs.

Reproduce the source smoke check using a disposable input copy (the workflow writes
an adjacent SRT):

```powershell
.venv-alignment/Scripts/python.exe asr_gui.py --release-check offline-workflow --input build/windows-offline-check/中文样例.wav --models models --report build/windows-offline-check/report.json
```

No new portable ZIP was built in this checkpoint. Still required: native bundling
rules for dynamic FunASR/sherpa imports, release-environment hash sync, compiled
workflow checks through the launcher, separate model notices, and clean Windows
offline/security-software validation. Preserve the previous v1.1.0 release when
choosing the new candidate version/output directory.
