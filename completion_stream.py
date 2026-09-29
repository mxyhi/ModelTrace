"""只提取完整 SSE 消息中的正文；终止标记缺失时拒绝残缺样本。"""
from __future__ import annotations

import json
from collections.abc import Iterable, Iterator


def sse_data(lines: Iterable[bytes]) -> Iterator[str]:
    data = []
    for raw_line in lines:
        line = raw_line.decode("utf-8").rstrip("\r\n")
        if not line:
            if data:
                yield "\n".join(data)
                data.clear()
        elif line.startswith("data:"):
            data.append(line[5:].removeprefix(" "))
    # SSE 事件必须以空行结束；EOF 时未完成的帧不构成终止确认。


def completion_deltas(lines: Iterable[bytes], api_format: str) -> Iterator[str]:
    finish_reason = None
    for data in sse_data(lines):
        if data == "[DONE]" and api_format == "openai":
            if finish_reason != "stop":
                raise RuntimeError("回答缺少正常结束原因，本次回答不计入")
            return
        try:
            payload = json.loads(data)
        except json.JSONDecodeError as error:
            raise RuntimeError("上游返回了无效的流式数据") from error
        if not isinstance(payload, dict):
            raise RuntimeError("上游返回了无效的流式事件")
        if payload.get("error") or payload.get("type") == "error":
            error = payload.get("error", {})
            message = error.get("message", "上游流式错误") if isinstance(error, dict) else str(error)
            raise RuntimeError(f"上游流式错误：{message}")
        text = ""
        if api_format == "anthropic":
            event_type = payload.get("type")
            if event_type == "content_block_start":
                block = payload.get("content_block", {})
                if block.get("type") == "text":
                    text = block.get("text", "")
            elif event_type == "content_block_delta":
                delta = payload.get("delta", {})
                if delta.get("type") == "text_delta":
                    text = delta.get("text", "")
            elif event_type == "message_delta":
                finish_reason = payload.get("delta", {}).get("stop_reason") or finish_reason
                if finish_reason not in {None, "end_turn", "stop_sequence"}:
                    raise RuntimeError(f"回答未正常完成（{finish_reason}），本次回答不计入")
            elif event_type == "message_stop":
                if finish_reason not in {"end_turn", "stop_sequence"}:
                    raise RuntimeError("回答缺少正常结束原因，本次回答不计入")
                return
        else:
            # role、reasoning 和最后的 usage 块均不是可参与指纹分析的正文。
            for choice in payload.get("choices", []):
                if choice.get("index", 0) != 0:
                    continue
                delta = choice.get("delta", {})
                if delta.get("refusal"):
                    raise RuntimeError("模型拒绝生成，本次回答不计入")
                text = delta.get("content") or ""
                if isinstance(text, list):
                    text = "".join(part.get("text", "") for part in text if part.get("type") == "text")
                finish_reason = choice.get("finish_reason") or finish_reason
                if finish_reason not in {None, "stop"}:
                    raise RuntimeError(f"回答未正常完成（{finish_reason}），本次回答不计入")
        if text:
            yield text
    raise RuntimeError("流式回答意外中断，未收到结束标记，本次回答不计入")
