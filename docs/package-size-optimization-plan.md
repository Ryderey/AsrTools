# Full Windows package optimization plan

Date: 2026-09-12. Status: **implemented**. Stages A and B are in the build
pipeline and a Windows x64 candidate was compiled and packaged. G0 unit guards
and the G1 source-tree equivalence comparison passed; packaged-runtime
acceptance (G3) remains user-owned and was not run. See section 8 for the
measured result.

This plan follows the [measured package audit](package-size-audit-2026-09-12.md).

## 1. Recommended scope and target

Use two implementation stages: **A, remove narrowly identified delivery data;
B, restrict FunASR to the selected inference path**. Stage B is the main route to
a substantial reduction. Preserve the current models and inference arithmetic.

The baseline is the Windows x64 v1.2.0 portable ZIP identified in the audit:
703.37 MiB compressed, 1,578.18 MiB uncompressed, 10,191 members, SHA-256
`0b54e5516bd92bc4208deb75f9fdfc2fb44094e5a474f0149beaabe734dd6243`.

| Milestone | Size objective | Evidence and qualification |
|---|---|---|
| A: delivery exclusions | Remove 17.51 MiB of current compressed payload and 39.61 MiB of uncompressed files | Exact measured candidate inventory; remaining payload bytes must be unchanged. About 685.86 MiB before deducting removed archive headers or compression improvements. Runtime equivalence is still required. |
| B: inference dependency closure | **Proposed full-package target: at most 650 MiB**, at least 7.6% smaller than baseline | Engineering target, not an achieved result or validated forecast. Re-estimate after dependency and compilation evidence. Never meet it by weakening capability. |
| Further reduction | Set a new target only after measuring B | No supported promise of 500 MiB, half-size, or a particular EXE reduction yet. |

The four current groups llvmlite/scipy/scipy.libs/sklearn occupy 62.05 MiB of ZIP
payload. Removing all of them plus A, with all other bytes unchanged, gives an
illustrative 623.81 MiB before archive-header changes. This is an upper opportunity
scenario, **not an expected output size**: some dependencies may remain, and a
rebuilt executable can change size in either direction.

“No capability loss” means the same first-use offline operation, CPU recognition,
character timing, online selection, supported media handling, GUI, exports,
cancellation, queue behavior and deployment compatibility. It does not require
shipping upstream training, model-hub, speaker-clustering or export APIs that
ASRTools does not expose. Keep the nine model/config bytes and all model notices.

## 2. Stage A — delivery-only changes

### A1. Exclude two exact subtrees

Add two reviewed exclusions to the portable staging/collection path:

```text
_runtime/torch/include/**
_runtime/jieba/lac_small/**
```

Keep this as one small rule in the existing release helper, not a general plugin
system or an unrestricted “delete unused files” pass. Apply it only to a newly
created portable staging directory. Do not delete from the source environment,
the original `.dist`, existing releases, or the model directory.

The first candidate should use the same compiled runtime and the same ZIP
compression settings as baseline. It can be assembled from a verified extraction
of that ZIP into a new candidate staging directory if its compilation inputs are
no longer available. Label it as a repack of the baseline hash, not a fresh build.
For subsequent full builds, enforce the same exclusions automatically.

Acceptance is exact: the member-name difference is the two intended subtrees,
all retained file-content hashes match baseline, all nine model hashes match,
and the packaged functional checks pass. Retain metadata and licenses even where
they describe the original upstream distribution's full inventory.

### A2. Tune compression separately

Keep DEFLATE ZIP and the existing portable layout. Evaluate explicit level 9 in
`scripts/release_payload.py:archive`; preserve ZIP64, atomic `.partial` completion,
and previous releases. A higher compression level changes download size without
changing the installed files.

Compare the default and level 9 on the same staged tree. Adopt level 9 if the
measured size benefit is worthwhile for release packaging time. Do not mix this
comparison with dependency pruning, and do not add the same saving twice when
the removed subtrees themselves compress differently.

