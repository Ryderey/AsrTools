"""CPU-only resident recognition. No online fallback and no GUI dependency."""

import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import wave

from app_runtime import application_resource_dir, resolve_ffmpeg_path
from .ASRData import ASRData, ASRDataSeg
from .offline_alignment import character_intervals, digest, load_aligner, spoken_text

OFFLINE_ENGINE = "本地离线（SenseVoice）"
ONLINE_ENGINE = "B 接口"
RATE = 16000
REVISION = "sensevoice-fa-zh-v2"


def bounded_vad_spans(start, end):
    """Enforce our allocation bound even when VAD exceeds its duration setting."""
    if start < 0 or end <= start:
        raise ValueError("VAD 返回无效语音范围")
    for begin in range(start, end, 12 * RATE):
        yield begin, min(begin + 12 * RATE, end)


def check_cancel(should_stop):
    if should_stop():
        raise InterruptedError("已取消离线识别")


def atomic_text(path, text, should_stop=lambda: False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix="asr-", suffix=".tmp", delete=False) as output:
            temporary = Path(output.name)
            output.write(text)
        check_cancel(should_stop)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class OfflineResult(ASRData):
    def __init__(self, entries):
        self.entries = entries
        self.characters = [c for entry in entries for c in entry["characters"]]
        cues = []
        for entry in entries:
            chars = entry["characters"]
            for offset in range(0, len(chars), 18):
                group = chars[offset:offset + 18]
                end = chars[offset + 18]["display_index"] if offset + 18 < len(chars) else len(entry["text"])
                begin = 0 if offset == 0 else group[0]["display_index"]
                cues.append(ASRDataSeg(entry["text"][begin:end], group[0]["start_ms"], group[-1]["end_ms"]))
        super().__init__(cues)

    def to_txt(self):
        return "\n".join(entry["text"] for entry in self.entries)


