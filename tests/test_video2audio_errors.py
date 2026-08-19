import unittest

try:
    import asr_gui
except ImportError:
    asr_gui = None


@unittest.skipUnless(asr_gui is not None, "requires PyQt5 environment")
class ConvertFailureMessageTests(unittest.TestCase):
    def test_no_audio_stream_gets_actionable_message(self):
        stderr = (
            "Output #0, mp3, to '/home/ryl/tmp/1.mp3':\n"
            "[out#0/mp3 @ 0x6081e079a7c0] Output file does not contain any stream\n"
            "Error opening output file /home/ryl/tmp/1.mp3.\n"
            "Error opening output files: Invalid argument\n"
        )
        message = asr_gui.convert_failure_message(stderr, 1, "/home/ryl/tmp/1.mp4")
        self.assertIn("不含音频流", message)
        self.assertIn("/home/ryl/tmp/1.mp4", message)

    def test_generic_failure_keeps_last_stderr_line(self):
        message = asr_gui.convert_failure_message("boom\n磁盘已满", 1, "/a.mp4")
        self.assertIn("磁盘已满", message)
        self.assertIn("原目录可写", message)

    def test_empty_stderr_falls_back_to_returncode(self):
        message = asr_gui.convert_failure_message("", 137, "/a.mp4")
        self.assertIn("137", message)


if __name__ == "__main__":
    unittest.main()