The planning-time in-memory result is only **2.07 MiB** smaller than the existing
ZIP, or **2.05 MiB** additional payload saving after A1. Treat this as optional
polish, not the route to the 650 MiB objective. Section 7 records the compressor
version limitation and measurement method.

## 3. Stage B — compile only the selected FunASR inference path

The detailed [FunASR inference pruning design](funasr-inference-pruning-design.md)
specifies this stage's implementation contract. It refines the mechanism below:
an authored profile drives guarded generation of four reduced modules and Nuitka
`module_code` YAML, with an original `build_model` method and unchanged numerical
definitions. The YAML is generated build output rather than a manually maintained
copy of upstream source. Use that design's file responsibilities and acceptance
gates for implementation.

### B1. Preserve the application interface

Keep these current entry points and result shapes:

```text
load_aligner(model_dir, threads) -> (model, hashes, load_seconds)
character_intervals(model, samples, text) -> validated character spans

model.model
model.kwargs["frontend"]
model.kwargs["tokenizer"]
```

Prefer retaining upstream `AutoModel.build_model` and its loading behavior to
writing another checkpoint loader. It sets random seeds and CPU thread count,
constructs tokenizer/frontend, calculates input/vocabulary sizes, merges model
configuration and loads parameters. These are semantic requirements, not just
incidental imports. The existing loader also handles checkpoint wrapper keys and
`module.` prefixes; a bare `torch.load` replacement would miss that behavior.

Keep `character_intervals`' existing feature extraction, encoder, timestamp
predictor, `ts_prediction_lfr6_standard`, character validation and tail handling.
Do not quantize, change dither, interpolate timestamps, replace Torch with ONNX,
or alter audio normalization as part of packaging work.

### B2. Make upstream import selection explicit at compilation

Recommended mechanism: a version-pinned Nuitka user package configuration in
`release-assets/funasr-inference.nuitka-package.config.yml` (new, proposed).
Use its import/anti-bloat transformations at compile time, so the development
environment remains a usable reference installation. Do not accumulate manual
edits to `.venv-alignment`.

The configuration and its source guards must cover these boundaries:

| Upstream location | Proposed transformation | Behavior to retain |
|---|---|---|
| `funasr/__init__.py` | Replace recursive `pkgutil` discovery/import with the explicit selected registrations; remove unused public frontend entry imports | Version metadata and the required registrations exist before model construction |
| `funasr/auto/auto_model.py` | Remove eager speaker-clustering/export/download-path imports that are outside the selected local configuration; restrict their unused branches consistently | Current local `build_model`, CPU settings, tokenizer/frontend construction, parameter loading and `.kwargs` shape |
| `funasr/utils/load_utils.py` | Prevent unused general media/URL-loading imports from pulling in librosa and other backends | The existing `extract_fbank` implementation used on application-provided PCM arrays |
| `funasr/frontends/wav_frontend.py` | Remove the eager EEND-only feature dependency from this selected frontend module | Original WavFrontend code, Kaldi fbank, CMVN and LFR arithmetic |
| Remaining selected module imports | Resolve the transitive closure and cut only uncalled optional branches | Every helper needed during initialization as well as inference |

These are transformations to design and verify, not an assertion that a ready
configuration already exists. Use literal/version-guarded transformations and
verify the expected transformation actually applied. If an upstream file no longer
matches, stop the build. Do not silently fall back to recursive inclusion and
claim the size objective was met.

The current fa-zh configuration establishes the following initial registration set:

| Registration | Defining module in pinned FunASR 1.2.6 |
|---|---|
| `MonotonicAligner` | `funasr.models.monotonic_aligner.model` |
| `SANMEncoder` | `funasr.models.sanm.encoder` |
| `CifPredictorV3` | `funasr.models.bicif_paraformer.cif_predictor` |
| `WavFrontend` | `funasr.frontends.wav_frontend` |
| `CharTokenizer` | `funasr.tokenizer.char_tokenizer` |
| `SpecAugLFR` | `funasr.models.specaug.specaug` |

