"""Local CPU feasibility probe; not an application engine or accuracy benchmark."""

import argparse
import hashlib
import importlib.metadata
import json
import platform
import socket
import time
import wave
from pathlib import Path
from unittest.mock import patch

import numpy as np
import psutil


def read_wav(path):
    with wave.open(str(path), "rb") as wav:
        if wav.getsampwidth() != 2 or wav.getnchannels() != 1:
            raise ValueError("Probe requires mono PCM16 WAV input")
        rate = wav.getframerate()
        samples = np.frombuffer(wav.readframes(wav.getnframes()), dtype="<i2")
    return samples.astype(np.float32) / 32768.0, rate


def digest(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def no_network(*args, **kwargs):
    raise RuntimeError("Network access is disabled during the local probe")


def run_probe(root, audio, threads):
    import sherpa_onnx

    probe_started = time.perf_counter()
    paths = {
        "asr": root / "sensevoice-small-int8/model.int8.onnx",
        "tokens": root / "sensevoice-small-int8/tokens.txt",
        "vad": root / "silero-vad/silero_vad.onnx",
    }
    samples, rate = read_wav(audio)
    if rate != 16000:
        raise ValueError("VAD probe requires a 16000 Hz WAV")
    duration = len(samples) / rate
    if not duration:
        raise ValueError("Empty audio")
    report = {
        "platform": platform.platform(),
        "cpu": platform.processor(),
        "physical_cores": psutil.cpu_count(logical=False),
        "logical_cores": psutil.cpu_count(),
        "system_memory_bytes": psutil.virtual_memory().total,
        "python": platform.python_version(),
        "dependencies": {name: importlib.metadata.version(name)
                         for name in ("sherpa-onnx", "sherpa-onnx-core", "numpy", "psutil")},
        "provider": "cpu", "threads": threads,
        "python_socket_connections_blocked": True,
        "input": str(audio), "audio_sha256": digest(audio),
        "audio_seconds": duration,
        "models": {name: {"path": str(path), "sha256": digest(path),
                          "bytes": path.stat().st_size} for name, path in paths.items()},
        "runs": [],
    }
    process = psutil.Process()
    for use_itn in (False, True):
        started = time.perf_counter()
        recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=str(paths["asr"]), tokens=str(paths["tokens"]),
            num_threads=threads, provider="cpu", language="zh", use_itn=use_itn,
        )
        load_seconds = time.perf_counter() - started
        for attempt in range(2):
            stream = recognizer.create_stream()
            started = time.perf_counter()
            stream.accept_waveform(rate, samples)
            recognizer.decode_stream(stream)
            elapsed = time.perf_counter() - started
            result = stream.result
            tokens = list(result.tokens)
            timestamps = list(result.timestamps)
            report["runs"].append({
                "itn": use_itn, "attempt": attempt + 1,
                "model_load_seconds": load_seconds,
                "decode_seconds": elapsed, "rtf": elapsed / duration,
                "text": result.text, "tokens": tokens, "timestamps": timestamps,
                "durations": list(getattr(result, "durations", [])),
                "token_timestamp_counts_match": len(tokens) == len(timestamps),
                "timestamps_monotonic": timestamps == sorted(timestamps),
                "timestamps_in_audio": all(0 <= value <= duration for value in timestamps),
                "raw_result": str(result),
            })
            if not result.text.strip() or not tokens or len(tokens) != len(timestamps):
                raise RuntimeError("ASR output lacks text or matching token timestamps")
        del stream, recognizer
    config = sherpa_onnx.VadModelConfig()
    config.silero_vad.model = str(paths["vad"])
    config.sample_rate = rate
    config.num_threads = 1
    config.provider = "cpu"
    config.silero_vad.min_silence_duration = 0.25
    vad = sherpa_onnx.VoiceActivityDetector(config, buffer_size_in_seconds=30)
    started = time.perf_counter()
    window = config.silero_vad.window_size
    for offset in range(0, len(samples), window):
        chunk = samples[offset:offset + window]
        vad.accept_waveform(np.pad(chunk, (0, window - len(chunk))))
    vad.flush()
    spans = []
    while not vad.empty():
        spans.append({"start": vad.front.start / rate,
                      "end": (vad.front.start + len(vad.front.samples)) / rate})
        vad.pop()
    if not spans:
        raise RuntimeError("VAD found no speech in the spoken example")
    report["vad"] = {"seconds": time.perf_counter() - started, "segments": spans}
    memory = process.memory_info()
    report["rss_bytes_at_end"] = memory.rss
    report["peak_working_set_bytes"] = getattr(memory, "peak_wset", None)
    report["total_probe_seconds"] = time.perf_counter() - probe_started
    report["limitations"] = [
        "Short sample only; no representative-video or minimum-spec validation.",
        "No human alignment reference; no character accuracy or timing error measured.",
        "Socket guard alone does not block native network calls; use OS network isolation.",
    ]
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models", type=Path, default=Path("models"))
    parser.add_argument("--audio", type=Path)
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--cpu-name", help="Exact CPU model, verified from system information")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.threads < 1:
        parser.error("--threads must be positive")
    audio = args.audio or args.models / "sensevoice-small-int8/test_wavs/zh.wav"
    with patch.object(socket.socket, "connect", no_network), \
            patch.object(socket.socket, "connect_ex", no_network), \
            patch.object(socket, "create_connection", no_network):
        report = run_probe(args.models, audio, args.threads)
    if args.cpu_name:
        report["cpu_name_user_supplied"] = args.cpu_name
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
