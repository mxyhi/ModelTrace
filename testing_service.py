"""手动和定时测试共用的执行、进度和历史记录路径，以及 API 配置的模型列表获取。"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from concurrent.futures import CancelledError
from contextlib import closing
from threading import Event
from urllib.parse import urlencode

from enrollment import _compact_upstream_error, iter_test_events, upstream_user_agent
from testing_store import TestingStore


logger = logging.getLogger(__name__)
MAX_MODEL_PAGES = 20


def models_url(base_url: str) -> str:
    """与 completion_url 的地址规则一致：去掉聊天/消息端点后拼接 /v1/models。"""
    normalized = base_url.rstrip("/").removesuffix("/chat/completions").removesuffix("/messages")
    return normalized + ("/models" if normalized.endswith("/v1") else "/v1/models")


def list_models(base_url: str, api_key: str) -> list[str]:
    """读取上游模型 ID。OpenAI 与 Anthropic 都返回 {"data": [{"id": ...}]}，同时带两种鉴权头即可兼容；
    Anthropic 分页时按 has_more / last_id 继续读取。"""
    headers = {
        "Authorization": f"Bearer {api_key}", "x-api-key": api_key, "anthropic-version": "2023-06-01",
        "Accept": "application/json", "User-Agent": upstream_user_agent(),
    }
    url, models = models_url(base_url), []
    for _ in range(MAX_MODEL_PAGES):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            with error:
                details = error.read().decode("utf-8", errors="replace").strip()
            logger.warning("list_models_http_error status=%s", error.code)
            raise RuntimeError(f"HTTP {error.code}: {_compact_upstream_error(details, error.reason)}") from error
        except OSError as error:
            logger.warning("list_models_connection_error error_type=%s", type(error).__name__)
            raise RuntimeError(f"无法连接接口：{getattr(error, 'reason', error)}") from error
        except ValueError as error:
            raise RuntimeError("接口未返回 JSON 格式的模型列表") from error
        items = payload.get("data") if isinstance(payload, dict) else payload
        if not isinstance(items, list):
            raise RuntimeError("接口返回中没有模型列表（data 字段）")
        models += [item.get("id") if isinstance(item, dict) else item for item in items]
        if not (isinstance(payload, dict) and payload.get("has_more") and payload.get("last_id")):
            break
        url = f"{models_url(base_url)}?{urlencode({'after_id': payload['last_id']})}"
    return sorted({model.strip() for model in models if isinstance(model, str) and model.strip()}, key=str.lower)


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
