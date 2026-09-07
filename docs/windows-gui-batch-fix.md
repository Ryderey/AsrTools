# Windows selected-file batch crash — 2026-09-07

The previous API and short-WAV checks did not cover the actual five-video GUI
workflow. Their passing results were insufficient to establish GUI batch stability.

## Reproduction and isolation

`scripts/check_gui_batch.py --videos` creates a real MainWindow, copies five
benchmark videos into an owned temporary directory, checks their rows and clicks
the selected-file processing button. It runs QApplication.exec(), retains normal
completion/error notices, verifies exports and window survival, then closes.
Run with `.venv-alignment/Scripts/python.exe -X faulthandler` and
`QT_QPA_PLATFORM=windows`. The diagnostic process suppresses Windows error dialogs
but retains fatal tracebacks and nonzero exit codes; production does not suppress them.

The original QThreadPool path repeatedly terminated at the first file transition
with Windows fatal exception `0xc0000374` (heap corruption). Removing InfoBars and
retaining QRunnables with auto-deletion disabled did not prevent the crash.
Using one persistent Python ThreadPoolExecutor worker allowed all remaining files
to run after an ordinary first-file error. This isolates the affected execution
path; the exact underlying native DLL corruption mechanism is not established.

## Changes

- Offline GUI jobs use a persistent single Python worker; model ownership and Qt
  signals are retained. Window close cancels and joins native work. Online work
  retains its existing Qt pool.
- A real VAD segment was 252256 samples (15.766 seconds) despite the 12-second
  setting. Explicitly subdivide oversized segments while preserving coverage;
  bounded PCM allocation no longer rejects this valid input.
- Splitting exposed fa-zh frame padding: one 12085 ms segment returned a final
  endpoint of 12120 ms. Clip only the final endpoint within one configured 60 ms
  LFR frame; invalid starts, larger overruns and overlaps still fail validation.
  This does not establish human-measured timing accuracy. Cache revision is v2.
- VAD reset failures cannot leave the engine ownership lock held.

## Verification

The repaired visible Windows run completed all five real videos, exported five
SRTs, kept the window visible after completion and exited with code 0 after closing.
The 57 application tests passed, including long-segment coverage and bounded tail
padding regression checks. Failure-continuation and model-residency verification
use the same command with `--inject-failure`. That visible Windows run also passed:
statuses were success/error/success/success/success, four SRTs were exported,
the thread/model identity sets each contained exactly one entry, the window
remained visible and the process exited 0. The nine probe tests also passed.

Source fix only; no new portable binary has been delivered by this change.
