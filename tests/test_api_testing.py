import io
import json
import os
import tempfile
import time
import unittest
import urllib.error
from concurrent.futures import CancelledError
from pathlib import Path
from threading import Event
from unittest.mock import patch

# 导入 app 前指向临时库，避免改写本机正在运行的测试记录。
os.environ["MODELTRACE_TESTING_DB"] = str(Path(tempfile.mkdtemp(prefix="modeltrace-tests-")) / "testing.sqlite3")
import app as web  # noqa: E402
import enrollment
from completion_stream import completion_deltas
from test_scheduler import TestScheduler


CONFIG = {"base_url": "https://example.test/v1", "api_key": "test-secret", "api_model": "test-model", "temperature": None, "stream": True}
PROBE = {**CONFIG, "prompt": "test", "expected_count": 100}


def sse(*events):
    return "".join("data: " + (event if isinstance(event, str) else json.dumps(event, ensure_ascii=False)) + "\r\n\r\n" for event in events).encode()


def chunk(text="", finish=None, **delta):
    return {"choices": [{"index": 0, "delta": {"content": text, **delta}, "finish_reason": finish}]}


def response(body, content_type="text/event-stream"):
    result = io.BytesIO(body)
    result.headers = {"Content-Type": content_type}
    return result


class StreamingTests(unittest.TestCase):
    def test_openai_only_content_and_complete_end(self):
        data = sse(chunk(reasoning_content="not numbers"), chunk("中文 1, "), chunk("2", "stop"), {"choices": [], "usage": {}}, "[DONE]")
        self.assertEqual("".join(completion_deltas(io.BytesIO(data), "openai")), "中文 1, 2")

    def test_anthropic_ignores_thinking_and_ping(self):
        data = sse(
            {"type": "message_start", "message": {}}, {"type": "ping"},
            {"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": "123"}},
            {"type": "content_block_start", "content_block": {"type": "text", "text": "1, "}},
            {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "2"}},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"}}, {"type": "message_stop"},
        )
        self.assertEqual("".join(completion_deltas(io.BytesIO(data), "anthropic")), "1, 2")

    def test_partial_refused_truncated_and_error_streams_are_rejected(self):
        cases = [
            ("openai", sse(chunk("1, 2"))),
            ("openai", sse(chunk("1, 2"), "[DONE]")),
            ("openai", sse(chunk("1, 2", "length"), "[DONE]")),
            ("openai", sse(chunk(refusal="no"), "[DONE]")),
            ("openai", sse(chunk("1", "stop")) + b"data: [DONE]"),
            ("openai", sse(chunk("1"), {"error": {"message": "failed"}})),
            ("anthropic", sse({"type": "message_stop"})),
            ("anthropic", sse({"type": "message_delta", "delta": {"stop_reason": "max_tokens"}}, {"type": "message_stop"})),
            ("anthropic", sse({"type": "error", "error": {"message": "overloaded"}})),
        ]
        for api_format, data in cases:
            with self.subTest(api_format=api_format, data=data), self.assertRaises(RuntimeError):
                list(completion_deltas(io.BytesIO(data), api_format))

    def test_auto_format_does_not_switch_after_partial_output(self):
        with patch("enrollment.urllib.request.urlopen", return_value=response(sse(chunk("1, 2")))) as upstream:
            chunks = enrollment.iter_completion(**CONFIG, prompt="test")
            self.assertEqual(next(chunks), "1, 2")
            with self.assertRaisesRegex(RuntimeError, "中断"):
                next(chunks)
            self.assertEqual(upstream.call_count, 1)

    def test_auto_format_can_fallback_before_output(self):
        events = sse(
            {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "42"}},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"}}, {"type": "message_stop"},
        )
        failed = urllib.error.HTTPError(CONFIG["base_url"], 404, "not found", {}, io.BytesIO(b"{}"))
        with patch("enrollment.urllib.request.urlopen", side_effect=[failed, response(events)]) as upstream:
            self.assertEqual(enrollment.request_completion(**CONFIG, prompt="test"), "42")
            self.assertTrue(upstream.call_args.args[0].full_url.endswith("/messages"))

    def test_transient_http_error_retries_before_stream(self):
        failed = urllib.error.HTTPError(CONFIG["base_url"], 503, "busy", {}, io.BytesIO(b"{}"))
        with patch("enrollment.urllib.request.urlopen", side_effect=[failed, response(sse(chunk("42", "stop"), "[DONE]"))]) as upstream, patch("enrollment.time.sleep"):
            self.assertEqual(enrollment.request_completion(**CONFIG, prompt="test"), "42")
            self.assertEqual(upstream.call_count, 2)

    def test_probe_yields_before_upstream_end_and_validates(self):
        data = sse(chunk("1, " * 50), chunk("2, " * 50, "stop"), "[DONE]")
        transport = response(data)
        with patch("enrollment.urllib.request.urlopen", return_value=transport) as upstream:
            result = web.app.test_client().post("/api/test/probe", json=PROBE, buffered=False)
            packets = iter(result.response)
            self.assertEqual(json.loads(next(packets))["type"], "start")
            self.assertEqual(json.loads(next(packets))["type"], "delta")
            self.assertLess(transport.tell(), len(data))
            self.assertTrue(json.loads(upstream.call_args.args[0].data)["stream"])
            last = [json.loads(item) for item in packets][-1]
            self.assertTrue(last["accepted"])
            self.assertEqual(last["parsed_numbers"], 100)

    def test_probe_stream_failure_never_emits_result(self):
        with patch("enrollment.urllib.request.urlopen", return_value=response(sse(chunk("1, " * 100)))):
            result = web.app.test_client().post("/api/test/probe", json=PROBE)
            packets = [json.loads(item) for item in result.data.splitlines()]
            self.assertEqual(packets[-1]["type"], "error")
            self.assertNotIn("result", [item["type"] for item in packets])

    def test_nonstream_probe_retains_json(self):
        data = json.dumps({"choices": [{"message": {"content": "1, " * 100}, "finish_reason": "stop"}]}).encode()
        with patch("enrollment.urllib.request.urlopen", return_value=response(data, "application/json")) as upstream:
            result = web.app.test_client().post("/api/test/probe", json={**PROBE, "stream": False})
            self.assertEqual(result.mimetype, "application/json")
            self.assertTrue(result.json["accepted"])
            self.assertFalse(json.loads(upstream.call_args.args[0].data)["stream"])

    def test_cancel_prevents_further_challenges(self):
        cancel = Event()
        cancel.set()
        with patch("enrollment.request_completion") as request, self.assertRaises(CancelledError):
            enrollment.test_automatic(**CONFIG, bank={}, cancel=cancel)
        request.assert_not_called()

    def test_zero_usable_answers_keep_upstream_error(self):
        with patch("enrollment.iter_completion", side_effect=RuntimeError("HTTP 401: invalid key")), self.assertRaisesRegex(ValueError, "HTTP 401"):
            enrollment.test_automatic(**CONFIG, bank={})

    def test_browser_disconnect_closes_upstream_response(self):
        transport = response(sse(chunk("1, 2"), chunk("3", "stop"), "[DONE]"))
        with patch("enrollment.urllib.request.urlopen", return_value=transport):
            result = web.app.test_client().post("/api/test/probe", json=PROBE, buffered=False)
            packets = iter(result.response)
            next(packets)
            next(packets)
            result.close()
            self.assertTrue(transport.closed)


