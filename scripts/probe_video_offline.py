"""Bounded-segment offline experiment; not a production engine or accuracy guarantee."""

import argparse
import difflib
import importlib.metadata
import json
import os
import platform
import socket
import sysconfig
import time
import unicodedata
from pathlib import Path
from unittest.mock import patch

import numpy as np
import psutil

from scripts.probe_alignment import check_spans, load_aligner, raw_character_alignment
from scripts.probe_offline_asr import digest, no_network, read_wav
from bk_asr.ASRData import ASRData, ASRDataSeg
from bk_asr.offline_alignment import spoken_text


def edit_distance(reference, hypothesis):
    previous = list(range(len(hypothesis) + 1))
    for i, a in enumerate(reference, 1):
        row = [i]
        for j, b in enumerate(hypothesis, 1):
            row.append(min(row[-1] + 1, previous[j] + 1,
                           previous[j - 1] + (a != b)))
        previous = row
    return previous[-1]


def check_cancel(path):
    if path is not None and path.exists():
        raise InterruptedError("Cancelled at a processing boundary")


def run(args, runtime=None):
    # ponytail: one synchronous probe batch owns this dict; no concurrent callers.
    runtime = {} if runtime is None else runtime
    started = time.perf_counter()
    cpu_started = time.process_time()
    dependencies = {d.metadata["Name"]: d.version for d in importlib.metadata.distributions(
        path=[sysconfig.get_paths()["purelib"]])}
    model_paths = {name: args.models / relative for name, relative in (
        ("asr", "sensevoice-small-int8/model.int8.onnx"),
        ("tokens", "sensevoice-small-int8/tokens.txt"),
        ("vad", "silero-vad/silero_vad.onnx"))}
    # Read before native construction so missing files produce a Python error.
    identity = (str(args.models.resolve()), args.threads)
    if runtime and runtime.get("identity") != identity:
        raise ValueError("Cannot change models/threads within a resident probe batch")
    model_hashes = runtime.get("model_hashes") or {name: digest(path) for name, path in model_paths.items()}
    import sherpa_onnx
    samples, rate = read_wav(args.audio)
    if rate != 16000 or not len(samples):
        raise ValueError("Expected nonempty 16 kHz mono PCM16 audio")
    original_seconds = len(samples) / rate
    if args.repeat > 1:
        samples = np.tile(samples, args.repeat)
    check_cancel(args.cancel_file)
    print("Loading local ASR/VAD", flush=True)
    loading = time.perf_counter()
    reused = "recognizer" in runtime
    if not reused:
        recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=str(model_paths["asr"]), tokens=str(model_paths["tokens"]),
            provider="cpu", num_threads=args.threads, language="zh", use_itn=True)
        config = sherpa_onnx.VadModelConfig()
        config.silero_vad.model = str(model_paths["vad"])
        config.silero_vad.min_silence_duration = 0.35
        config.silero_vad.max_speech_duration = 12
        config.sample_rate = rate
        config.num_threads = 1
        vad = sherpa_onnx.VoiceActivityDetector(config, buffer_size_in_seconds=30)
        runtime.update(identity=identity, model_hashes=model_hashes,
                       recognizer=recognizer, vad=vad, config=config)
    recognizer, vad, config = runtime["recognizer"], runtime["vad"], runtime["config"]
    vad.reset()
    asr_load_seconds = time.perf_counter() - loading
    vad_started = time.perf_counter()
    spans = []
    window = config.silero_vad.window_size
    for offset in range(0, len(samples), window):
        check_cancel(args.cancel_file)
        chunk = samples[offset:offset + window]
        vad.accept_waveform(np.pad(chunk, (0, window - len(chunk))))
        while not vad.empty():
            spans.append((vad.front.start, vad.front.start + len(vad.front.samples)))
            vad.pop()
    vad.flush()
    while not vad.empty():
        spans.append((vad.front.start, min(len(samples), vad.front.start + len(vad.front.samples))))
        vad.pop()
    vad_seconds = time.perf_counter() - vad_started
    segments = []
    first_text_seconds = None
    for i, (start, end) in enumerate(spans):
        check_cancel(args.cancel_file)
        left = (spans[i - 1][1] + start) // 2 if i else 0
        right = (end + spans[i + 1][0]) // 2 if i + 1 < len(spans) else len(samples)
        start, end = max(left, start - 3200), min(right, end + 3200)
        stream = recognizer.create_stream()
        before = time.perf_counter()
        stream.accept_waveform(rate, samples[start:end])
        recognizer.decode_stream(stream)
        segments.append({"start_sample": start, "end_sample": end,
                         "text": stream.result.text, "asr_seconds": time.perf_counter() - before})
        if first_text_seconds is None and stream.result.text.strip():
            first_text_seconds = time.perf_counter() - started
        print(f"ASR {i + 1}/{len(spans)}: {stream.result.text}", flush=True)
    hypothesis = "\n".join(s["text"] for s in segments)
    reference = args.reference.read_text(encoding="utf-8") * args.repeat
    ref, _ = spoken_text(unicodedata.normalize("NFKC", reference).casefold())
    hyp, _ = spoken_text(unicodedata.normalize("NFKC", hypothesis).casefold())
    if not ref:
        raise ValueError("Empty reference after normalization")
    distance = edit_distance(ref, hyp)
    changes = [{"operation": op, "reference": ref[a:b], "offline": hyp[c:d]}
               for op, a, b, c, d in difflib.SequenceMatcher(None, ref, hyp, autojunk=False).get_opcodes()
               if op != "equal"]
    check_cancel(args.cancel_file)
    before = time.perf_counter()
    aligner_hashes, weight_load_seconds = {}, 0.0
    aligner = None
    if segments:
        if "aligner" not in runtime:
            print("Loading local aligner (imports may take time)", flush=True)
            aligner, aligner_hashes, weight_load_seconds = load_aligner(args.models / "fa-zh", args.threads)
            runtime.update(aligner=aligner, aligner_hashes=aligner_hashes)
        aligner, aligner_hashes = runtime["aligner"], runtime["aligner_hashes"]
    alignment_startup_seconds = time.perf_counter() - before if segments else 0.0
    for i, segment in enumerate(segments):
        check_cancel(args.cancel_file)
        target, indices = spoken_text(segment["text"])
        tokens = aligner.kwargs["tokenizer"].ids2tokens(aligner.kwargs["tokenizer"].encode(target)) if target else []
        segment.update(alignment_text=target, display_indices=indices, alignment_tokens=tokens)
        if not target or "<unk>" in tokens or tokens != [c.lower() for c in target]:
            segment["alignment_status"] = "unmapped_tokens"
            continue
        begin, end = segment["start_sample"], segment["end_sample"]
        before = time.perf_counter()
        result, raw_tokens, intervals = raw_character_alignment(aligner, samples[begin:end], target)
        segment["alignment_seconds"] = time.perf_counter() - before
        valid = (raw_tokens == tokens and len(intervals) == len(target)
                 and check_spans(intervals, (end - begin) / rate * 1000))
        segment["alignment_status"] = "structurally_valid" if valid else "invalid_intervals"
        segment["raw_alignment"] = result
        segment["pre_postprocess_intervals"] = intervals
        if valid:
            segment["characters"] = [{"text": c, "display_index": index,
                                      "start_ms": begin / rate * 1000 + pair[0],
                                      "end_ms": begin / rate * 1000 + pair[1]}
                                     for c, index, pair in zip(target, indices, intervals)]
        print(f"Alignment {i + 1}/{len(segments)}: {segment['alignment_status']}", flush=True)
    report = {"audio_seconds": len(samples) / rate, "audio_sha256": digest(args.audio),
              "original_audio_seconds": original_seconds, "synthetic_repeat": args.repeat,
              "reference_sha256": digest(args.reference), "cpu": args.cpu_name or platform.processor(),
              "threads": args.threads, "dependencies": dependencies,
              "resident_asr_reused": reused,
              "model_hashes": model_hashes, "python": platform.python_version(),
              "platform": platform.platform(), "system_memory_bytes": psutil.virtual_memory().total,
              "asr_load_seconds": asr_load_seconds, "vad_seconds": vad_seconds,
              "first_text_seconds": first_text_seconds,
              "alignment_startup_seconds": alignment_startup_seconds,
              "alignment_weight_load_seconds": weight_load_seconds,
              "total_seconds": time.perf_counter() - started,
              "peak_working_set_bytes": psutil.Process().memory_info().peak_wset,
              "aligner_hashes": aligner_hashes, "segments": segments,
              "reference_characters": len(ref), "hypothesis_characters": len(hyp),
              "edit_distance": distance, "cer": distance / len(ref), "differences": changes,
              "normalization": "NFKC, casefold, remove punctuation/whitespace; no homophone or number rewriting",
              "limitations": ["No human boundary labels: timing accuracy/drift unmeasured.",
                              "Full WAV held in memory; only inference chunks bounded.",
                              "CPU host is not the specified minimum-spec machine."]}
    cpu_seconds = time.process_time() - cpu_started
    logical_cpus = psutil.cpu_count() or 1
    report["resources"] = {
        "process_cpu_seconds": cpu_seconds,
        "average_cpu_percent_one_core": cpu_seconds / report["total_seconds"] * 100,
        "average_cpu_percent_machine": cpu_seconds / report["total_seconds"] / logical_cpus * 100,
        "logical_cpus": logical_cpus,
        "physical_cores": psutil.cpu_count(logical=False),
        "rss_bytes_at_end": psutil.Process().memory_info().rss,
        "measurement_scope": "Current process from run entry through inference; excludes extraction/export. CPU average, not sampled peak.",
    }
    check_cancel(args.cancel_file)
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "offline.txt").write_text(hypothesis, encoding="utf-8")
    ASRData([ASRDataSeg(s["text"], s["start_sample"] / rate * 1000,
                       s["end_sample"] / rate * 1000) for s in segments]).to_srt(
                           str(args.output / "offline.sentences.srt"))
    characters = [c for s in segments for c in s.get("characters", [])]
    report["aligned_characters"] = len(characters)
    report["alignment_target_characters"] = sum(len(s["alignment_text"]) for s in segments)
    if characters:
        complete = len(characters) == report["alignment_target_characters"]
        filename = "offline.characters.srt" if complete else "offline.characters.partial.srt"
        ASRData([ASRDataSeg(c["text"], c["start_ms"], c["end_ms"]) for c in characters]).to_srt(
            str(args.output / filename))
    review = []
    for segment in segments:
        chars = segment.get("characters", [])
        for offset in range(0, len(chars), 18):
            group = chars[offset:offset + 18]
            end_index = chars[offset + 18]["display_index"] if offset + 18 < len(chars) else len(segment["text"])
            review.append(ASRDataSeg(segment["text"][group[0]["display_index"]:end_index],
                                     group[0]["start_ms"], group[-1]["end_ms"]))
    if review:
        suffix = "" if len(characters) == report["alignment_target_characters"] else ".partial"
        subtitles = ASRData(review)
        subtitles.to_srt(str(args.output / f"offline.review{suffix}.srt"))
        subtitles.to_ass(save_path=str(args.output / f"offline.review{suffix}.ass"))
    (args.output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audio", type=Path, required=True)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--models", type=Path, default=Path("models"))
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--cpu-name")
    parser.add_argument("--repeat", type=int, default=1, help="Synthetic repetition stress test, 1-10")
    parser.add_argument("--cancel-file", type=Path, help="Stop at next boundary when this file exists")
    args = parser.parse_args()
    if args.threads < 1:
        parser.error("--threads must be positive")
    if not 1 <= args.repeat <= 10:
        parser.error("--repeat must be between 1 and 10")
    if args.output.exists() and (not args.output.is_dir() or any(args.output.iterdir())):
        parser.error("Output exists; choose a new experiment directory")
    os.environ["HF_HUB_OFFLINE"] = "1"
    try:
        check_cancel(args.cancel_file)
        with patch.object(socket.socket, "connect", no_network), \
                patch.object(socket.socket, "connect_ex", no_network), \
                patch.object(socket, "create_connection", no_network):
            report = run(args)
    except (KeyboardInterrupt, InterruptedError):
        print("Cancelled; no finished result exported", flush=True)
        raise SystemExit(130)
    print(json.dumps({k: report[k] for k in ("audio_seconds", "cer", "total_seconds", "peak_working_set_bytes")}))


if __name__ == "__main__":
    main()
