import unittest
import tempfile
from pathlib import Path
from types import SimpleNamespace

from scripts.probe_video_offline import check_cancel, edit_distance, run, spoken_text


class VideoProbeTests(unittest.TestCase):
    def test_missing_models_fail_before_audio_or_native_inference(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(FileNotFoundError):
                run(SimpleNamespace(models=Path(directory), threads=4))

    def test_cancel_marker_stops_processing(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "cancel"
            check_cancel(marker)
            marker.touch()
            with self.assertRaises(InterruptedError):
                check_cancel(marker)

    def test_mapping_preserves_original_display_indices(self):
        text, indices = spoken_text("早上9点，下午5点。")
        self.assertEqual(text, "早上9点下午5点")
        self.assertEqual(indices, [0, 1, 2, 3, 5, 6, 7, 8])

    def test_distance_counts_substitution_insertion_deletion(self):
        for reference, hypothesis, expected in [
            ("", "甲", 1), ("甲乙", "", 2), ("甲乙", "甲乙", 0),
            ("甲乙丙", "甲丁丙", 1), ("kitten", "sitting", 3),
        ]:
            self.assertEqual(edit_distance(reference, hypothesis), expected)


if __name__ == "__main__":
    unittest.main()
