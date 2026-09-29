"""本地单进程定时测试：凭据只在工作线程内存中，轮次之间固定延迟。

每轮的归因结果由 runner 写入测试记录，这里只保留轮次状态。
"""
from __future__ import annotations

import logging
import time
from collections import deque
from collections.abc import Callable
from concurrent.futures import CancelledError
from datetime import datetime, timezone
from threading import Event, Lock, Thread


logger = logging.getLogger(__name__)


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


class TestScheduler:
    def __init__(self, runner: Callable[[dict, Event], object]):
        self._runner = runner
        self._lock = Lock()
        self._thread: Thread | None = None
        self._stop = Event()
        self._enabled = False
        self._running = False
        self._next_run_at: str | None = None
        self._config_id: str | None = None
        self._config_name = ""
        self._interval = 0.0
        self._stream = True
        self._history: deque[dict] = deque(maxlen=30)

    def status(self) -> dict:
        with self._lock:
            stopping = not self._enabled and self._thread is not None and self._thread.is_alive()
            return {
                "state": "stopping" if stopping else "running" if self._running else "waiting" if self._enabled else "stopped",
                "enabled": self._enabled,
                "config_id": self._config_id,
                "config_name": self._config_name,
                "stream": self._stream,
                "interval_minutes": self._interval / 60,
                "next_run_at": self._next_run_at,
                "history": list(self._history),
            }

    def start(self, configuration: dict, interval_seconds: float) -> dict:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise RuntimeError("定时任务已存在或正在停止，请等待结束后再启动")
            self._stop = Event()
            self._enabled = True
            self._running = True
            self._config_id = configuration.get("id")
            self._config_name = configuration.get("name", "")
            self._stream = configuration["stream"]
            self._interval = interval_seconds
            self._next_run_at = None
            self._thread = Thread(
                target=self._run, args=(dict(configuration), self._stop),
                daemon=True, name="modeltrace-scheduled-test",
            )
            self._thread.start()
        logger.info("schedule_started interval_seconds=%s stream=%s", interval_seconds, self._stream)
        return self.status()

    def stop(self) -> dict:
        with self._lock:
            self._enabled = False
            self._next_run_at = None
            self._stop.set()
        logger.info("schedule_stop_requested")
        return self.status()

    def _run(self, configuration: dict, stop: Event) -> None:
        try:
            while not stop.is_set():
                with self._lock:
                    self._running = True
                    self._next_run_at = None
                started = time.monotonic()
                record = {"started_at": timestamp()}
                try:
                    self._runner(configuration, stop)
                    if stop.is_set():
                        raise CancelledError()
                    record["status"] = "success"
                except CancelledError:
                    record["status"] = "cancelled"
                except Exception as error:
                    # 上游错误可能回显凭据，保留错误前进行脱敏。
                    record.update({
                        "status": "error",
                        "error": str(error).replace(configuration["api_key"], "[已隐藏]")[:1000],
                    })
                record.update({"finished_at": timestamp(), "duration_seconds": round(time.monotonic() - started, 2)})
                with self._lock:
                    self._history.appendleft(record)
                    self._running = False
                    if not stop.is_set():
                        self._next_run_at = datetime.fromtimestamp(time.time() + self._interval, timezone.utc).isoformat()
                logger.info("scheduled_test_finished status=%s duration_seconds=%s", record["status"], record["duration_seconds"])
                # Event.wait 可立即停止等待；计时从上一轮结束起算，不补跑错过的轮次。
                if stop.wait(self._interval):
                    break
        finally:
            configuration.clear()
            with self._lock:
                self._enabled = False
                self._running = False
                self._next_run_at = None
