import sys
import subprocess
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
