"""Local fa-zh CPU timing experiment, not a character-accuracy benchmark."""

import argparse
import importlib.metadata
import json
import os
import platform
import socket
import sysconfig
import time
from pathlib import Path
from unittest.mock import patch

import psutil

from scripts.probe_offline_asr import digest, no_network, read_wav
from bk_asr.offline_alignment import check_spans, load_aligner


def raw_character_alignment(model, samples, text):
    """Single-threaded probe instrumentation before FunASR merges abbreviations."""
    from funasr.utils import postprocess_utils
    original = postprocess_utils.sentence_postprocess
    captured = []

    def record(tokens, timestamps=None):
        captured.append((list(tokens), [list(pair) for pair in timestamps or []]))
        return original(tokens, timestamps)

    # Probe-only, synchronous scope: never use this global hook for concurrent jobs.
    with patch.object(postprocess_utils, "sentence_postprocess", record):
        result = model.generate(input=(samples, text), data_type=("sound", "text"))
    if len(captured) != 1:
        raise RuntimeError("Expected one raw alignment callback for one segment")
    return result, captured[0][0], captured[0][1]


def run_probe(args):
    started = time.perf_counter()
    dependencies = {d.metadata["Name"]: d.version for d in importlib.metadata.distributions(
        path=[sysconfig.get_paths()["purelib"]])}
    import torch
    import funasr
    import_seconds = time.perf_counter() - started
    samples, rate = read_wav(args.audio)
    if rate != 16000 or not len(samples):
        raise ValueError("Alignment probe requires nonempty 16 kHz mono PCM16 WAV")
    model, hashes, load_seconds = load_aligner(args.model, args.threads)
    tokenizer = model.kwargs["tokenizer"]
    cases = [("spoken_numerals", "开饭时间早上九点至下午五点"),
             ("digits", "开饭时间早上9点至下午5点"),
             ("digits_punctuation", "开饭时间早上9点至下午5点。")]
    runs = []
    for name, text in cases:
        for attempt in range(2):
            tokens = tokenizer.ids2tokens(tokenizer.encode(text))
            before = time.perf_counter()
            with torch.inference_mode():
                result = model.generate(input=(samples, text), data_type=("sound", "text"))
            elapsed = time.perf_counter() - before
            if not result or not result[0].get("timestamp"):
                raise RuntimeError(f"No timestamp result for {name}")
            spans = result[0]["timestamp"]
            runs.append({"case": name, "attempt": attempt + 1, "input_text": text,
                         "tokens": tokens, "unknown_tokens": tokens.count("<unk>"),
                         "seconds": elapsed, "rtf": elapsed / (len(samples) / rate),
                         "result": result,
                         "input_token_count_matches_spans": len(tokens) == len(spans),
                         "spans_ordered_inside_audio": check_spans(spans, len(samples) / rate * 1000)})
    memory = psutil.Process().memory_info()
    return {"cpu": args.cpu_name or platform.processor(), "platform": platform.platform(),
            "python": platform.python_version(), "threads": args.threads, "provider": "cpu",
            "system_memory_bytes": psutil.virtual_memory().total,
            "dependencies": dependencies, "import_seconds": import_seconds,
            "model_hashes": hashes, "audio_sha256": digest(args.audio),
            "audio_seconds": len(samples) / rate, "model_load_seconds": load_seconds,
            "total_probe_seconds": time.perf_counter() - started,
            "peak_working_set_bytes": getattr(memory, "peak_wset", None),
            "runs": runs, "python_socket_guard": True,
            "limitations": ["Short example only; no human-labelled boundary accuracy measured.",
                            "Not minimum-spec hardware or long-video validation.",
                            "Socket guard does not replace OS-level network isolation.",
                            "Fixed transcripts match the supplied zh.wav, not arbitrary audio."]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, default=Path("models/fa-zh"))
    parser.add_argument("--audio", type=Path, default=Path("models/sensevoice-small-int8/test_wavs/zh.wav"))
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--cpu-name")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.threads < 1:
        parser.error("--threads must be positive")
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["HF_DATASETS_OFFLINE"] = "1"
    with patch.object(socket.socket, "connect", no_network), \
            patch.object(socket.socket, "connect_ex", no_network), \
            patch.object(socket, "create_connection", no_network):
        report = run_probe(args)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
