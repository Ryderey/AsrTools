# Six-file benchmark and resident-model failure recovery

## Scope

User supplied six MP4/TXT pairs under `example/test`. The typed alternative path
does not exist; the matching directory in this project was used. TXT files are
unchanged text references, not character-boundary annotations. No online service
was called. Each source was extracted to 16 kHz mono PCM16 using the existing
verified FFmpeg build input and matched to its reference by SHA-256.

Per the user's 2026-09-07 amendment, current-machine resource figures replace the
minimum-spec hardware acceptance requirement for this task. They are a reference,
not a ninth-generation Intel performance guarantee.

Host: Intel Core Ultra 7 155H (verified from Windows processor registry), 16 physical
cores / 22 logical CPUs, approximately 32 GB RAM; CPU-only, ASR/alignment four
threads and VAD one thread. Exact system memory, dependency versions, input/model
hashes and CPU counters are in each `report.json`.

## Results

| ID / topic | Audio seconds | Separate-process seconds | Resident seconds | Reference CER | Aligned characters |
|---|---:|---:|---:|---:|---:|
| 1 / Turing patterns | 178.539 | 33.865 | 15.479 | 1.57% | 763/763 |
| 2 / Venturi effect | 176.171 | 17.495 | 8.179 | 3.87% | 747/747 |
| 3 / Hanger reflex | 226.923 | 19.182 | 10.210 | 3.51% | 965/965 |
| 4 / Light-year | 210.240 | 18.160 | 10.206 | 1.69% | 1006/1006 |
| 5 / Paraquat | 174.485 | 16.669 | 7.958 | 2.18% | 780/780 |
| 6 / Semiconductor | 178.368 | 15.899 | 8.568 | 2.57% | 739/739 |

Total audio: 1144.725 seconds (19m04.7s). Weighted CER: 128 edits / 5007 reference
characters = 2.56%; predicted intervals cover 5000/5000 recognized characters.
CER measures agreement with the supplied TXT, not independently certified accuracy.
Normalization remains NFKC/casefold and punctuation/whitespace removal, with no
homophone correction or numeral rewriting to improve the score.

Separate-process measured processing totals 121.270 seconds; resident successful
processing totals 60.599 seconds. The resident batch's wall time including the
extra failed transcription and result comparison is 65.791 seconds. These are
single runs with differing startup/cache effects, not a controlled speedup claim.
Per-file processing includes model setup when needed but excludes interpreter
startup, FFmpeg extraction and final export. The first independent run's longer
startup is retained, not discarded as an outlier.

Resident peak working set is approximately 1.259 GiB. Per-file average process CPU
as a share of the machine's 22 logical CPUs: 3.95%, 7.09%, 6.04%, 6.07%, 5.61%,
5.34%. This is CPU-seconds / elapsed-seconds / 22, **not a sampled peak or total
system load**, and does not compensate for unequal P/E core capacities. Reports
also contain the unnormalized one-core percentage, CPU seconds and end RSS.
Resident per-file peak memory is the process-lifetime high-water mark, not a reset
per-file measurement. No claim of leak-free indefinitely long operation is made.

## Load-once and failure test

`scripts/probe_resident_batch.py` wraps the actual native constructors/load helper
with call counters while using real local models. One runtime is retained for the
whole synchronous batch. VAD state resets per file; recognition uses fresh streams.

- Actual load counts: **SenseVoice 1, Silero VAD 1, fa-zh 1**.
- After successful file 1, an extra transcription of file 2 raises a deliberately
  injected alignment exception after its ASR stage has run.
- That failure exports no result directory. The next six-file sequence entries
  continue using the same models; all six real files succeed. Existing successful
  artifacts remain intact.
- Text, segment count, VAD sample offsets, character identity/mapping and coverage
  match the independent runs. No per-file VAD offset leakage was observed.
- `tests/test_batch_failure.py` independently checks the existing API contract with
  three mocked jobs: success, ordinary failure, success; counts are 2 success,
  1 failed, 0 paused, with both successful files preserved.

This validates the **experimental resident runner**, not production GUI integration.
The application implementation must retain existing semantics: ordinary file errors
are recorded and the queue continues; Bcut rate-limit/polling errors retain their
separate pause behavior. Never automatically upload after local failure, reload
healthy models merely for a file error, or delete previous successful results.
Fatal native crashes/unusable shared models are outside this recoverable-error test.

## Timing variability remains explicit

The first resident experiment stopped on a strict interval-equality assertion.
Inspection showed identical text and VAD offsets but differences in predicted
character boundaries. The final regression requires exact text/mapping/VAD identity
and records boundary differences separately instead of falsely claiming determinism.
Maximum cold-vs-resident boundary differences by file: 0, 20, 0, 120, 20, 20 ms.
The cause and human significance are not established. This is **not** an accepted
120 ms error threshold, nor evidence of accuracy against human speech boundaries.

## Reproduce and inspect

Independent evidence/subtitles: `example/benchmark-2026-09-07/1` through `/6`.
Final resident evidence/subtitles: `example/benchmark-resident-2026-09-07-v2/1`
through `/6`; summary `batch.json` includes source filenames, outcomes and counters.
Each file directory includes TXT, readable SRT/ASS, character SRT and raw JSON.
Original videos, references and earlier failed experiment artifacts are preserved.

```powershell
$env:PYTHONIOENCODING = 'utf-8'
$env:NUMBA_CACHE_DIR = Join-Path $PWD 'build/numba-cache'
.venv-alignment/Scripts/python.exe -m scripts.probe_resident_batch `
  --source example/test --audio-dir build/benchmark-2026-09-07 `
  --output build/new-resident-run --cpu-name 'Intel(R) Core(TM) Ultra 7 155H'
.venv-alignment/Scripts/python.exe -m unittest discover -s scripts/tests
.venv/Scripts/python.exe -m unittest discover -s tests
```

This dedicated regression expects the numbered extracted WAVs and independent
reports from this benchmark; it is not a generic production batch API. Use a new
output directory. Socket guards and local-only model paths remain enabled;
packaged OS-enforced offline verification is still separate.