class SchedulerTests(unittest.TestCase):
    def tearDown(self):
        scheduler = getattr(self, "scheduler", None)
        if scheduler:
            scheduler.stop()
            if scheduler._thread:
                scheduler._thread.join(2)
                self.assertFalse(scheduler._thread.is_alive())

    def test_rounds_do_not_overlap_and_stopping_prevents_restart(self):
        entered = Event()
        release = Event()
        calls = []

        def run(configuration, cancel):
            calls.append(configuration.copy())
            entered.set()
            release.wait(2)
            return {"prediction_name": "test", "probability": .5, "used_outputs": 3}

        self.scheduler = TestScheduler(run)
        self.scheduler.start(CONFIG, .01)
        self.assertTrue(entered.wait(1))
        time.sleep(.03)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.scheduler.stop()["state"], "stopping")
        try:
            with self.assertRaises(RuntimeError):
                self.scheduler.start(CONFIG, .01)
        finally:
            release.set()
        self.scheduler._thread.join(1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.scheduler.status()["history"][0]["status"], "cancelled")
        self.assertNotIn(CONFIG["api_key"], json.dumps(self.scheduler.status()))

    def test_failure_is_recorded_and_next_round_runs(self):
        second = Event()
        calls = []

        def run(configuration, cancel):
            calls.append(time.monotonic())
            if len(calls) == 1:
                raise RuntimeError("bad " + configuration["api_key"])
            second.set()
            return {"prediction_name": "test", "probability": .7, "used_outputs": 2}

        self.scheduler = TestScheduler(run)
        self.scheduler.start(CONFIG, .03)
        self.assertTrue(second.wait(2))
        self.scheduler.stop()
        self.scheduler._thread.join(1)
        self.assertGreaterEqual(calls[1] - calls[0], .03)
        status = self.scheduler.status()
        self.assertEqual(status["history"][-1]["status"], "error")
        self.assertNotIn(CONFIG["api_key"], json.dumps(status))
        self.assertIsNone(status["next_run_at"])

    def test_schedule_routes_validation_and_page_independence(self):
        called = Event()

        def run(configuration, cancel):
            called.set()
            return {"prediction_name": "test", "probability": .8, "used_outputs": 3}

        self.scheduler = TestScheduler(run)
        with patch.object(web, "test_schedule", self.scheduler):
            first = web.app.test_client()
            saved = first.post("/api/test/configs", json={**CONFIG, "name": "定时", "api_models": [CONFIG["api_model"]]}).json["config"]
            # 定时只接受已保存的配置。
            for payload in ({"config_id": saved["id"], "interval_minutes": 0}, {"config_id": saved["id"], "interval_minutes": "nan"}, CONFIG):
                self.assertEqual(first.post("/api/test/schedule", json=payload).status_code, 400)
            self.assertEqual(first.post("/api/test/schedule", json={"config_id": "missing"}).status_code, 404)
            self.assertEqual(first.post("/api/test/schedule", json={"config_id": saved["id"], "interval_minutes": 60}).status_code, 201)
            self.assertTrue(called.wait(1))
            self.assertEqual(first.post("/api/test/schedule", json={"config_id": saved["id"]}).status_code, 409)
            # 新页面/新客户端能读取并停止同一后台任务，不依赖启动页面。
            second = web.app.test_client()
            status = second.get("/api/test/schedule").json
            self.assertTrue(status["enabled"])
            self.assertNotIn("api_key", status)
            self.assertEqual(second.delete("/api/test/schedule").status_code, 200)


if __name__ == "__main__":
    unittest.main()
