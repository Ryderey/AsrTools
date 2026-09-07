import unittest
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

from scripts.probe_alignment import check_spans, raw_character_alignment


class AlignmentSpanTests(unittest.TestCase):
    def test_raw_letter_intervals_survive_abbreviation_merge(self):
        original = lambda tokens, timestamps: ("IDC", [[0, 60]], ["IDC"])
        postprocess = SimpleNamespace(sentence_postprocess=original)
        utils = ModuleType("funasr.utils")
        utils.postprocess_utils = postprocess
        package = ModuleType("funasr")
        package.utils = utils

        class Model:
            def generate(self, **kwargs):
                return postprocess.sentence_postprocess(["i", "d", "c"], [[0, 20], [20, 40], [40, 60]])

        with patch.dict(sys.modules, {"funasr": package, "funasr.utils": utils}):
            result, tokens, spans = raw_character_alignment(Model(), [], "IDC")
        self.assertEqual(result[1], [[0, 60]])
        self.assertEqual(tokens, ["i", "d", "c"])
        self.assertEqual(spans, [[0, 20], [20, 40], [40, 60]])
        self.assertIs(postprocess.sentence_postprocess, original)

    def test_only_ordered_nonempty_in_range_intervals_pass(self):
        self.assertTrue(check_spans([[10, 20], [20, 35]], 40))
        for spans in ([], [[-1, 20]], [[20, 20]], [[10, 41]],
                      [[10, 30], [20, 35]], [[10]], [[10, float("nan")]]):
            with self.subTest(spans=spans):
                self.assertFalse(check_spans(spans, 40))


if __name__ == "__main__":
    unittest.main()