Also preserve `register`, tokenizer base classes, transformer/SANM helpers,
checkpoint loading, timestamp tools and the existing registry inspect fix.
This is a seed list, not a complete dependency whitelist. For example,
MonotonicAligner constructs augmentation and loss objects even though inference
does not train, and its module imports additional helpers. Preserve those until
their initialization dependencies have been proved unnecessary.

Moving an import inside an unused function alone does not ensure Nuitka omits it.
After resolving runtime reachability, pair the transformations with explicit
compilation exclusions for the proven-unneeded paths. Conversely, applying
`--nofollow-import-to` without removing a required eager import will break startup.
Do not use import exceptions or missing registry entries as evidence of success.

If this import-only route cannot preserve construction and inference without
widespread algorithm changes, stop B and retain A. A separate inference adapter
would require a revised design; it is not the default fallback in this plan.

### B3. Remove the dependency chain only after proving it is disconnected

Priority candidates are librosa → Numba → llvmlite, and optional speaker
clustering → sklearn/SciPy. Currently the selected frontend's module and
`load_utils` eagerly reach librosa; simply deleting LLVM is invalid.

For each candidate, record both:

1. Static inclusion reasons from the new Nuitka report, including imports that
   are not observed during one test run.
2. Required imports and native loads during cold initialization and the complete
   supported runtime corpus, including error/cancellation paths.

Then assert the expected absent modules/data/native libraries in the resulting
archive. A failed import hidden by upstream broad exception handling is a failure,
not evidence of an unnecessary dependency. Leave Numba enabled until its whole
chain is demonstrably absent; changing the JIT switch is not the optimization.

Keep PyTorch CPU, torchaudio's Kaldi implementation, NumPy, sherpa-onnx,
onnxruntime, sentencepiece initialization and the selected FunASR code. Retain
NumPy's BLAS library. SciPy's different BLAS DLL is removable only if all SciPy
consumers disappear, not by treating it as a duplicate of NumPy's library.

### B4. Make build reuse and source delivery reflect the profile

Modify `scripts/build_release.ps1` to replace `--include-package=funasr` with the
validated selection/configuration. A changed compilation profile requires a full
recompile. Stage A's artifact reuse must not be reused as evidence for B.

Extend `release_payload.py:compile_fingerprint` to hash the new package
configuration contents and the relevant transformation inputs. Currently it
hashes stable argument strings, application resources/sources and environment
Python sources; a YAML path in the arguments alone does not detect a changed YAML
file at that path. Test that editing the profile invalidates compile reuse.

The existing FunASR source-copy step must also follow the accepted closure.
Otherwise a full `.py` tree can reintroduce excluded code or recursive discovery.
Preserve source files required for inspect/linecache, and their notices; retain
the complete corresponding source and transformations in the separate source ZIP.
Do not strip all FunASR source to save its small compressed contribution.

Keep `requirements-release.lock` versions fixed during this work. An installed
build dependency need not be shipped, and pruning the environment itself adds
unnecessary resolver and reproducibility changes. Defer splitting the probe and
release requirement inputs until there is a separately justified maintenance need.

## 4. Work items and verification sequence

All paths below describe future changes. This planning round edits documentation only.

