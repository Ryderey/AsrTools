import importlib
import json
import os
import tempfile
import threading
import time as real_time
import unittest
from unittest.mock import Mock, patch

import requests

from bk_asr.BaseASR import BaseASR
from bk_asr.BcutASR import (
    BcutASR,
    BcutPollingTimeoutError,
    BcutRateLimitedError,
)


bcut_module = importlib.import_module("bk_asr.BcutASR")


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def make_response(status, data=None):
    response = requests.Response()
    response.status_code = status
    response.url = "https://member.bilibili.com/<REDACTED>"
    response.request = requests.Request("GET", response.url).prepare()
    if data is not None:
        response._content = json.dumps(data).encode("utf-8")
        response.headers["Content-Type"] = "application/json"
    return response


class CacheProbeASR(BaseASR):
    def __init__(self, audio, cache_file):
        self.CACHE_FILE = cache_file
        super().__init__(audio, use_cache=True)

    def _run(self):
        return {"value": self.crc32_hex}

    def _make_segments(self, resp_data):
        return []


class BcutRequestResilienceTests(unittest.TestCase):
    def setUp(self):
        BcutASR._next_request_at = 0.0
        BcutASR._circuit_open_until = 0.0

    def test_412_opens_fail_fast_circuit_without_sleeping(self):
        clock = FakeClock()
        first = BcutASR(b"first")
        first.session.request = Mock(return_value=make_response(412))

        with patch.object(bcut_module.time, "monotonic", clock.monotonic), patch.object(
            bcut_module.time, "sleep", clock.sleep
        ):
            with self.assertRaises(BcutRateLimitedError) as caught:
                first.result("sensitive-task-id")

            self.assertEqual(first.session.request.call_count, 1)
            self.assertNotIn("sensitive-task-id", str(caught.exception))
            self.assertEqual(caught.exception.retry_after, 30.0)

            second = BcutASR(b"second")
            second.session.request = Mock(
                return_value=make_response(200, {"data": {"state": 1}})
            )
            with self.assertRaises(BcutRateLimitedError) as open_circuit:
                second.result("next-task")
            self.assertEqual(open_circuit.exception.retry_after, 30.0)
            second.session.request.assert_not_called()

            clock.now = 30.0
            self.assertEqual(second.result("next-task"), {"state": 1})

        self.assertEqual(clock.sleeps, [])
        request_kwargs = second.session.request.call_args.kwargs
        self.assertEqual(request_kwargs["params"]["model_id"], 7)
        self.assertEqual(request_kwargs["timeout"], (10, 60))

    def test_api_requests_are_serialized_across_instances(self):
        active = 0
        max_active = 0
        state_lock = threading.Lock()

        def transport(*args, **kwargs):
            nonlocal active, max_active
            with state_lock:
                active += 1
                max_active = max(max_active, active)
            real_time.sleep(0.02)
            with state_lock:
                active -= 1
            return make_response(200, {"data": {"state": 1}})

        instances = [BcutASR(str(index).encode()) for index in range(3)]
        for instance in instances:
            instance.session.request = transport

        errors = []

        def query(instance, task_id):
            try:
                instance.result(task_id)
            except Exception as exc:  # pragma: no cover - assertion reports details
                errors.append(exc)

        with patch.object(bcut_module, "API_REQUEST_INTERVAL_SECONDS", 0):
            threads = [
                threading.Thread(target=query, args=(instance, f"task-{index}"))
                for index, instance in enumerate(instances)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

        self.assertEqual(max_active, 1)
        self.assertEqual(errors, [])

    def test_successful_api_requests_are_spaced_one_second_apart(self):
        clock = FakeClock()
        asr = BcutASR(b"paced")
        asr.session.request = Mock(
            side_effect=[
                make_response(200, {"data": {"state": 1}}),
                make_response(200, {"data": {"state": 1}}),
            ]
        )

        with patch.object(bcut_module.time, "monotonic", clock.monotonic), patch.object(
            bcut_module.time, "sleep", clock.sleep
        ):
            asr.result("first")
            asr.result("second")

        self.assertEqual(clock.sleeps, [1.0])

    def test_single_request_timeout_is_not_reported_as_polling_stall(self):
        asr = BcutASR(b"timeout")
        asr.session.request = Mock(side_effect=requests.ReadTimeout("secret-url"))

        with self.assertRaisesRegex(RuntimeError, "ReadTimeout") as caught:
            asr.result("sensitive-task-id")

        self.assertNotIsInstance(caught.exception, BcutPollingTimeoutError)
        self.assertNotIn("secret-url", str(caught.exception))
        self.assertNotIn("sensitive-task-id", str(caught.exception))

    def test_cancelled_worker_does_not_touch_api_transport(self):
        asr = BcutASR(b"cancelled", should_stop=lambda: True)
        asr.session.request = Mock()

        with self.assertRaisesRegex(RuntimeError, "已取消"):
            asr.result("task")

        asr.session.request.assert_not_called()

    def test_upload_and_create_task_use_models_sessions_and_timeouts(self):
        clock = FakeClock()
        calls = []

        create_upload = make_response(
            200,
            {
                "data": {
                    "in_boss_key": "key",
                    "resource_id": "resource",
                    "upload_id": "upload",
                    "upload_urls": ["https://storage.example/part"],
                    "per_size": 3,
                    "size": 3,
                }
            },
        )
        upload_part = make_response(200)
        upload_part.headers["Etag"] = "etag"
        commit = make_response(200, {"data": {"download_url": "https://download"}})
        task = make_response(200, {"data": {"task_id": "task"}})

        def transport(method, url, **kwargs):
            calls.append((method, url, kwargs))
            if url.endswith("/resource/create"):
                return create_upload
            if url == "https://storage.example/part":
                return upload_part
            if url.endswith("/resource/create/complete"):
                return commit
            if url.endswith("/task"):
                return task
            raise AssertionError(url)

        asr = BcutASR(b"abc")
        asr.session.request = transport
        with patch.object(bcut_module.time, "monotonic", clock.monotonic), patch.object(
            bcut_module.time, "sleep", clock.sleep
        ):
            asr.upload()
            self.assertEqual(asr.create_task(), "task")

        create_body = json.loads(calls[0][2]["data"])
        upload_call = calls[1]
        commit_body = json.loads(calls[2][2]["data"])
        task_body = calls[3][2]["json"]
        self.assertEqual(create_body["model_id"], "8")
        self.assertEqual(commit_body["model_id"], "8")
        self.assertEqual(task_body["model_id"], "8")
        self.assertEqual(upload_call[2]["timeout"], (10, 180))
        self.assertEqual(calls[0][2]["timeout"], (10, 60))

    def test_upload_part_error_does_not_expose_signed_url(self):
        signed_url = "https://storage.example/signed-secret"
        create_upload = make_response(
            200,
            {
                "data": {
                    "in_boss_key": "key",
                    "resource_id": "resource",
                    "upload_id": "upload",
                    "upload_urls": [signed_url],
                    "per_size": 3,
                    "size": 3,
                }
            },
        )

        def transport(method, url, **kwargs):
            if url.endswith("/resource/create"):
                return create_upload
            if url == signed_url:
                response = make_response(500)
                response.url = signed_url
                response.request = requests.Request("PUT", signed_url).prepare()
                return response
            raise AssertionError(url)

        asr = BcutASR(b"abc")
        asr.session.request = transport
        with self.assertRaises(RuntimeError) as caught:
            asr.upload()

        self.assertNotIn(signed_url, str(caught.exception))


class CacheConcurrencyTests(unittest.TestCase):
    def test_saves_merge_with_latest_disk_cache(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cache_file = os.path.join(temp_dir, "asr_cache.json")
            first = CacheProbeASR(b"first", cache_file)
            second = CacheProbeASR(b"second", cache_file)

            first.run()
            second.run()

            with open(cache_file, "r", encoding="utf-8") as file:
                cache = json.load(file)

        self.assertEqual(set(cache), {first._get_key(), second._get_key()})


class BcutPollingTests(unittest.TestCase):
    def setUp(self):
        BcutASR._next_request_at = 0.0
        BcutASR._circuit_open_until = 0.0

    def make_asr(self, result_side_effect):
        asr = BcutASR(b"polling")
        asr.upload = Mock()
        asr.create_task = Mock(return_value="task")
        asr.result = Mock(side_effect=result_side_effect)
        return asr

    def test_polling_waits_before_first_query_and_returns_completed_result(self):
        clock = FakeClock()
        asr = self.make_asr(
            [{"state": 4, "result": json.dumps({"utterances": []})}]
        )

        with patch.object(bcut_module.time, "monotonic", clock.monotonic), patch.object(
            bcut_module.time, "sleep", clock.sleep
        ):
            result = asr.run()

        self.assertEqual(result.segments, [])
        self.assertEqual(clock.sleeps, [2.0])

    def test_polling_raises_timeout_at_deadline(self):
        clock = FakeClock()
        asr = self.make_asr(lambda: {"state": 1})

        with patch.object(bcut_module, "POLL_TIMEOUT_SECONDS", 8.0), patch.object(
            bcut_module.time, "monotonic", clock.monotonic
        ), patch.object(bcut_module.time, "sleep", clock.sleep):
            with self.assertRaisesRegex(BcutPollingTimeoutError, "识别超时"):
                asr.run()

        self.assertEqual(clock.now, 8.0)

    def test_query_timeout_is_capped_by_remaining_deadline(self):
        clock = FakeClock()
        asr = BcutASR(b"polling")
        asr.upload = Mock()
        asr.create_task = Mock(return_value="task")

        def transport(*args, **kwargs):
            clock.sleep(sum(kwargs["timeout"]))
            raise requests.ReadTimeout()

        asr.session.request = Mock(side_effect=transport)
        with patch.object(bcut_module, "POLL_TIMEOUT_SECONDS", 8.0), patch.object(
            bcut_module.time, "monotonic", clock.monotonic
        ), patch.object(bcut_module.time, "sleep", clock.sleep):
            with self.assertRaisesRegex(BcutPollingTimeoutError, "识别超时"):
                asr.run()

        self.assertLessEqual(clock.now, 8.0)

    def test_early_connect_timeout_is_not_misreported_as_polling_stall(self):
        clock = FakeClock()
        asr = BcutASR(b"polling")
        asr._poll_deadline = 60.0

        def transport(*args, **kwargs):
            clock.sleep(kwargs["timeout"][0])
            raise requests.ConnectTimeout()

        asr.session.request = Mock(side_effect=transport)
        with patch.object(
            bcut_module.time, "monotonic", clock.monotonic
        ), patch.object(bcut_module.time, "sleep", clock.sleep):
            with self.assertRaisesRegex(RuntimeError, "ConnectTimeout") as caught:
                asr.result("task")

        self.assertNotIsInstance(caught.exception, BcutPollingTimeoutError)
        self.assertEqual(clock.now, 10.0)

    def test_upload_time_does_not_count_toward_polling_deadline(self):
        clock = FakeClock()
        asr = self.make_asr(lambda: {"state": 1})
        asr.upload.side_effect = lambda: clock.sleep(8.0)

        with patch.object(bcut_module, "POLL_TIMEOUT_SECONDS", 8.0), patch.object(
            bcut_module.time, "monotonic", clock.monotonic
        ), patch.object(bcut_module.time, "sleep", clock.sleep):
            with self.assertRaisesRegex(BcutPollingTimeoutError, "识别超时"):
                asr.run()

        asr.create_task.assert_called_once()
        self.assertEqual(clock.now, 16.0)

    def test_open_circuit_fails_polling_request_without_waiting(self):
        clock = FakeClock()
        BcutASR._circuit_open_until = 30.0
        asr = BcutASR(b"polling")
        asr.upload = Mock()
        asr.create_task = Mock(return_value="task")
        asr.session.request = Mock(
            return_value=make_response(200, {"data": {"state": 1}})
        )

        with patch.object(bcut_module, "POLL_TIMEOUT_SECONDS", 8.0), patch.object(
            bcut_module.time, "monotonic", clock.monotonic
        ), patch.object(bcut_module.time, "sleep", clock.sleep):
            with self.assertRaises(BcutRateLimitedError):
                asr.run()

        asr.session.request.assert_not_called()
        self.assertEqual(clock.now, 2.0)

    def test_completed_task_requires_nonempty_json_object_result(self):
        for raw_result in (None, "", "not-json", "[]"):
            with self.subTest(raw_result=raw_result):
                clock = FakeClock()
                asr = self.make_asr([{"state": 4, "result": raw_result}])
                with patch.object(
                    bcut_module.time, "monotonic", clock.monotonic
                ), patch.object(bcut_module.time, "sleep", clock.sleep):
                    with self.assertRaisesRegex(RuntimeError, "识别结果"):
                        asr.run()


if __name__ == "__main__":
    unittest.main()
