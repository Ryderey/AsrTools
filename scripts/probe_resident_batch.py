"""Real-model resident batch regression with one injected alignment failure."""

import argparse
import json
import os
import socket
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import sherpa_onnx

from scripts import probe_video_offline as probe
from scripts.probe_offline_asr import digest, no_network


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--audio-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cpu-name", required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Use a new output directory")
    by_hash = {digest(path): path for path in args.source.glob("*.txt")}
    if not by_hash:
        parser.error("No references found")
    # Match by recorded content hash; PowerShell and Python filename order differ.
    references = [by_hash[json.loads((Path("example/benchmark-2026-09-07") / str(i) /
                                    "report.json").read_text(encoding="utf-8"))["reference_sha256"]]
                  for i in range(1, len(by_hash) + 1)]
    for i, reference in enumerate(references, 1):
        if not (args.audio_dir / f"{i}.wav").is_file():
            parser.error(f"Missing extracted audio for {reference.name}")
    args.output.mkdir(parents=True)
    os.environ["HF_HUB_OFFLINE"] = "1"
    runtime, outcomes = {}, []
    started = time.perf_counter()
    with patch.object(socket.socket, "connect", no_network), \
            patch.object(socket.socket, "connect_ex", no_network), \
            patch.object(socket, "create_connection", no_network), \
            patch.object(sherpa_onnx.OfflineRecognizer, "from_sense_voice",
                         wraps=sherpa_onnx.OfflineRecognizer.from_sense_voice) as asr_load, \
            patch.object(sherpa_onnx, "VoiceActivityDetector",
                         wraps=sherpa_onnx.VoiceActivityDetector) as vad_load, \
            patch.object(probe, "load_aligner", wraps=probe.load_aligner) as align_load:
        for i, reference in enumerate(references, 1):
            options = SimpleNamespace(audio=args.audio_dir / f"{i}.wav", reference=reference,
                                      output=args.output / str(i), models=Path("models"),
                                      threads=4, repeat=1, cancel_file=None, cpu_name=args.cpu_name)
            # Fail an extra transcription between the first and second real files.
            # This exercises an exception after ASR/VAD work, not just a missing path.
            if i == 2:
                failed = SimpleNamespace(**vars(options))
                failed.output = args.output / "injected-failure"
                try:
                    with patch.object(probe, "raw_character_alignment",
                                      side_effect=RuntimeError("Injected alignment failure")):
                        probe.run(failed, runtime)
                except RuntimeError as exc:
                    if str(exc) != "Injected alignment failure":
                        raise
                    assert not failed.output.exists(), "Failed transcription exported results"
                    outcomes.append({"id": "injected-failure", "status": "failed", "error": str(exc)})
                else:
                    raise AssertionError("Injection did not execute")
            print(f"RESIDENT FILE {i}/{len(references)}: {reference.name}", flush=True)
            report = probe.run(options, runtime)
            cold_path = Path("example/benchmark-2026-09-07") / str(i) / "report.json"
            cold = json.loads(cold_path.read_text(encoding="utf-8"))
            for field in ("hypothesis_characters", "edit_distance", "aligned_characters"):
                assert report[field] == cold[field], f"Resident/cold mismatch: {i} {field}"
            boundary_deltas = []
            for left, right in zip(report["segments"], cold["segments"]):
                assert left["text"] == right["text"]
                assert (left["start_sample"], left["end_sample"]) == (right["start_sample"], right["end_sample"]), "VAD offsets leaked between files"
                assert left["alignment_status"] == right["alignment_status"]
                lc, rc = left.get("characters", []), right.get("characters", [])
                assert [(c["text"], c["display_index"]) for c in lc] == [(c["text"], c["display_index"]) for c in rc]
                boundary_deltas.extend(abs(a[key] - b[key]) for a, b in zip(lc, rc)
                                       for key in ("start_ms", "end_ms"))
            assert len(report["segments"]) == len(cold["segments"])
            outcomes.append({"id": i, "source": reference.name, "status": "success",
                             "seconds": report["total_seconds"], "cold_text_and_vad_match": True,
                             "max_boundary_difference_from_cold_ms": max(boundary_deltas, default=0)})
        counts = {"asr": asr_load.call_count, "vad": vad_load.call_count, "aligner": align_load.call_count}
        assert counts == {"asr": 1, "vad": 1, "aligner": 1}, counts
    result = {"model_load_counts": counts, "outcomes": outcomes,
              "wall_seconds_including_injected_failure": time.perf_counter() - started,
              "scope": "Probe batch, not GUI/API integration. Peak memory in per-file reports is process lifetime high-water mark."}
    (args.output / "batch.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