| Order | Files/responsibility | Required output before moving on |
|---|---|---|
| 0 | Existing baseline artifact and audit | Retained baseline hash/inventory; agreed reference corpus; build provenance gaps recorded |
| 1 | `scripts/release_payload.py`, staging call in `scripts/build_release.ps1`, `tests/test_release_helpers.py` | A1 candidate; exact retained-file comparison; model checks; no modification to source `.dist` or previous archives |
| 2 | Compression option in the same helper | A2 size/time comparison and unpacked-content equality; choose one ZIP setting |
| 3 | New `release-assets/funasr-inference.nuitka-package.config.yml`; small guard support in existing release helper if necessary | Selected registration/import closure, pinned transformation checks, unchanged numerical function bodies |
| 4 | `scripts/build_release.ps1`, `compile_fingerprint`, source collection, release-helper tests | Configuration passed to Nuitka; profile changes invalidate reuse; source archive carries everything required to reproduce transformations |
| 5 | Existing offline checks plus one focused original/candidate equivalence harness | New compilation report, ZIP group inventory, expected dependency absence; controlled cold-runtime comparison |
| 6 | User-owned packaged GUI/distribution acceptance | Same-hash output/behavior/performance evidence; candidate meets capability requirements; measured target decision |

Preserve the current builder's no-auto-launch policy. Its successful completion
must produce checksums and a complete manifest; the historical missing-manifest
failure remains a delivery prerequisite, not a size optimization. Candidate
runtime checks are a separate explicit validation activity owned by the user.

Use existing tests and conventions; do not create one test per exclusion constant.
Meaningful additional coverage is an archive with retained/excluded members,
retained byte equality, guard failure on unexpected upstream sources, registry
completeness, and changed-profile invalidation of compilation reuse.

### Equivalence and performance gates

- Compare baseline and candidate in separate fresh processes with the same CPU,
  thread count, model/input bytes and no result cache. Exercise cache-hit behavior
  separately so cached outputs cannot conceal a broken new inference path.
- Preserve model-loading RNG behavior. `WavFrontend` defaults to `dither=1.0`;
  both comparison processes need matched RNG state at corresponding operations
  and the same file order. Do not set dither to zero to make tests easier.
- Compare tokenizer output, feature shapes/dtypes/lengths, feature values,
  encoder outputs, alphas/peaks and final character/display-index mapping. With
  controlled RNG and unchanged math, require matching final text and exported
  timing. Quantify baseline repeatability before choosing any numeric tolerance;
  do not relax it after seeing a candidate discrepancy.
- Run the existing real GUI success/failure/success video sequence, long/short
  segments, silence, unsupported characters, cancellation, clean shutdown,
  Chinese paths, SRT/TXT/ASS exports and explicit online workflow.
- Run cold startup without Python, compiler or system FFmpeg; verify all current
  supported media conversions and no network retrieval for local inference.
- Measure cold load and transcription/RAM over repeated runs on the same machine.
  A repeatable slowdown or memory regression beyond baseline measurement noise
  blocks acceptance; do not exchange functionality or throughput for the size target.

Reuse `tests/test_offline_engine.py`, `tests/test_offline_gui.py`, release-helper
and Bcut tests, and `scripts/check_gui_batch.py --videos` with its
`--inject-failure` variant. Mocked tests or a source-only success do not replace
the packaged native checks.

## 5. Stop conditions and rollback

Keep one candidate directory per stage, recording parent hash, changed inputs,
inventory delta, compressed/uncompressed sizes, member count, build duration and
validation status. Do not overwrite the baseline or make a runtime feature toggle
for switching dependency profiles in the customer application.

- If A breaks a required path, restore the specific excluded subtree and repeat
  the same comparison. Do not suppress the resulting import error.
- If B cannot prove equivalence or remove the targeted import chain, keep the last
  validated A artifact. Revert the build profile/source-copy/fingerprint changes
  together; retain the failed report for diagnosis.
- If B passes functionality but remains above 650 MiB, report the actual outcome
  and inclusion evidence. Revise the size target or investigate the next specific
  contributor; do not remove models or change precision to satisfy the number.
- If packaging changes only, compare retained byte hashes. If compiled inputs
  change, invalidate reuse and repeat the entire required runtime gate.

## 6. Deferred alternatives

