"""Probe input checks, isolated from the production dependency test suite."""

import tempfile
import unittest
import wave
from pathlib import Path

from scripts.probe_offline_asr import digest, no_network, read_wav


class OfflineProbeInputTests(unittest.TestCase):
    def test_pcm16_normalization_and_unicode_path(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "中文样例.wav"
            with wave.open(str(path), "wb") as wav:
                wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
                wav.writeframes(b"\x00\x80\x00\x00\xff\x7f")
            samples, rate = read_wav(path)
            self.assertEqual(rate, 16000)
            self.assertEqual(samples.tolist(), [-1.0, 0.0, 32767 / 32768])
            self.assertEqual(len(digest(path)), 64)

    def test_stereo_input_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stereo.wav"
            with wave.open(str(path), "wb") as wav:
                wav.setparams((2, 2, 16000, 0, "NONE", "not compressed"))
                wav.writeframes(b"\x00" * 4)
            with self.assertRaisesRegex(ValueError, "mono PCM16"):
                read_wav(path)

    def test_network_guard_rejects_connection(self):
        with self.assertRaisesRegex(RuntimeError, "Network access is disabled"):
            no_network(("example.com", 443))


if __name__ == "__main__":
    unittest.main()
