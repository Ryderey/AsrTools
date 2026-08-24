import os
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import Mock, patch

import API.asr_api as api_module
from API.asr_api import ASRAPI
from bk_asr.BcutASR import BcutPollingTimeoutError, BcutRateLimitedError

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    import asr_gui
    from PyQt5.QtWidgets import QApplication
except ImportError:
    asr_gui = None
    QApplication = None


class APIConcurrencyLimitTests(unittest.TestCase):
    def test_accepts_one_to_three_workers(self):
        for workers in (1, 2, 3):
            with self.subTest(workers=workers):
                self.assertEqual(ASRAPI(max_workers=workers).max_workers, workers)

    def test_rejects_worker_counts_outside_one_to_three(self):
        for workers in (0, 4, 10):
            with self.subTest(workers=workers):
                with self.assertRaisesRegex(ValueError, "1.*3"):
                    ASRAPI(max_workers=workers)

    def test_cli_rejects_worker_count_above_three(self):
        script = os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "API", "asr_api.py"
        )
        result = subprocess.run(
            [sys.executable, script, "missing.mp3", "--workers", "4"],
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("invalid choice", result.stderr)

    def test_batch_stops_bounded_submission_and_reports_paused_inputs(self):
        api = ASRAPI(max_workers=3)
        barrier = threading.Barrier(3)
        limited = threading.Event()
        calls = []

        def process(input_path, *_args):
            calls.append(input_path)
            barrier.wait(timeout=2)
            if input_path == "input-0.mp3":
                limited.set()
                raise BcutRateLimitedError(30)
            limited.wait(timeout=2)
            time.sleep(0.05)
            return f"{input_path}.srt"

        api._process_single_with_output = Mock(side_effect=process)
        inputs = [f"input-{index}.mp3" for index in range(50)]

        results = api.batch_process(inputs)

        self.assertEqual(set(calls), set(inputs[:3]))
        self.assertEqual(results["success"], [
            "input-1.mp3.srt",
            "input-2.mp3.srt",
        ])
        self.assertEqual(results["failed"], [])
        self.assertEqual(results["paused"], [inputs[0], *inputs[3:]])

    def test_single_worker_batch_stops_after_first_rate_limit(self):
        api = ASRAPI(max_workers=1)
        inputs = [f"input-{index}.mp3" for index in range(5)]
        api._process_single_with_output = Mock(
            side_effect=BcutRateLimitedError(30)
        )

        results = api.batch_process(inputs)

        api._process_single_with_output.assert_called_once()
        self.assertEqual(results["success"], [])
        self.assertEqual(results["failed"], [])
        self.assertEqual(results["paused"], inputs)

    def test_single_file_api_keeps_none_on_rate_limit(self):
        api = ASRAPI()
        with patch("API.asr_api.os.path.exists", return_value=True), patch(
            "API.asr_api.BcutASR"
        ) as asr_class:
            asr_class.return_value.run.side_effect = BcutRateLimitedError(30)
            self.assertIsNone(api.process_file("input.mp3"))

    def test_batch_convenience_api_forwards_output_dir(self):
        with patch.object(api_module, "ASRAPI") as api_class:
            api_class.return_value.batch_process.return_value = {
                "success": [],
                "failed": [],
                "paused": [],
            }

            result = api_module.batch_process(
                ["input.mp3"],
                output_format="txt",
                output_dir="subtitles",
                max_workers=2,
            )

        api_class.assert_called_once_with(max_workers=2)
        api_class.return_value.batch_process.assert_called_once_with(
            ["input.mp3"], "txt", "subtitles"
        )
        self.assertEqual(result["paused"], [])

    def test_directory_convenience_api_forwards_batch_options(self):
        with patch.object(api_module, "ASRAPI") as api_class:
            api_class.return_value.process_directory.return_value = {
                "success": [],
                "failed": [],
                "paused": [],
            }

            api_module.process_directory(
                "videos",
                output_format="ass",
                output_dir="subtitles",
                recursive=True,
                use_cache=False,
            )

        api_class.assert_called_once_with(use_cache=False)
        api_class.return_value.process_directory.assert_called_once_with(
            "videos", "ass", "subtitles", True
        )


@unittest.skipUnless(asr_gui is not None, "requires PyQt5 environment")
class GUIConcurrencyLimitTests(unittest.TestCase):
    def test_historical_thread_settings_are_clamped_to_one_to_three(self):
        self.assertEqual(asr_gui.clamp_thread_count(0), 1)
        self.assertEqual(asr_gui.clamp_thread_count(2), 2)
        self.assertEqual(asr_gui.clamp_thread_count(10), 3)


@unittest.skipUnless(asr_gui is not None, "requires PyQt5 environment")
class GUIBatchCircuitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_cooldown_runs_one_probe_then_requires_manual_single_probe(self):
        widget = asr_gui.ASRWidget()
        try:
            inputs = [f"input-{index}.mp3" for index in range(50)]
            for input_path in inputs:
                widget.add_file_to_table(input_path)
                widget.enqueue_file(input_path)

            widget.process_file = Mock()
            widget.open_batch_circuit(30)

            self.assertEqual(widget.batch_state, "cooldown")
            self.assertTrue(widget.cooldown_timer.isSingleShot())
            self.assertTrue(widget.cooldown_timer.isActive())
            self.assertEqual(widget.cooldown_timer.interval(), 30000)
            self.assertEqual(
                [widget.table.item(row, 2).text() for row in range(50)],
                ["风控暂停"] * 50,
            )
            self.assertIn("剩余 50 个任务", widget.circuit_notice.text())

            widget.start_recovery_probe()
            probe = inputs[0]
            widget.process_file.assert_called_once_with(probe, is_probe=True)
            self.assertEqual(widget.batch_state, "probing")

            widget.handle_error(probe, "rate_limited", "limited", 30)
            self.assertEqual(widget.batch_state, "manual_pause")
            self.assertFalse(widget.cooldown_timer.isActive())
            self.assertEqual(widget.process_button.text(), "继续处理")
            self.assertEqual(widget.processing_queue, inputs)

            widget.start_recovery_probe()
            self.assertEqual(widget.batch_state, "probing")
            self.assertEqual(widget.process_file.call_count, 2)
            self.assertEqual(widget.processing_queue, inputs[1:])
        finally:
            widget.cooldown_timer.stop()
            widget.countdown_timer.stop()
            widget.close()

    def test_deleting_active_probe_does_not_leave_probing_state_stuck(self):
        widget = asr_gui.ASRWidget()
        try:
            inputs = ["probe.mp3", "next.mp3"]
            for input_path in inputs:
                widget.add_file_to_table(input_path)
                widget.enqueue_file(input_path)

            widget.process_file = Mock()
            widget.batch_state = "cooldown"
            widget.start_recovery_probe()
            probe_worker = Mock()
            widget.workers[inputs[0]] = probe_worker
            with patch.object(
                asr_gui.ASRWorker, "get_worker", return_value=probe_worker
            ):
                widget._terminate_and_cleanup_task(inputs[0])

            self.assertEqual(widget.batch_state, "manual_pause")
            self.assertIsNone(widget.probe_file)
            self.assertEqual(widget.processing_queue, [inputs[1]])
            probe_worker.cancel.assert_called_once_with()
        finally:
            widget.cooldown_timer.stop()
            widget.countdown_timer.stop()
            widget.close()

    def test_scheduler_uses_tracked_workers_at_completion_boundary(self):
        widget = asr_gui.ASRWidget()
        try:
            widget.max_threads = 1
            widget.processing_queue = ["next.mp3", "later.mp3"]
            widget.thread_pool = Mock()
            widget.thread_pool.activeThreadCount.return_value = 1

            def track_worker(file_path):
                widget.workers[file_path] = Mock()

            widget.process_file = Mock(side_effect=track_worker)
            widget.process_next_in_queue()

            widget.process_file.assert_called_once_with("next.mp3")
            self.assertEqual(widget.processing_queue, ["later.mp3"])
        finally:
            widget.cooldown_timer.stop()
            widget.countdown_timer.stop()
            widget.close()

    def test_cancelled_probe_stays_paused_and_is_requeued_first(self):
        widget = asr_gui.ASRWidget()
        try:
            inputs = ["probe.mp3", "next.mp3"]
            for input_path in inputs:
                widget.add_file_to_table(input_path)
                widget.enqueue_file(input_path)

            widget.process_file = Mock()
            widget.batch_state = "cooldown"
            widget.start_recovery_probe()
            widget.handle_cancelled(inputs[0])

            self.assertEqual(widget.batch_state, "manual_pause")
            self.assertEqual(widget.processing_queue, inputs)
            self.assertEqual(widget.process_file.call_count, 1)
            self.assertEqual(widget.process_button.text(), "继续处理")
        finally:
            widget.cooldown_timer.stop()
            widget.countdown_timer.stop()
            widget.close()

    def test_failed_probe_does_not_resume_normal_concurrency(self):
        widget = asr_gui.ASRWidget()
        try:
            inputs = ["probe.mp3", "next.mp3"]
            for input_path in inputs:
                widget.add_file_to_table(input_path)
                widget.enqueue_file(input_path)

            widget.process_file = Mock()
            widget.batch_state = "cooldown"
            widget.start_recovery_probe()
            with patch.object(asr_gui.InfoBar, "error"):
                widget.handle_error(inputs[0], "error", "network", 0)

            self.assertEqual(widget.batch_state, "manual_pause")
            self.assertEqual(widget.processing_queue, [inputs[1]])
            self.assertEqual(widget.process_file.call_count, 1)
            self.assertEqual(widget.table.item(0, 2).text(), "错误")
        finally:
            widget.cooldown_timer.stop()
            widget.countdown_timer.stop()
            widget.close()

    def test_polling_stall_cancels_other_active_workers(self):
        widget = asr_gui.ASRWidget()
        try:
            stalled = "stalled.mp3"
            active = "active.mp3"
            for input_path in (stalled, active):
                widget.add_file_to_table(input_path)
            active_worker = Mock()
            widget.workers[active] = active_worker

            widget.handle_error(
                stalled,
                "stalled",
                str(BcutPollingTimeoutError("timeout")),
                0,
            )

            self.assertEqual(widget.batch_state, "manual_pause")
            self.assertEqual(widget.processing_queue, [stalled])
            active_worker.cancel.assert_called_once_with()
            self.assertEqual(widget.table.item(1, 2).text(), "风控暂停")
        finally:
            widget.cooldown_timer.stop()
            widget.countdown_timer.stop()
            widget.close()

    def test_worker_preserves_rate_limit_category_and_retry_after(self):
        worker = asr_gui.ASRWorker("input.mp3", "B 接口", "SRT")
        errors = []
        worker.signals.errno.connect(lambda *args: errors.append(args))
        with patch.object(asr_gui, "BcutASR") as asr_class:
            asr_class.return_value.run.side_effect = BcutRateLimitedError(12.5)
            worker.run()
        self.app.processEvents()

        self.assertEqual(errors[0][0:2], ("input.mp3", "rate_limited"))
        self.assertEqual(errors[0][3], 12.5)


if __name__ == "__main__":
    unittest.main()
