import json
import logging
import threading
import time
from typing import Callable, Optional, Union

import requests

from .ASRData import ASRDataSeg
from .BaseASR import BaseASR


__version__ = "0.0.3"

API_BASE_URL = "https://member.bilibili.com/x/bcut/rubick-interface"

# 申请上传
API_REQ_UPLOAD = API_BASE_URL + "/resource/create"

# 提交上传
API_COMMIT_UPLOAD = API_BASE_URL + "/resource/create/complete"

# 创建任务
API_CREATE_TASK = API_BASE_URL + "/task"

# 查询结果
API_QUERY_RESULT = API_BASE_URL + "/task/result"

UPLOAD_MODEL_ID = "8"
RESULT_MODEL_ID = 7
API_TIMEOUT = (10, 60)
UPLOAD_TIMEOUT = (10, 180)
API_REQUEST_INTERVAL_SECONDS = 1.0
RATE_LIMIT_COOLDOWN_SECONDS = 30.0
INITIAL_POLL_DELAY_SECONDS = 2.0
POLL_INTERVAL_SECONDS = 3.0
POLL_TIMEOUT_SECONDS = 10 * 60.0
POLL_TIMEOUT_MESSAGE = "必剪识别超时，请稍后重新处理当前文件"


class BcutRateLimitedError(RuntimeError):
    """The public Bcut API rejected the request because of rate limiting."""

    def __init__(self, retry_after: float):
        self.retry_after = max(0.0, retry_after)
        super().__init__(
            f"必剪接口触发临时风控，请在 {self.retry_after:.0f} 秒后重试"
        )


class BcutPollingTimeoutError(RuntimeError):
    """The remote recognition task stopped making progress in time."""


