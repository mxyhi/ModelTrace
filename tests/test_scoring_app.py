import sys
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scoring_app import create_app


class ScoringTests(unittest.TestCase):
    def setUp(self):
        self.client = create_app(token="t" * 32).test_client()
        self.headers = {"Authorization": "Bearer " + "t" * 32}
        self.version = self.client.get("/v1/health", headers=self.headers).json["version"]

    def post(self, path, **body):
        return self.client.post(path, json={"version": self.version, **body}, headers=self.headers)

    def test_auth_and_no_full_app(self):
        self.assertEqual(self.client.get("/v1/health").status_code, 401)
        self.assertEqual(self.client.get("/", headers=self.headers).status_code, 404)
        # 全量测试会先导入完整应用；在新进程中验证精简入口的导入边界。
        result = subprocess.run(
            [sys.executable, "-c", "from scoring_app import create_app; import sys; create_app(token='t'*32); assert 'app' not in sys.modules; assert 'testing_store' not in sys.modules"],
            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_nine_challenges_and_version(self):
        response = self.post("/v1/challenges")
        self.assertEqual(response.status_code, 200)
        challenges = [item for group in response.json["rounds"] for item in group]
        self.assertEqual(len(challenges), 9)
        self.assertEqual(len({item["id"] for item in challenges}), 9)
        self.assertEqual(self.post("/v1/challenges", version="old").status_code, 409)
        self.assertEqual(self.post("/v1/challenges", model="unknown").status_code, 400)

    def test_only_three_complete_valid_samples(self):
        output = {"text": " ".join(str(i % 355 + 1) for i in range(320)), "expected_count": 320, "completion": "complete"}
        response = self.post("/v1/attribute", outputs=[output] * 3)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["used_outputs"], 3)
        self.assertEqual(response.json["prediction"], response.json["results"][0]["model"])
        for invalid in ({**output, "completion": "truncated"}, {**output, "text": "refused"}):
            self.assertEqual(self.post("/v1/attribute", outputs=[output, output, invalid]).status_code, 422)
        self.assertEqual(self.post("/v1/attribute", outputs=[output]).status_code, 422)

    def test_validate_uses_fingerprint_sample_boundaries(self):
        for count, expected, valid in [(79, 80, False), (80, 80, True), (175, 320, False), (176, 320, True)]:
            with self.subTest(count=count, expected=expected):
                output = {"text": "1 " * count, "expected_count": expected, "completion": "complete"}
                response = self.post("/v1/validate", output=output)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json["version"], self.version)
                self.assertIs(response.json["valid"], valid)
                self.assertEqual(self.post("/v1/attribute", outputs=[output] * 3).status_code, 200 if valid else 422)
        for text in ("refused", "1 " * 79 + "356 0", "1 " * 79 + "split " + "2 " * 79):
            response = self.post("/v1/validate", output={"text": text, "expected_count": 80, "completion": "complete"})
            self.assertEqual(response.status_code, 200)
            self.assertFalse(response.json["valid"])
            self.assertEqual(response.json["reason"], "insufficient_valid_numbers")

    def test_validate_transport_contract(self):
        output = {"text": "1 " * 320, "expected_count": 320, "completion": "complete"}
        for completion in ("truncated", "refused", "unknown"):
            response = self.post("/v1/validate", output={**output, "completion": completion})
            self.assertEqual(response.status_code, 200)
            self.assertFalse(response.json["valid"])
            self.assertEqual(response.json["reason"], "incomplete_output")
        self.assertEqual(self.post("/v1/validate", version="old", output=output).status_code, 409)
        self.assertEqual(self.client.post("/v1/validate", json=[], headers=self.headers).status_code, 400)
        self.assertEqual(self.client.post("/v1/validate", data="{", content_type="application/json", headers=self.headers).status_code, 400)
        for invalid in (None, {}, {**output, "text": 1}, {**output, "text": "1" * 100001}, {**output, "expected_count": True}, {**output, "expected_count": 79}, {**output, "expected_count": 1001}, {**output, "completion": None}):
            self.assertEqual(self.post("/v1/validate", output=invalid).status_code, 422)
        self.assertEqual(self.client.post("/v1/validate", json={"version": self.version, "output": output}).status_code, 401)
        with patch("scoring_app.analyze_outputs", side_effect=ValueError("broken scoring bank")):
            with self.assertLogs(self.client.application.logger, level="ERROR"):
                self.assertEqual(self.post("/v1/validate", output=output).status_code, 500)


if __name__ == "__main__":
    unittest.main()
