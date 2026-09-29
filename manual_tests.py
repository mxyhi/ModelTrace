"""手动测试后台任务：由服务端线程执行，刷新或关闭页面不影响；进度只保存在内存，页面轮询读取。

每个配置同一时间最多一个手动任务；多个模型按顺序逐个测试，每个模型由 runner 写一条测试记录。
服务重启不恢复任务，未完成的记录由 TestingStore 启动时标为中断。
"""
from __future__ import annotations

import copy
import logging
import secrets
from collections.abc import Callable, Iterator
from concurrent.futures import CancelledError
from threading import Event, Lock, Thread

from test_scheduler import timestamp


logger = logging.getLogger(__name__)
MAX_ATTEMPTS = 6
ACTIVE_STATES = ("running", "stopping")


class ManualTests:
    def __init__(self, runner: Callable[[dict, Event], Iterator[dict]]):
        # runner 测试单个模型，逐个产出 recorded_test 的进度事件。
        self._runner = runner
        self._lock = Lock()
        # 每个配置保留最近一次任务（含已结束的），页面轮询时才能读到最终状态。
        self._jobs: dict[str, dict] = {}
        self._cancels: dict[str, Event] = {}

    def status(self, config_id: str) -> dict | None:
        with self._lock:
            job = self._jobs.get(config_id)
            return copy.deepcopy(job) if job else None

    def active(self, config_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(config_id)
            return job is not None and job["state"] in ACTIVE_STATES

    def start(self, configuration: dict, models: list[str]) -> dict:
        config_id = configuration["id"]
        with self._lock:
            previous = self._jobs.get(config_id)
            if previous and previous["state"] in ACTIVE_STATES:
                raise RuntimeError("此配置正在手动测试，请等待结束或先停止")
            job = {
                "id": secrets.token_hex(8), "config_id": config_id, "state": "running", "stream": configuration["stream"],
                "models": [{"api_model": model, "status": "pending", "run_id": None} for model in models],
                "current": 0, "attempt": 0, "accepted": 0, "steps": ["pending"] * MAX_ATTEMPTS, "text": "",
                "errors": [], "result": None, "started_at": timestamp(), "finished_at": None,
            }
            cancel = Event()
            self._jobs[config_id] = job
            self._cancels[config_id] = cancel
            Thread(target=self._run, args=(job, dict(configuration), cancel), daemon=True, name="modeltrace-manual-test").start()
        logger.info("manual_test_started job_id=%s config_id=%s models=%s", job["id"], config_id, len(models))
        return self.status(config_id)

    def stop(self, config_id: str) -> dict | None:
        """不再发起后续挑战和模型；正在等待的上游请求结束后任务进入 stopped。"""
        with self._lock:
            job = self._jobs.get(config_id)
            if job and job["state"] == "running":
                job["state"] = "stopping"
                self._cancels[config_id].set()
                logger.info("manual_test_stop_requested job_id=%s config_id=%s", job["id"], config_id)
        return self.status(config_id)

    def forget(self, config_id: str) -> None:
        """配置删除后丢弃其已结束的任务。"""
        with self._lock:
            if not (config_id in self._jobs and self._jobs[config_id]["state"] in ACTIVE_STATES):
                self._jobs.pop(config_id, None)

    def _run(self, job: dict, configuration: dict, cancel: Event) -> None:
        try:
            for index, model in enumerate(job["models"]):
                if cancel.is_set():
                    break
                with self._lock:
                    job.update(current=index, attempt=0, accepted=0, steps=["pending"] * MAX_ATTEMPTS, text="")
                    model["status"] = "running"
                try:
                    for event in self._runner({**configuration, "api_model": model["api_model"]}, cancel):
                        with self._lock:
                            self._apply(job, model, event)
                except CancelledError:
                    break
                except Exception as error:
                    # runner 已把上游错误转成 error 事件；这里只兜底写记录等意外失败，单个模型失败不影响后续模型。
                    logger.exception("manual_test_model_failed job_id=%s", job["id"])
                    with self._lock:
                        model["status"] = "error"
                        job["errors"].append(self._prefix(job, model) + str(error).replace(configuration["api_key"], "[已隐藏]")[:1000])
        finally:
            configuration.clear()
            with self._lock:
                for model in job["models"]:
                    if model["status"] == "running":
                        model["status"] = "cancelled"
                job["state"] = "stopped" if cancel.is_set() else "finished"
                job["finished_at"] = timestamp()
                self._cancels.pop(job["config_id"], None)
            logger.info("manual_test_finished job_id=%s state=%s", job["id"], job["state"])

    @staticmethod
    def _prefix(job: dict, model: dict) -> str:
        return f"{model['api_model']} · " if len(job["models"]) > 1 else ""

    def _apply(self, job: dict, model: dict, event: dict) -> None:
        kind = event["type"]
        attempt = event.get("attempt")
        if kind == "start":
            model["run_id"] = event["run_id"]
        elif kind == "challenge":
            job["attempt"] = attempt
            job["steps"][attempt - 1] = "working"
            job["text"] = ""
        elif kind == "delta":
            job["text"] += event["text"]
        elif kind == "challenge_result":
            job["steps"][attempt - 1] = "done" if event["accepted"] else "invalid"
            if event["accepted"]:
                job["accepted"] += 1
            else:
                job["errors"].append(f"{self._prefix(job, model)}挑战 {attempt}：有效数字不足 {event['parsed_numbers']}/{event['minimum_numbers']}")
        elif kind == "challenge_error":
            job["steps"][attempt - 1] = "error"
            job["errors"].append(f"{self._prefix(job, model)}挑战 {attempt}：{event['error']}")
        elif kind == "result":
            model["status"] = "success"
            job["result"] = event["result"]
            job["steps"] = ["skipped" if step == "pending" else step for step in job["steps"]]
        elif kind == "error":
            model["status"] = "error"
            # 汇总错误会重复各挑战的原因；已逐条列出时只标记该模型失败。
            listed = any(step in ("invalid", "error") for step in job["steps"])
            job["errors"].append(self._prefix(job, model) + ("测试失败，未取得足够的有效回答" if listed else event["error"]))