Do not spend the first iteration on Qt replacement, custom Torch builds, custom
FFmpeg codecs, model quantization, a new ONNX aligner, model downloads at first
launch, 7z-only delivery or onefile/self-extracting packaging. They either change
the capability/deployment contract or have larger validation costs than the known
opportunities. Preserve existing licensing and source delivery.

The duplicate `asmjit.dll`/`fbgemm.dll` pairs offer only 1.15 MiB compressed and
need native load-path proof. Treat them as a separate optional follow-up after B,
not part of its promised benefit. Metadata and copied FunASR sources are similarly
small; keep them until their consumers are mapped.

## 7. Planning validation record

The audit provides measured archive inventory, ZIP CRC validation and all nine
model checksums. This plan additionally traces AutoModel construction, the exact
selected registration modules, frontend dither behavior and compilation-cache
invalidation inputs. No application or inference was executed while planning.

### In-memory full-package recompression

Every member was read with Python `zipfile`, streamed in 1 MiB chunks through
`zlib.compressobj(9, zlib.DEFLATED, -15)`, and flushed. Compressed bytes were counted
and discarded. This exercised ZIP member decompression/CRC checks but did not
write or extract a candidate archive. No functional validation was performed.

| Measurement | Result |
|---|---:|
| Measurement environment | Python 3.10.0, zlib 1.2.11 |
| Existing ZIP compressed-member bytes | 735,590,676 |
| Measured level-9 compressed-member bytes | 733,416,286 |
| Payload reduction versus existing ZIP | 2,174,390 bytes / **2.07 MiB**, about 0.29% of ZIP size |
| Estimated ZIP with existing header overhead | 735,366,962 bytes / **701.30 MiB** |
| A1 candidate files at this compression level | 18,339,149 compressed bytes |
| A1 + level 9 estimate before removing A1's ZIP headers | 717,027,813 bytes / **683.81 MiB** |
| Single successful pass, read/decompress/compress wall time | 112.55 seconds |

The prepared release environment is Python 3.12.13 with zlib 1.3.1. Therefore this
is a measured alternative-compressor payload comparison, **not** an isolated
level-6-versus-level-9 timing comparison in the release environment. The 112.55
seconds includes reading/decompression and omits writing a final ZIP. It cannot
be reported as extra release build time or an exact future package size.
If A2 is implemented, perform the final comparison with the same release Python,
zlib version and member inventory. No additional compression benchmark is required
to establish the main design decision: prioritize dependency/code inclusion.

## 8. Implementation record and measured result

Implemented after this plan:

| Change | File |
|---|---|
| Reviewed delivery exclusions and explicit ZIP compression level | `scripts/release_payload.py` (`PORTABLE_EXCLUSIONS`, `prune_portable`, `archive(compresslevel=9)`) |
| Exclusions applied to the fresh staging tree | `scripts/build_release.ps1` |
| Inference profile (hashes, retained nodes, registry, 52-module closure) | `release-assets/funasr-inference-profile.json` |
| Guarded generator for the four projected modules and the Nuitka configuration | `scripts/prepare_funasr_profile.py` |
| Closure compilation, closure-based source collection, report gate, fingerprint inputs | `scripts/build_release.ps1`, `scripts/release_payload.py` |
| G0 guards and the G1 equivalence harness | `tests/test_funasr_profile.py`, `tests/test_release_helpers.py` |

The Stage B profile is the build default. `-DisableInferenceProfile` restores the
previous full-package inclusion as the documented rollback path.

### 8.1 Stage A, measured

The baseline archive was extracted, pruned and re-archived at DEFLATE level 9
without recompiling. All 1,483 retained members were byte-identical by CRC and
size, and all nine model hashes were re-verified after pruning.

| Measurement | Baseline | Stage A candidate |
|---|---:|---:|
| Portable ZIP | 737,541,352 B / 703.37 MiB | 715,318,975 B / 682.18 MiB |
| Members | 10,191 | 1,483 |
| Removed | — | 8,708 files / 41,538,131 B raw |

