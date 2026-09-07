import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from API.asr_api import ASRAPI
from bk_asr.ASRData import ASRData, ASRDataSeg


class BatchFailureTests(unittest.TestCase):
    def test_ordinary_failure_preserves_success_and_continues_queue(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = [Path(directory) / f"{i}.wav" for i in range(3)]
            for path in paths:
                path.touch()
            result = ASRData([ASRDataSeg("测试", 0, 1000)])
            with patch("API.asr_api.BcutASR", side_effect=[
                Mock(run=Mock(return_value=result)), RuntimeError("single file failed"),
                Mock(run=Mock(return_value=result)),
            ]) as constructor:
                outcomes = ASRAPI(use_cache=False, max_workers=1, engine="bcut").batch_process(
                    [str(path) for path in paths], output_format="txt")
            self.assertEqual(constructor.call_count, 3)
            self.assertEqual(outcomes["failed"], [str(paths[1])])
            self.assertEqual(outcomes["paused"], [])
            self.assertEqual(outcomes["success"], [str(paths[i].with_suffix(".txt")) for i in (0, 2)])
            self.assertFalse(paths[1].with_suffix(".txt").exists())
            for i in (0, 2):
                self.assertIn("测试", paths[i].with_suffix(".txt").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
