import os
import io
import json
import sqlite3
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from threading import Event
from unittest.mock import patch

# 导入 app 前指向临时库，避免改写本机正在运行的测试记录。
os.environ["MODELTRACE_TESTING_DB"] = str(Path(tempfile.mkdtemp(prefix="modeltrace-tests-")) / "testing.sqlite3")
import app as web  # noqa: E402
from testing_store import TestingStore
from test_scheduler import TestScheduler


VALUES = {"name": "测试配置", "base_url": "https://example.test/v1", "api_models": ["mock"],
          "api_key": "local-test-secret", "temperature": None, "stream": True, "interval_minutes": 60}


class PersistedTestingTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "testing.sqlite3"
        self.store = TestingStore(self.path)
        self.patch = patch.object(web, "testing_store", self.store)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.client = web.app.test_client()

    def create(self, **values):
        response = self.client.post("/api/test/configs", json={**VALUES, **values})
        self.assertEqual(response.status_code, 201)
        self.assertNotIn(VALUES["api_key"], response.text)
        return response.json["config"]

    def started(self, config_id: str, source: str = "manual", model: str = "mock") -> str:
        return self.store.start_run({**self.store.config(config_id, include_key=True), "api_model": model}, source)

    def run_manual(self, config_id: str, **payload) -> dict:
        """发起后台手动测试并等待结束，返回最终任务快照。"""
        response = self.client.post(f"/api/test/manual/{config_id}", json=payload)
        self.assertEqual(response.status_code, 201, response.text)
        return self.wait_job(config_id)

    def wait_job(self, config_id: str) -> dict:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            job = self.client.get(f"/api/test/manual/{config_id}").json["job"]
            if job["state"] not in ("running", "stopping"):
                return job
            time.sleep(.02)
        self.fail("手动测试未在 5 秒内结束")

    def test_configs_persist_and_blank_key_edit_preserves_secret(self):
        config = self.create()
        edited = self.client.patch(f"/api/test/configs/{config['id']}", json={"name": "已编辑", "api_key": ""})
        self.assertEqual(edited.status_code, 200)
        self.assertTrue(edited.json["config"]["has_api_key"])
        self.assertNotIn("api_key", edited.json["config"])
        restarted = TestingStore(self.path)
        self.assertEqual(restarted.config(config["id"], include_key=True)["api_key"], VALUES["api_key"])
        self.assertEqual(restarted.configs()[0]["name"], "已编辑")
        self.assertEqual(self.client.get("/api/test/configs").json["key_storage"], "local")

    def test_config_validation_and_not_found(self):
        for values in ({"name": ""}, {"api_key": ""}, {"interval_minutes": 0}, {"stream": "true"}, {"base_url": "file:///tmp"},
                       {"api_models": []}, {"api_models": "mock"}, {"api_models": [" "]}, {"api_models": [f"m{i}" for i in range(21)]}):
            self.assertEqual(self.client.post("/api/test/configs", json={**VALUES, **values}).status_code, 400)
        self.assertEqual(self.client.patch("/api/test/configs/missing", json={"name": "test"}).status_code, 404)
        self.assertEqual(self.client.post("/api/test/manual/missing", json={}).status_code, 404)
        config = self.create()
        for models in (None, [], "mock", ["other"], [{"model": "mock"}]):
            self.assertEqual(self.client.post(f"/api/test/manual/{config['id']}", json={"api_models": models}).status_code, 400)
        self.assertIsNone(self.client.get(f"/api/test/manual/{config['id']}").json["job"])

    def test_models_are_trimmed_deduplicated_and_kept_in_order(self):
        config = self.create(api_models=[" b ", "a", "b", ""])
        self.assertEqual(config["api_models"], ["b", "a"])
        edited = self.client.patch(f"/api/test/configs/{config['id']}", json={"api_models": ["c"]}).json["config"]
        self.assertEqual(edited["api_models"], ["c"])

    def test_single_model_configs_are_migrated_to_model_lists(self):
        with sqlite3.connect(self.path) as db:
            db.execute("DROP TABLE api_configs")
            db.execute("""CREATE TABLE api_configs (id TEXT PRIMARY KEY, name TEXT NOT NULL, base_url TEXT NOT NULL,
                api_model TEXT NOT NULL, api_key TEXT NOT NULL, temperature REAL, stream INTEGER NOT NULL,
                interval_minutes REAL NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
            db.execute("INSERT INTO api_configs VALUES ('old','旧配置','https://example.test/v1','old-model','key',NULL,1,60,'t','t')")
        db.close()
        store = TestingStore(self.path)
        self.assertEqual(store.config("old")["api_models"], ["old-model"])
        self.assertEqual(TestingStore(self.path).config("old", include_key=True)["api_key"], "key")

    def test_manual_run_saves_result_and_configuration_snapshot(self):
        config = self.create()
        with patch("enrollment.iter_completion", side_effect=lambda *args, **kwargs: (part for part in ["7, " * 320])):
            job = self.run_manual(config["id"])
        self.assertEqual(job["state"], "finished")
        self.assertEqual(job["models"][0]["status"], "success")
        self.assertEqual(job["result"]["used_outputs"], 3)
        self.assertEqual(job["steps"], ["done"] * 3 + ["skipped"] * 3)
        self.assertEqual(job["text"], "7, " * 320)
        run_id = job["models"][0]["run_id"]
        self.client.patch(f"/api/test/configs/{config['id']}", json={"name": "改名后", "api_models": ["another-model"]})
        run = self.client.get(f"/api/test/history/{run_id}").json["run"]
        self.assertEqual(run["status"], "success")
        self.assertEqual(run["source"], "manual")
        self.assertEqual(run["config_name"], VALUES["name"])
        self.assertEqual(run["config_snapshot"]["api_model"], "mock")
        self.assertNotIn(VALUES["api_key"], json.dumps(run))
        self.assertEqual(TestingStore(self.path).run(run_id)["result"]["used_outputs"], 3)

    def test_failure_saved_and_secret_redacted(self):
        config = self.create()
        with patch("enrollment.iter_completion", side_effect=RuntimeError("failed " + VALUES["api_key"])):
            job = self.run_manual(config["id"])
        self.assertEqual(job["models"][0]["status"], "error")
        self.assertEqual(job["steps"], ["error"] * 6)
        self.assertEqual(len(job["errors"]), 7)
        self.assertNotIn(VALUES["api_key"], json.dumps(job))
        run = self.store.run(job["models"][0]["run_id"])
        self.assertEqual(run["status"], "error")
        self.assertIsNone(run["result"])
        self.assertNotIn(VALUES["api_key"], run["error"])

    def test_manual_run_continues_in_background_until_stopped(self):
        config = self.create(api_models=["first", "second"])
        entered, release = Event(), Event()

        def completion(*args, **kwargs):
            entered.set()
            release.wait(2)
            yield "7, " * 320

        with patch("enrollment.iter_completion", side_effect=completion):
            started = self.client.post(f"/api/test/manual/{config['id']}", json={})
            self.assertEqual(started.status_code, 201)
            self.assertEqual([model["api_model"] for model in started.json["job"]["models"]], ["first", "second"])
            self.assertTrue(entered.wait(1))
            # 发起请求早已结束，测试仍在后台进行；进行中不能重复发起，也不能删除配置。
            job = self.client.get(f"/api/test/manual/{config['id']}").json["job"]
            self.assertEqual((job["state"], job["current"], job["attempt"]), ("running", 0, 1))
            self.assertEqual(self.client.post(f"/api/test/manual/{config['id']}", json={}).status_code, 409)
            self.assertEqual(self.client.delete(f"/api/test/configs/{config['id']}").status_code, 409)
            self.assertEqual(self.client.delete(f"/api/test/manual/{config['id']}").json["job"]["state"], "stopping")
            release.set()
            job = self.wait_job(config["id"])
        self.assertEqual(job["state"], "stopped")
        self.assertEqual([model["status"] for model in job["models"]], ["cancelled", "pending"])
        run = self.store.run(job["models"][0]["run_id"])
        self.assertEqual((run["status"], run["error"]), ("cancelled", "手动测试已停止"))
        self.assertEqual(self.store.history(config["id"], 20, 0)["total"], 1)
        self.assertEqual(self.client.delete(f"/api/test/configs/{config['id']}").status_code, 200)
        self.assertIsNone(self.client.get(f"/api/test/manual/{config['id']}").json["job"])

    def test_manual_run_tests_selected_models_in_order(self):
        config = self.create(stream=False, api_models=["good", "bad", "unused"])

        def completion(*args, **kwargs):
            if args[2] == "bad":
                raise RuntimeError("bad model")
            return (part for part in ["9, " * 320])

        with patch("enrollment.iter_completion", side_effect=completion):
            job = self.run_manual(config["id"], api_models=["bad", "good"])
        self.assertEqual(job["state"], "finished")
        self.assertEqual([(model["api_model"], model["status"]) for model in job["models"]], [("bad", "error"), ("good", "success")])
        # 多个模型时错误带模型名前缀；已逐条列出挑战错误时只标记该模型失败。
        self.assertEqual(job["errors"][-1], "bad · 测试失败，未取得足够的有效回答")
        records = self.store.history(config["id"], 20, 0)["items"]
        self.assertEqual({record["api_model"]: record["status"] for record in records}, {"good": "success", "bad": "error"})
        self.assertEqual({record["source"] for record in records}, {"manual"})

    def test_scheduled_round_tests_each_model_in_order(self):
        config = self.create(stream=False, api_models=["good", "bad"])

        def completion(*args, **kwargs):
            if args[2] == "bad":
                raise RuntimeError("bad model")
            return (part for part in ["9, " * 320])

        with patch("enrollment.iter_completion", side_effect=completion):
            web.run_scheduled_test(self.store.config(config["id"], include_key=True), Event())
            records = self.store.history(config["id"], 20, 0)["items"]
            # 单个模型失败不影响其他模型；全部失败时本轮失败。
            self.assertEqual({record["api_model"]: record["status"] for record in records}, {"good": "success", "bad": "error"})
            self.assertEqual({record["source"] for record in records}, {"scheduled"})
            only_bad = self.create(api_models=["bad"])
            with self.assertRaisesRegex(RuntimeError, "bad"):
                web.run_scheduled_test(self.store.config(only_bad["id"], include_key=True), Event())

    def test_pagination_filter_and_delete_removes_config_history(self):
        first, second = self.create(), self.create(name="配置二")
        for config_id in [first["id"], second["id"], first["id"]]:
            self.store.finish_run(self.started(config_id), "error", .1, error="测试失败")
        response = self.client.get(f"/api/test/history?config_id={first['id']}&limit=1&offset=1")
        self.assertEqual(response.json["total"], 2)
        self.assertEqual(len(response.json["items"]), 1)
        deleted = self.client.delete(f"/api/test/configs/{first['id']}")
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(deleted.json["deleted_runs"], 2)
        history = self.client.get("/api/test/history").json
        self.assertEqual(history["total"], 1)
        self.assertEqual(history["items"][0]["config_id"], second["id"])
        self.assertEqual(self.client.get("/api/test/history?limit=10000").status_code, 400)

    def test_config_list_includes_run_count_and_latest_run_per_model(self):
        first, second = self.create(api_models=["mock", "other"]), self.create(name="配置二")
        self.store.finish_run(self.started(first["id"]), "error", .1, error="测试失败")
        succeeded = self.started(first["id"], "scheduled")
        self.store.finish_run(succeeded, "success", .2, result={"prediction_name": "mock", "probability": .9, "used_outputs": 3})
        # 最近一次失败时，仍要能看到之前最近一次成功的结果。
        self.store.finish_run(self.started(first["id"]), "error", .1, error="最新失败")
        self.store.finish_run(self.started(first["id"], model="other"), "error", .1, error="其他模型失败")
        configs = {config["id"]: config for config in self.client.get("/api/test/configs").json["configs"]}
        mock, other = configs[first["id"]]["models"]
        self.assertEqual(configs[first["id"]]["run_count"], 4)
        self.assertEqual(configs[first["id"]]["last_run"]["api_model"], "other")
        self.assertEqual((mock["api_model"], other["api_model"]), ("mock", "other"))
        self.assertEqual(mock["last_run"]["status"], "error")
        self.assertEqual(mock["last_run"]["error"], "最新失败")
        self.assertEqual(mock["last_success"], {"started_at": mock["last_success"]["started_at"], "prediction": "mock", "probability": .9, "used_outputs": 3})
        self.assertEqual(other["last_run"]["error"], "其他模型失败")
        self.assertIsNone(other["last_success"])
        self.assertEqual(configs[second["id"]]["run_count"], 0)
        self.assertIsNone(configs[second["id"]]["last_run"])
        self.assertEqual(configs[second["id"]]["models"], [{"api_model": "mock", "last_run": None, "last_success": None}])
        self.assertNotIn(VALUES["api_key"], json.dumps(configs))
        self.assertTrue(all("api_key" not in config for config in configs.values()))

    def test_restart_marks_interrupted_run_as_failed(self):
        config = self.create()
        run_id = self.started(config["id"])
        run = TestingStore(self.path).run(run_id)
        self.assertEqual(run["status"], "error")
        self.assertIn("重启", run["error"])

    def test_active_schedule_keeps_snapshot_and_blocks_config_deletion(self):
        config = self.create()
        entered, release = Event(), Event()
        snapshots = []

        def runner(configuration, cancel):
            entered.set()
            release.wait(2)
            snapshots.append(dict(configuration))
            return {"prediction_name": "mock", "probability": .5, "used_outputs": 3}

        scheduler = TestScheduler(runner)
        with patch.object(web, "test_schedule", scheduler):
            try:
                status = self.client.post("/api/test/schedule", json={"config_id": config["id"]})
                self.assertEqual(status.status_code, 201)
                self.assertEqual(status.json["config_id"], config["id"])
                self.assertTrue(entered.wait(1))
                self.assertEqual(self.client.delete(f"/api/test/configs/{config['id']}").status_code, 409)
                self.client.patch(f"/api/test/configs/{config['id']}", json={"api_models": ["changed"], "stream": False})
            finally:
                scheduler.stop()
                release.set()
                scheduler._thread.join(2)
            self.assertEqual(snapshots[0]["api_models"], ["mock"])
            self.assertTrue(snapshots[0]["stream"])
            self.assertEqual(self.client.delete(f"/api/test/configs/{config['id']}").status_code, 200)

    def test_nonstream_run_records_result_without_body_deltas(self):
        config = self.create(stream=False)
        with patch("enrollment.iter_completion", side_effect=lambda *args, **kwargs: (part for part in ["8, " * 320])):
            job = self.run_manual(config["id"])
        self.assertFalse(job["stream"])
        self.assertEqual(job["text"], "")
        self.assertEqual(job["models"][0]["status"], "success")
        self.assertFalse(self.store.run(job["models"][0]["run_id"])["config_snapshot"]["stream"])


    def test_fetch_models_supports_both_formats_and_saved_key(self):
        pages = [{"data": [{"id": "b-model"}, {"id": "A-model"}], "has_more": True, "last_id": "b-model"},
                 {"data": [{"id": "b-model"}, {"id": "c-model"}], "has_more": False}]
        requests = []

        def urlopen(request, timeout):
            requests.append(request)
            return io.BytesIO(json.dumps(pages[len(requests) - 1]).encode())

        config = self.create()
        with patch("urllib.request.urlopen", side_effect=urlopen):
            response = self.client.post("/api/test/models", json={"base_url": "https://example.test/v1/chat/completions", "api_key": "", "config_id": config["id"]})
        self.assertEqual(response.json, {"models": ["A-model", "b-model", "c-model"]})
        self.assertEqual([request.full_url for request in requests],
                         ["https://example.test/v1/models", "https://example.test/v1/models?after_id=b-model"])
        self.assertEqual(requests[0].get_header("X-api-key"), VALUES["api_key"])
        self.assertEqual(requests[0].get_header("Authorization"), f"Bearer {VALUES['api_key']}")

    def test_fetch_models_validation_and_redacted_errors(self):
        for payload in ({"base_url": "file:///tmp", "api_key": "k"}, {"base_url": "https://example.test", "api_key": ""}):
            self.assertEqual(self.client.post("/api/test/models", json=payload).status_code, 400)
        self.assertEqual(self.client.post("/api/test/models", json={"base_url": "https://example.test", "config_id": "missing"}).status_code, 404)
        failure = urllib.error.HTTPError("https://example.test/v1/models", 401, "Unauthorized", {}, io.BytesIO(b'{"error": {"message": "bad key secret-key"}}'))
        with patch("urllib.request.urlopen", side_effect=failure):
            response = self.client.post("/api/test/models", json={"base_url": "https://example.test", "api_key": "secret-key"})
        self.assertEqual(response.status_code, 502)
        self.assertIn("HTTP 401", response.json["error"])
        self.assertNotIn("secret-key", response.text)
        with patch("urllib.request.urlopen", return_value=io.BytesIO(b"<html>")):
            self.assertEqual(self.client.post("/api/test/models", json={"base_url": "https://example.test", "api_key": "k"}).status_code, 502)


if __name__ == "__main__":
    unittest.main()