class BcutASR(BaseASR):
    """必剪 语音识别接口"""
    _request_lock = threading.Lock()
    _next_request_at = 0.0
    _circuit_open_until = 0.0

    headers = {
        'User-Agent': 'Bilibili/1.0.0 (https://www.bilibili.com)',
        'Content-Type': 'application/json'
    }

    def __init__(
        self,
        audio_path: Union[str, bytes],
        use_cache: bool = False,
        should_stop: Optional[Callable[[], bool]] = None,
    ):
        super().__init__(audio_path, use_cache=use_cache)
        self.should_stop = should_stop
        self.session = requests.Session()
        self.session.headers.update(self.headers)
        self.__in_boss_key = ""
        self.__resource_id = ""
        self.__upload_id = ""
        self.__upload_urls: list[str] = []
        self.__per_size = 0
        self.__clips = 0
        self.__etags: list[str] = []
        self.__download_url = ""
        self.task_id: Optional[str] = None
        self._poll_deadline: Optional[float] = None

    def _remaining_poll_time(self) -> Optional[float]:
        if self._poll_deadline is None:
            return None
        remaining = self._poll_deadline - time.monotonic()
        if remaining <= 0:
            raise BcutPollingTimeoutError(POLL_TIMEOUT_MESSAGE)
        return remaining

    def _check_cancelled(self) -> None:
        if self.should_stop and self.should_stop():
            raise RuntimeError("当前任务已取消")

    def _poll_timeout(self) -> tuple[float, float]:
        remaining = self._remaining_poll_time()
        if remaining is None or sum(API_TIMEOUT) <= remaining:
            return API_TIMEOUT
        connect_timeout = min(API_TIMEOUT[0], remaining / 2)
        return connect_timeout, min(API_TIMEOUT[1], remaining - connect_timeout)

    def _api_request(self, stage: str, method: str, url: str, **kwargs):
        """Send one paced request to the Bcut API without leaking its URL."""
        cls = type(self)
        with cls._request_lock:
            self._check_cancelled()
            now = time.monotonic()
            retry_after = cls._circuit_open_until - now
            if retry_after > 0:
                raise BcutRateLimitedError(retry_after)

            delay = cls._next_request_at - now
            if delay > 0:
                time.sleep(delay)
                self._check_cancelled()

            request_timeout = self._poll_timeout()
            try:
                response = self.session.request(
                    method, url, timeout=request_timeout, **kwargs
                )
            except requests.RequestException as exc:
                cls._next_request_at = (
                    time.monotonic() + API_REQUEST_INTERVAL_SECONDS
                )
                if (
                    isinstance(exc, requests.Timeout)
                    and self._poll_deadline is not None
                    and time.monotonic() >= self._poll_deadline
                ):
                    raise BcutPollingTimeoutError(POLL_TIMEOUT_MESSAGE) from exc
                raise RuntimeError(
                    f"必剪接口{stage}请求失败：{type(exc).__name__}"
                ) from exc

            now = time.monotonic()
            cls._next_request_at = now + API_REQUEST_INTERVAL_SECONDS
            if response.status_code == 412:
                cls._circuit_open_until = max(
                    cls._circuit_open_until, now + RATE_LIMIT_COOLDOWN_SECONDS
                )
                raise BcutRateLimitedError(cls._circuit_open_until - now)
            if not response.ok:
                raise RuntimeError(
                    f"必剪接口{stage}请求失败：HTTP {response.status_code}"
                )
            self._remaining_poll_time()
            return response

    def upload(self) -> None:
        """申请上传"""
        if not self.file_binary:
            raise ValueError("none set data")
        payload = json.dumps({
            "type": 2,
            "name": "audio.mp3",
            "size": len(self.file_binary),
            "ResourceFileType": "mp3",
            "model_id": UPLOAD_MODEL_ID,
        })

        resp = self._api_request(
            "申请上传",
            "POST",
            API_REQ_UPLOAD,
            data=payload,
        )
        resp = resp.json()
        resp_data = resp["data"]

        self.__in_boss_key = resp_data["in_boss_key"]
        self.__resource_id = resp_data["resource_id"]
        self.__upload_id = resp_data["upload_id"]
        self.__upload_urls = resp_data["upload_urls"]
        self.__per_size = resp_data["per_size"]
        self.__clips = len(resp_data["upload_urls"])

        logging.info(
            "申请上传成功, 总计大小%sKB, %s分片, 分片大小%sKB",
            resp_data["size"] // 1024,
            self.__clips,
            resp_data["per_size"] // 1024,
        )
        self.__upload_part()
        self.__commit_upload()

    def __upload_part(self) -> None:
        """上传音频数据"""
        for clip in range(self.__clips):
            self._check_cancelled()
            start_range = clip * self.__per_size
            end_range = (clip + 1) * self.__per_size
            logging.info(f"开始上传分片{clip}: {start_range}-{end_range}")
            try:
                resp = self.session.put(
                    self.__upload_urls[clip],
                    data=self.file_binary[start_range:end_range],
                    timeout=UPLOAD_TIMEOUT,
                )
            except requests.RequestException as exc:
                raise RuntimeError(
                    f"必剪上传分片 {clip + 1}/{self.__clips} 失败：{type(exc).__name__}"
                ) from exc
            self._check_cancelled()
            if not resp.ok:
                raise RuntimeError(
                    f"必剪上传分片 {clip + 1}/{self.__clips} 失败：HTTP {resp.status_code}"
                )
            etag = resp.headers.get("Etag")
            if not etag:
                raise RuntimeError(
                    f"必剪上传分片 {clip + 1}/{self.__clips} 失败：未返回 ETag"
                )
            self.__etags.append(etag)
            logging.info("分片%s上传成功", clip)

    def __commit_upload(self) -> None:
        """提交上传数据"""
        data = json.dumps({
            "InBossKey": self.__in_boss_key,
            "ResourceId": self.__resource_id,
            "Etags": ",".join(self.__etags),
            "UploadId": self.__upload_id,
            "model_id": UPLOAD_MODEL_ID,
        })
        resp = self._api_request(
            "提交上传",
            "POST",
            API_COMMIT_UPLOAD,
            data=data,
        )
        resp = resp.json()
        self.__download_url = resp["data"]["download_url"]
        logging.info("提交成功")

    def create_task(self) -> str:
        """开始创建转换任务"""
        resp = self._api_request(
            "创建任务",
            "POST",
            API_CREATE_TASK,
            json={"resource": self.__download_url, "model_id": UPLOAD_MODEL_ID},
        )
        resp = resp.json()
        self.task_id = resp["data"]["task_id"]
        logging.info("任务已创建")
        return self.task_id

    def result(self, task_id: Optional[str] = None):
        """查询转换结果"""
        resp = self._api_request(
            "查询结果",
            "GET",
            API_QUERY_RESULT,
            params={
                "model_id": RESULT_MODEL_ID,
                "task_id": task_id or self.task_id,
            },
        )
        resp = resp.json()
        return resp["data"]

    def _run(self):
        self.upload()
        self.create_task()
        self._poll_deadline = time.monotonic() + POLL_TIMEOUT_SECONDS
        try:
            remaining = self._remaining_poll_time()
            if remaining is not None and INITIAL_POLL_DELAY_SECONDS >= remaining:
                raise BcutPollingTimeoutError(POLL_TIMEOUT_MESSAGE)
            time.sleep(INITIAL_POLL_DELAY_SECONDS)

            while True:
                self._remaining_poll_time()
                task_resp = self.result()
                if task_resp["state"] == 4:
                    raw_result = task_resp.get("result")
                    if not raw_result:
                        raise RuntimeError("必剪识别结果为空，请重新处理当前文件")
                    try:
                        result = json.loads(raw_result)
                    except (TypeError, json.JSONDecodeError) as exc:
                        raise RuntimeError("必剪识别结果格式无效，请重新处理当前文件") from exc
                    if not isinstance(result, dict):
                        raise RuntimeError("必剪识别结果格式无效，请重新处理当前文件")
                    logging.info("转换成功")
                    return result

                remaining = self._remaining_poll_time()
                if remaining is not None and POLL_INTERVAL_SECONDS >= remaining:
                    time.sleep(remaining)
                else:
                    time.sleep(POLL_INTERVAL_SECONDS)
        finally:
            self._poll_deadline = None

    def _make_segments(self, resp_data: dict) -> list[ASRDataSeg]:
        return [ASRDataSeg(u['transcript'], u['start_time'], u['end_time']) for u in resp_data['utterances']]


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    # Example usage
    audio_file = r"test.mp3"
    asr = BcutASR(audio_file)
    asr_data = asr.run()
    print(asr_data)