Stage A saving: **22,222,377 B / 21.19 MiB / 3.01%**. This artifact was a repack
of the baseline hash, not a fresh build.

### 8.2 Stage B, measured on a fresh compilation

A full Windows x64 build was compiled from source with the inference profile
enabled (Nuitka 4.1.3, `--lto=yes`, Python 3.12.13, FunASR 1.2.6). The
compilation report gate passed: 52 reviewed FunASR modules compiled, **0 modules
outside the closure**, `librosa`, `numba` and `llvmlite` absent. `scipy` remained
as the one allowed conditional dependency. Nuitka recorded a full module
replacement for each of the four projected modules.

| Group | Baseline compressed MiB | Candidate compressed MiB | Delta |
|---|---:|---:|---:|
| `ASRTools-runtime.exe` (raw 439.68 → 321.30) | 153.10 | 111.04 | **−42.06** |
| `llvmlite/` | 28.80 | 0.00 | **−28.80** (removed) |
| `jieba/` (31 → 9 files) | 17.25 | 5.75 | **−11.51** |
| `torch/` (8,698 → 12 files, headers removed) | 82.36 | 75.73 | **−6.63** |
| `sklearn/` | 3.45 | 0.00 | **−3.45** (removed) |
| `scipy/` | 23.51 | 22.30 | −1.20 |
| `funasr/` sources (371 → 53 files) | 0.59 | 0.06 | −0.53 |
| `numba/`, `soxr/`, `Crypto/`, `msgpack/` | 0.60 | 0.00 | −0.60 |
| Everything else | ~390 | ~390 | ≤0.1 each |

| Measurement | Baseline | Candidate | Change |
|---|---:|---:|---:|
| Portable ZIP | 737,541,352 B / 703.37 MiB | 635,445,333 B / 606.01 MiB | **−102,096,019 B / −97.37 MiB / −13.84%** |
| Uncompressed members | 1,654,842,428 B / 1,578.18 MiB | 1,382,761,352 B / 1,318.70 MiB | −259.48 MiB |
| Members | 10,191 | 1,030 | −9,161 |
| EXE raw | 461,024,064 B / 439.68 MiB | 336,906,752 B / 321.30 MiB | −118.38 MiB |

The plan's engineering target was at most 650 MiB. The measured candidate is
**606.01 MiB**, 44 MiB below the target. Model weights, FFmpeg, PyTorch CPU,
torchaudio's Kaldi frontend, sherpa-onnx, ONNX Runtime, sentencepiece, Qt and all
nine model/config files are unchanged and their hashes verified.

### 8.3 Validation status

| Gate | Status |
|---|---|
| G0 generation guards (hash/definition/closure/YAML) | passed, `tests/test_funasr_profile.py` |
| G1 original vs generated source equivalence | passed: three fresh processes (baseline twice, candidate once) produced identical model hashes, token IDs, feature shape/dtype/digest, encoder output digest, alphas/peaks digests and final character spans; the candidate process loaded none of `librosa`, `numba`, `llvmlite`, `scipy`, `sklearn`, `jieba`, `funasr.models.campplus` |
| G2 compilation and package inventory | passed for the module-inventory gate; artifact sizes measured above |
| G3 packaged capability and performance | **not run.** No application was launched. The candidate ZIP requires the existing Windows 10+ clean-machine, GUI batch, export, cancellation, media-conversion and timing acceptance before release |

Known deferred items: the copied `*.dist-info` trees for the removed packages
(`librosa`, `numba`, `llvmlite`, `scikit-learn`) and for other unused
distributions are still shipped (~0.1 MiB compressed); that is cargo metadata, not
code, and trimming it needs one mapped consumer list. The duplicate
`asmjit.dll`/`fbgemm.dll` pairs and Qt plugin pruning from section 6 remain
unaddressed. `scipy` and its OpenBLAS DLLs remain compiled in.
