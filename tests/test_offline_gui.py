import os
import json
import importlib.util
import subprocess
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
try:
    import asr_gui
    from PyQt5.QtWidgets import QApplication
except ImportError:
    asr_gui = None


@unittest.skipIf(asr_gui is None, "requires GUI environment")
class OfflineGUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_default_offline_queue_is_single_file(self):
        widget = asr_gui.ASRWidget()
        try:
            self.assertEqual(widget.combo_box.currentText(), asr_gui.OFFLINE_ENGINE)
            self.assertEqual(widget.thread_spinbox.value(), 1)
            self.assertFalse(widget.thread_spinbox.isEnabled())
            widget.max_threads = 3
            widget.processing_queue = ["a.mp4", "b.mp4"]
            def start(path):
                widget.workers[path] = Mock()
            widget.process_file = Mock(side_effect=start)
            widget.process_next_in_queue()
            widget.process_file.assert_called_once_with("a.mp4")
            self.assertEqual(widget.processing_queue, ["b.mp4"])
        finally:
            widget.workers.clear()
            widget.processing_queue.clear()
            widget.close()

    def test_worker_routes_local_without_online_or_sidecar_cleanup(self):
        from bk_asr.OfflineASR import OfflineResult
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "原视频.mp4"
            source.touch()
            unrelated = source.with_suffix(".mp3")
            unrelated.write_bytes(b"user audio")
            engine = Mock()
            engine.transcribe.return_value = OfflineResult([])
            worker = asr_gui.ASRWorker(str(source), asr_gui.OFFLINE_ENGINE, "SRT", offline_engine=engine)
            with patch.object(asr_gui, "BcutASR") as online:
                worker.run()
            engine.transcribe.assert_called_once()
            online.assert_not_called()
            self.assertTrue(source.with_suffix(".srt").exists())
            self.assertEqual(unrelated.read_bytes(), b"user audio")

    def test_old_worker_cannot_unregister_replacement(self):
        first = asr_gui.ASRWorker("same.mp4", asr_gui.OFFLINE_ENGINE, "TXT")
        second = asr_gui.ASRWorker("same.mp4", asr_gui.OFFLINE_ENGINE, "TXT")
        asr_gui.ASRWorker.remove_worker("same.mp4", first)
        self.assertIs(asr_gui.ASRWorker.get_worker("same.mp4"), second)
        asr_gui.ASRWorker.remove_worker("same.mp4", second)

    def test_offline_progress_only_emits_stage_changes(self):
        worker = asr_gui.ASRWorker("stage-test.wav", asr_gui.OFFLINE_ENGINE, "SRT")
        received = []
        worker.signals.progress.connect(lambda *args: received.append(args))
        try:
            worker.report_offline_stage("加载识别模型", 0, 0)
            for i in range(1000):
                for stage in ("扫描音频", "识别中", "逐字对齐"):
                    worker.report_offline_stage(stage, i, 1000)
            self.assertEqual(received, [("stage-test.wav", "加载识别模型", 0, 0),
                                        ("stage-test.wav", "识别中", 0, 0)])
        finally:
            asr_gui.ASRWorker.remove_worker("stage-test.wav", worker)

    def test_release_workflow_explicitly_selects_requested_engine(self):
        for mode, expected in (("workflow", asr_gui.ONLINE_ENGINE),
                               ("offline-workflow", asr_gui.OFFLINE_ENGINE)):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                source = Path(directory) / "sample.wav"
                source.touch()
                report = Path(directory) / "report.json"
                widget = Mock()
                widget.table.item.return_value.text.return_value = "已处理"
                def complete():
                    source.with_suffix(".srt").write_text("verified", encoding="utf-8")
                widget.process_files.side_effect = complete
                with patch.object(asr_gui, "ASRWidget", return_value=widget), \
                        patch.object(asr_gui, "BcutASR") as online:
                    code = asr_gui.run_release_check([
                        "--release-check", mode, "--input", str(source), "--report", str(report)])
                self.assertEqual(code, 0, report.read_text(encoding="utf-8"))
                self.assertEqual(json.loads(report.read_text(encoding="utf-8"))["engine"], expected)
                widget.combo_box.setCurrentText.assert_called_once_with(expected)
                online.assert_not_called()

    @unittest.skipUnless(importlib.util.find_spec("sentencepiece"), "requires combined GUI/offline environment")
    def test_native_import_order_finishes_in_fresh_process(self):
        code = ("import sys,site;sys.path=" + repr(sys.path)
                + ";[site.addsitedir(p) for p in sys.path.copy() if p.endswith('site-packages')]"
                + ";import asr_gui;import sentencepiece;print('native-ready')")
        result = subprocess.run([sys._base_executable, "-c", code], capture_output=True,
                                text=True, encoding="utf-8", timeout=15,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("native-ready", result.stdout)


if __name__ == "__main__":
    unittest.main()
