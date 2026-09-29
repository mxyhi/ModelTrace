"""手动和定时测试共用的执行、进度和历史记录路径。"""
from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from concurrent.futures import CancelledError
from contextlib import closing
from threading import Event

from enrollment import iter_test_events
from testing_store import TestingStore


logger = logging.getLogger(__name__)


def redact(value, api_key: str):
    if isinstance(value, str):
        return value.replace(api_key, "[已隐藏]") if api_key else value
    if isinstance(value, list):
        return [redact(item, api_key) for item in value]
    if isinstance(value, dict):
        return {key: redact(item, api_key) for key, item in value.items()}
    return value


def recorded_test(
    store: TestingStore, configuration: dict, source: str, bank: dict, summary: dict,
    cancel: Event | None = None,
) -> Iterator[dict]:
    run_id = store.start_run(configuration, source)
    started = time.monotonic()
    finished = False
    try:
        yield {"type": "start", "run_id": run_id, "config_name": configuration.get("name") or configuration["api_model"]}
        arguments = {key: configuration[key] for key in ("base_url", "api_key", "api_model", "temperature", "stream")}
        with closing(iter_test_events(**arguments, api_format="auto", bank=bank, cancel=cancel)) as events:
            for raw_event in events:
                event = redact(raw_event, configuration["api_key"])
                if event["type"] == "result":
                    event["result"]["bank"] = summary
                    # 先保存再发完成事件；页面在此后关闭也不会丢失已完成结果。
                    store.finish_run(run_id, "success", time.monotonic() - started, result=event["result"])
                    finished = True
                    event["run_id"] = run_id
                    logger.info("test_run_completed run_id=%s source=%s", run_id, source)
                yield event
    except CancelledError:
        store.finish_run(run_id, "cancelled", time.monotonic() - started, error="定时测试已停止")
        finished = True
        raise
    except Exception as error:
        message = redact(str(error), configuration["api_key"])[:4000]
        store.finish_run(run_id, "error", time.monotonic() - started, error=message)
        finished = True
        logger.warning("test_run_failed run_id=%s source=%s error=%s", run_id, source, message)
        yield {"type": "error", "run_id": run_id, "error": message}
    finally:
        if not finished:
            store.finish_run(run_id, "cancelled", time.monotonic() - started, error="页面连接已断开，测试已停止")


def collect_recorded_test(*args, **kwargs) -> dict:
    with closing(recorded_test(*args, **kwargs)) as events:
        for event in events:
            if event["type"] == "result":
                return event["result"]
            if event["type"] == "error":
                raise ValueError(event["error"])
    raise RuntimeError("测试未返回结果")
