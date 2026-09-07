import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
import wave

from API.asr_api import ASRAPI
from bk_asr.OfflineASR import OfflineASR, OfflineResult, atomic_text
from bk_asr.offline_alignment import digest, spoken_text


def entry(text="中文"):
    return {"text": text, "characters": [
        {"text": c, "display_index": i, "start_ms": i * 100, "end_ms": (i + 1) * 100}
        for i, c in enumerate(text)]}


class OfflineContractsTests(unittest.TestCase):
    def test_long_vad_is_bounded_without_gaps_and_padding_is_limited(self):
        from bk_asr.OfflineASR import bounded_vad_spans, RATE
        from bk_asr.offline_alignment import trim_padded_tail, check_spans
        spans = list(bounded_vad_spans(1366624, 1618880))
        self.assertEqual(spans[0][0], 1366624)
        self.assertEqual(spans[-1][1], 1618880)
        self.assertTrue(all(b - a <= 12 * RATE for a, b in spans))
        self.assertTrue(all(a[1] == b[0] for a, b in zip(spans, spans[1:])))
        self.assertEqual(trim_padded_tail([[11910, 12120]], 12085), [[11910, 12085]])
        self.assertFalse(check_spans(trim_padded_tail([[11910, 12200]], 12085), 12085))
        self.assertFalse(check_spans(trim_padded_tail([[12100, 12120]], 12085), 12085))

    def test_cancelled_hash_and_source_overwrite_guard(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.wav"
            source.touch()
            with self.assertRaises(InterruptedError):
                digest(source, lambda: True)
            api = ASRAPI()
            with patch.object(api.offline, "transcribe") as local:
                self.assertIsNone(api.process_file(str(source), output_path=str(source)))
                local.assert_not_called()

    def test_default_local_failure_never_constructs_online_engine(self):
        with tempfile.TemporaryDirectory() as directory, patch("API.asr_api.BcutASR") as online:
            source = Path(directory) / "source.wav"
            source.touch()
            api = ASRAPI(model_dir=Path(directory) / "missing")
            self.assertEqual(api.engine, "offline")
            self.assertIsNone(api.process_file(str(source)))
            online.assert_not_called()

    def test_local_batch_continues_and_reuses_owner(self):
        with tempfile.TemporaryDirectory() as directory, patch("API.asr_api.BcutASR") as online:
            paths = [Path(directory) / f"{i}.wav" for i in range(3)]
            for path in paths:
                path.touch()
            api = ASRAPI(max_workers=3)
            owner = api.offline
            with patch.object(owner, "transcribe", side_effect=[OfflineResult([entry()]),
                             RuntimeError("bad file"), OfflineResult([entry()])]) as local:
                result = api.batch_process([str(p) for p in paths], output_format="txt")
            self.assertEqual(local.call_count, 3)
            self.assertIs(api.offline, owner)
            self.assertEqual(result["failed"], [str(paths[1])])
            self.assertEqual(len(result["success"]), 2)
            self.assertEqual(result["paused"], [])
            online.assert_not_called()

    def test_cancelled_atomic_export_preserves_existing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "字幕.txt"
            atomic_text(output, "original")
            with self.assertRaises(InterruptedError):
                atomic_text(output, "cancelled", lambda: True)
            self.assertEqual(output.read_text(encoding="utf-8"), "original")
            self.assertEqual(list(Path(directory).iterdir()), [output])

    def test_text_and_character_mapping_export(self):
        record = entry()
        record["text"] = "中文。"
        result = OfflineResult([record])
        OfflineASR._validate(result, 200)
        self.assertEqual(result.to_txt(), "中文。")
        self.assertIn("中文。", result.to_srt())
        self.assertEqual(len(result.characters), 2)
        self.assertEqual(spoken_text("早上9点，IDC。"), ("早上9点IDC", [0, 1, 2, 3, 5, 6, 7]))
        record["characters"][1]["start_ms"] = 50
        with self.assertRaises(ValueError):
            OfflineASR._validate(result, 200)

    def test_cancelled_owner_releases_lock_and_resets_vad(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "test.wav"
            source.touch()
            engine = OfflineASR()
            engine.vad = Mock()
            with self.assertRaises(InterruptedError):
                engine.transcribe(source, should_stop=lambda: True)
            engine.vad.reset.assert_called_once()
            self.assertTrue(engine._lock.acquire(blocking=False))
            engine._lock.release()


@unittest.skipUnless(importlib.util.find_spec("numpy"), "requires offline test environment")
class BoundedAudioTests(unittest.TestCase):
    def test_silence_skips_alignment_and_reads_bounded_windows(self):
        with tempfile.TemporaryDirectory() as directory:
            audio = Path(directory) / "静音.wav"
            with wave.open(str(audio), "wb") as wav:
                wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
                wav.writeframes(b"\0\0" * 16000)
            engine = OfflineASR(cache_dir=Path(directory) / "cache")
            engine.window = 512
            engine.vad = Mock(empty=Mock(return_value=True))
            with patch.object(engine, "_load_alignment") as align:
                result = engine._transcribe_wav(audio, False, lambda: False, lambda *args: None)
            self.assertEqual(result.to_txt(), "")
            align.assert_not_called()
            self.assertTrue(all(len(call.args[0]) == 512 for call in engine.vad.accept_waveform.call_args_list))

    def test_failed_alignment_releases_owner_for_next_job(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.wav"
            source.touch()
            engine = OfflineASR()
            engine.vad = Mock()
            with patch.object(engine, "_load"), patch.object(engine, "_convert"), \
                    patch.object(engine, "_transcribe_wav", side_effect=[ValueError("bad alignment"), OfflineResult([])]):
                with self.assertRaises(ValueError):
                    engine.transcribe(source)
                self.assertEqual(engine.transcribe(source).to_txt(), "")
            self.assertEqual(engine.vad.reset.call_count, 2)


if __name__ == "__main__":
    unittest.main()
