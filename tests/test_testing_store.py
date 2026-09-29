import os
import json
import tempfile
import unittest
from pathlib import Path
from threading import Event
from unittest.mock import patch

# 导入 app 前指向临时库，避免改写本机正在运行的测试记录。
os.environ["MODELTRACE_TESTING_DB"] = str(Path(tempfile.mkdtemp(prefix="modeltrace-tests-")) / "testing.sqlite3")
import app as web  # noqa: E402
from testing_store import TestingStore
from test_scheduler import TestScheduler


VALUES = {"name": "测试配置", "base_url": "https://example.test/v1", "api_model": "mock",
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
        for values in ({"name": ""}, {"api_key": ""}, {"interval_minutes": 0}, {"stream": "true"}, {"base_url": "file:///tmp"}):
            self.assertEqual(self.client.post("/api/test/configs", json={**VALUES, **values}).status_code, 400)
        self.assertEqual(self.client.patch("/api/test/configs/missing", json={"name": "test"}).status_code, 404)
        self.assertEqual(self.client.post("/api/test/runs", json={"config_id": "missing"}).status_code, 404)

    def test_manual_run_saves_result_and_configuration_snapshot(self):
        config = self.create()
        with patch("enrollment.iter_completion", side_effect=lambda *args, **kwargs: (part for part in ["7, " * 320])):
            response = self.client.post("/api/test/runs", json={"config_id": config["id"]})
            packets = [json.loads(line) for line in response.data.splitlines()]
        self.assertEqual(packets[-1]["type"], "result")
        self.assertEqual(packets[-1]["result"]["used_outputs"], 3)
        run_id = packets[-1]["run_id"]
        self.client.patch(f"/api/test/configs/{config['id']}", json={"name": "改名后", "api_model": "another-model"})
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
            response = self.client.post("/api/test/runs", json={"config_id": config["id"]})
            packets = [json.loads(line) for line in response.data.splitlines()]
        self.assertEqual(packets[-1]["type"], "error")
        self.assertNotIn(VALUES["api_key"], response.text)
        run = self.store.run(packets[-1]["run_id"])
        self.assertEqual(run["status"], "error")
        self.assertIsNone(run["result"])
        self.assertNotIn(VALUES["api_key"], run["error"])

    def test_disconnected_manual_run_becomes_cancelled(self):
        config = self.create()
        with patch("enrollment.iter_completion", side_effect=lambda *args, **kwargs: (part for part in ["7, " * 160, "8, " * 160])):
            response = self.client.post("/api/test/runs", json={"config_id": config["id"]}, buffered=False)
            packets = iter(response.response)
            run_id = json.loads(next(packets))["run_id"]
            self.assertEqual(json.loads(next(packets))["type"], "challenge")
            self.assertEqual(json.loads(next(packets))["type"], "delta")
            response.close()
        self.assertEqual(self.store.run(run_id)["status"], "cancelled")

    def test_scheduled_runs_use_the_same_history(self):
        config = self.create(stream=False)
        with patch("enrollment.iter_completion", side_effect=lambda *args, **kwargs: (part for part in ["9, " * 320])):
            result = web.run_scheduled_test(self.store.config(config["id"], include_key=True), Event())
        self.assertEqual(result["used_outputs"], 3)
        record = self.store.history(config["id"], 20, 0)["items"][0]
        self.assertEqual(record["source"], "scheduled")
        self.assertEqual(record["status"], "success")

    def test_pagination_filter_and_delete_removes_config_history(self):
        first, second = self.create(), self.create(name="配置二")
        for config_id in [first["id"], second["id"], first["id"]]:
            run = self.store.start_run(self.store.config(config_id, include_key=True), "manual")
            self.store.finish_run(run, "error", .1, error="测试失败")
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

    def test_config_list_includes_run_count_and_latest_run(self):
        first, second = self.create(), self.create(name="配置二")
        failed = self.store.start_run(self.store.config(first["id"], include_key=True), "manual")
        self.store.finish_run(failed, "error", .1, error="测试失败")
        succeeded = self.store.start_run(self.store.config(first["id"], include_key=True), "scheduled")
        self.store.finish_run(succeeded, "success", .2, result={"prediction_name": "mock", "probability": .9, "used_outputs": 3})
        # 最近一次失败时，仍要能看到之前最近一次成功的结果。
        latest = self.store.start_run(self.store.config(first["id"], include_key=True), "manual")
        self.store.finish_run(latest, "error", .1, error="最新失败")
        configs = {config["id"]: config for config in self.client.get("/api/test/configs").json["configs"]}
        self.assertEqual(configs[first["id"]]["run_count"], 3)
        self.assertEqual(configs[first["id"]]["last_run"]["status"], "error")
        self.assertEqual(configs[first["id"]]["last_run"]["error"], "最新失败")
        self.assertEqual(configs[first["id"]]["last_success"]["prediction"], "mock")
        self.assertEqual(configs[first["id"]]["last_success"]["probability"], .9)
        self.assertEqual(configs[first["id"]]["last_success"]["used_outputs"], 3)
        self.assertEqual(configs[second["id"]]["run_count"], 0)
        self.assertIsNone(configs[second["id"]]["last_run"])
        self.assertIsNone(configs[second["id"]]["last_success"])
        self.assertNotIn(VALUES["api_key"], json.dumps(configs))
        self.assertTrue(all("api_key" not in config for config in configs.values()))

    def test_restart_marks_interrupted_run_as_failed(self):
        config = self.create()
        run_id = self.store.start_run(self.store.config(config["id"], include_key=True), "manual")
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
                self.client.patch(f"/api/test/configs/{config['id']}", json={"api_model": "changed", "stream": False})
            finally:
                scheduler.stop()
                release.set()
                scheduler._thread.join(2)
            self.assertEqual(snapshots[0]["api_model"], "mock")
            self.assertTrue(snapshots[0]["stream"])
            self.assertEqual(self.client.delete(f"/api/test/configs/{config['id']}").status_code, 200)

    def test_nonstream_run_records_result_without_body_deltas(self):
        config = self.create(stream=False)
        with patch("enrollment.iter_completion", side_effect=lambda *args, **kwargs: (part for part in ["8, " * 320])):
            response = self.client.post("/api/test/runs", json={"config_id": config["id"]})
            packets = [json.loads(line) for line in response.data.splitlines()]
        self.assertNotIn("delta", [packet["type"] for packet in packets])
        self.assertEqual(packets[-1]["type"], "result")
        self.assertFalse(self.store.run(packets[-1]["run_id"])["config_snapshot"]["stream"])


if __name__ == "__main__":
    unittest.main()