class OfflineASR:
    def __init__(self, model_dir=None, threads=4, ffmpeg_path=None, cache_dir=None):
        if threads < 1:
            raise ValueError("threads must be positive")
        self.model_dir = Path(model_dir) if model_dir else application_resource_dir() / "models"
        self.threads = threads
        self.ffmpeg_path = Path(ffmpeg_path) if ffmpeg_path else None
        self.cache_dir = Path(cache_dir) if cache_dir else Path(tempfile.gettempdir()) / "bk_asr" / "offline"
        self._lock = threading.Lock()
        self.recognizer = self.vad = self.aligner = None
        self.identity = None

    def _load(self, progress, should_stop):
        if self.recognizer is not None:
            return
        progress("加载识别模型", 0, 0)
        paths = {name: self.model_dir / relative for name, relative in (
            ("asr", "sensevoice-small-int8/model.int8.onnx"),
            ("tokens", "sensevoice-small-int8/tokens.txt"),
            ("vad", "silero-vad/silero_vad.onnx"))}
        expected = {"asr": "c71f0ce00bec95b07744e116345e33d8cbbe08cef896382cf907bf4b51a2cd51",
                    "tokens": "f449eb28dc567533d7fa59be34e2abca8784f771850c78a47fb731a31429a1dc",
                    "vad": "9e2449e1087496d8d4caba907f23e0bd3f78d91fa552479bb9c23ac09cbb1fd6"}
        for name, path in paths.items():
            check_cancel(should_stop)
            if not path.is_file():
                raise FileNotFoundError(f"缺少离线模型：{path}；请手动放置模型，不会自动上传音频")
            if digest(path, should_stop) != expected[name]:
                raise ValueError(f"离线模型校验失败：{path}")
        try:
            import sherpa_onnx
        except ImportError as exc:
            raise RuntimeError("缺少 sherpa-onnx 离线运行库，请安装离线依赖或使用完整离线应用包") from exc
        recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=str(paths["asr"]), tokens=str(paths["tokens"]), provider="cpu",
            num_threads=self.threads, language="zh", use_itn=True)
        config = sherpa_onnx.VadModelConfig()
        config.silero_vad.model = str(paths["vad"])
        config.silero_vad.min_silence_duration = 0.35
        config.silero_vad.max_speech_duration = 12
        config.sample_rate = RATE
        config.num_threads = 1
        vad = sherpa_onnx.VoiceActivityDetector(config, buffer_size_in_seconds=30)
        self.recognizer, self.vad = recognizer, vad
        self.asr_hashes = expected
        self.window = config.silero_vad.window_size

    def _load_alignment(self, progress, should_stop):
        if self.aligner is not None:
            return
        check_cancel(should_stop)
        progress("加载对齐模型", 0, 0)
        try:
            aligner, hashes, _ = load_aligner(self.model_dir / "fa-zh", self.threads)
        except ImportError as exc:
            raise RuntimeError("缺少 FunASR/PyTorch CPU 对齐运行库，请安装离线依赖") from exc
        self.aligner = aligner
        versions = {name: importlib.metadata.version(name) for name in ("funasr", "torch", "sherpa-onnx")}
        self.identity = {"revision": REVISION, "alignment": hashes, "versions": versions,
                         "threads": self.threads, "asr_models": self.asr_hashes,
                         "language": "zh", "itn": True, "vad_silence": 0.35,
                         "vad_max_speech": 12, "padding_samples": 3200}

    def _convert(self, source, target, should_stop, progress):
        progress("转换音频", 0, 0)
        executable = self.ffmpeg_path or resolve_ffmpeg_path()
        command = [str(executable), "-nostdin", "-hide_banner", "-loglevel", "error", "-n",
                   "-i", str(source), "-vn", "-ac", "1", "-ar", str(RATE), "-c:a", "pcm_s16le", str(target)]
        with tempfile.TemporaryFile() as errors:
            process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=errors,
                                       creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            try:
                while True:
                    check_cancel(should_stop)
                    try:
                        code = process.wait(timeout=0.1)
                        break
                    except subprocess.TimeoutExpired:
                        continue
                if code:
                    raise RuntimeError("FFmpeg 音频转换失败，请检查源文件是否可播放")
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait()

    def transcribe(self, source, *, use_cache=True, should_stop=lambda: False,
                   progress=lambda stage, current, total: None):
        source = Path(source).resolve()
        if not source.is_file():
            raise FileNotFoundError(f"输入文件不存在：{source}")
        # Serial ownership protects native states even for direct API callers.
        while not self._lock.acquire(timeout=0.1):
            check_cancel(should_stop)
        try:
            check_cancel(should_stop)
            self._load(progress, should_stop)
            with tempfile.TemporaryDirectory(prefix="asr-offline-") as temporary:
                audio = Path(temporary) / "audio.wav"
                self._convert(source, audio, should_stop, progress)
                return self._transcribe_wav(audio, use_cache, should_stop, progress)
        finally:
            try:
                if self.vad is not None:
                    self.vad.reset()
            finally:
                self._lock.release()

    def _transcribe_wav(self, audio, use_cache, should_stop, progress):
        import numpy as np
        self.vad.reset()
        entries = []
        pending = None
        previous_end = 0
        with wave.open(str(audio), "rb") as wav, wave.open(str(audio), "rb") as segment_reader:
            total = wav.getnframes()
            if wav.getframerate() != RATE or wav.getnchannels() != 1 or wav.getsampwidth() != 2:
                raise ValueError("离线音频必须是 16 kHz 单声道 PCM16")
            # PCM identity intentionally shares cache between byte-identical audio.
            audio_hash = digest(audio, should_stop)
            def cache_path():
                key = hashlib.sha256((audio_hash + json.dumps(self.identity, sort_keys=True)).encode()).hexdigest()
                return self.cache_dir / f"{key}.json"
            cache = cache_path()
            if use_cache and self.identity is not None and cache.is_file():
                try:
                    data = json.loads(cache.read_text(encoding="utf-8"))
                    if data["identity"] == self.identity:
                        result = OfflineResult(data["entries"])
                        self._validate(result, total / 16)
                        check_cancel(should_stop)
                        progress("读取离线缓存", total / RATE, total / RATE)
                        return result
                except InterruptedError:
                    raise
                except (ValueError, KeyError, TypeError, IndexError, OSError):
                    pass

            def decode(span, next_start=None):
                nonlocal previous_end
                start, end = span
                left = (previous_end + start) // 2
                right = (end + next_start) // 2 if next_start is not None else total
                begin, finish = max(left, start - 3200), min(right, end + 3200, total)
                if not 0 <= begin < finish <= total or finish - begin > 15 * RATE:
                    raise ValueError(f"VAD 分段越界或超出离线内存上限：{start=}, {end=}, {begin=}, {finish=}, {total=}")
                previous_end = end
                check_cancel(should_stop)
                segment_reader.setpos(begin)
                samples = np.frombuffer(segment_reader.readframes(finish - begin), dtype="<i2").astype(np.float32) / 32768
                stream = self.recognizer.create_stream()
                stream.accept_waveform(RATE, samples)
                progress("识别中", begin / RATE, total / RATE)
                self.recognizer.decode_stream(stream)
                check_cancel(should_stop)
                text = stream.result.text
                target, indices = spoken_text(text)
                if not target:
                    return
                self._load_alignment(progress, should_stop)
                check_cancel(should_stop)
                progress("逐字对齐", begin / RATE, total / RATE)
                intervals = character_intervals(self.aligner, samples, target)
                entries.append({"text": text, "characters": [
                    {"text": c, "display_index": index, "start_ms": begin / 16 + pair[0], "end_ms": begin / 16 + pair[1]}
                    for c, index, pair in zip(target, indices, intervals)]})

            def drain():
                nonlocal pending
                while not self.vad.empty():
                    current = self.vad.front
                    span = (current.start, min(total, current.start + len(current.samples)))
                    self.vad.pop()
                    for bounded in bounded_vad_spans(*span):
                        if pending is not None:
                            decode(pending, bounded[0])
                        pending = bounded

            last_scan_second = -1
            while True:
                check_cancel(should_stop)
                raw = wav.readframes(self.window)
                if not raw:
                    break
                scan_second = wav.tell() // RATE
                if scan_second != last_scan_second:
                    progress("扫描音频", wav.tell() / RATE, total / RATE)
                    last_scan_second = scan_second
                samples = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768
                self.vad.accept_waveform(np.pad(samples, (0, self.window - len(samples))))
                drain()
            self.vad.flush()
            drain()
            if pending is not None:
                decode(pending)
        check_cancel(should_stop)
        result = OfflineResult(entries)
        self._validate(result, total / 16)
        if use_cache and self.identity is not None:
            try:
                atomic_text(cache_path(), json.dumps({"identity": self.identity, "entries": entries}, ensure_ascii=False), should_stop)
            except InterruptedError:
                raise
            except OSError:
                pass  # Cache availability must not turn completed recognition into failure.
        progress("识别完成", total / RATE, total / RATE)
        return result

    @staticmethod
    def _validate(result, duration_ms):
        previous = 0
        for entry in result.entries:
            target, indices = spoken_text(entry["text"])
            chars = entry["characters"]
            if [(c["text"], c["display_index"]) for c in chars] != list(zip(target, indices)):
                raise ValueError("离线逐字映射不完整")
            for char in chars:
                if not previous <= char["start_ms"] < char["end_ms"] <= duration_ms:
                    raise ValueError("离线时间轴越界或重叠")
                previous = char["end_ms"]
