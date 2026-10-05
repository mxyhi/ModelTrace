"""无密钥、无调度副作用的内部归因服务；不要导入完整 app。"""
from __future__ import annotations

import hashlib
import hmac
import os
from pathlib import Path

from flask import Flask, jsonify, request
from werkzeug.exceptions import HTTPException

from fingerprint import analyze_global_outputs, generate_challenges, load_bank

ALGORITHM_VERSION = "global-fingerprint-v1"


def create_app(bank_path: Path | None = None, token: str | None = None) -> Flask:
    secret = token if token is not None else os.environ.get("MODELTRACE_INTERNAL_TOKEN", "")
    if len(secret) < 32:
        raise ValueError("MODELTRACE_INTERNAL_TOKEN 至少需要 32 个字符")
    path = bank_path or Path(__file__).with_name("data") / "unified_bank.json"
    bank = load_bank(path)
    # 同时固定算法源码和数据内容，滚动升级时旧批次明确失败，禁止混样。
    digest = hashlib.sha256(path.read_bytes() + Path(__file__).with_name("fingerprint.py").read_bytes() + Path(__file__).read_bytes()).hexdigest()
    version = f"{ALGORITHM_VERSION}:{digest}"
    models = [model["id"] for model in bank["models"]]
    app = Flask(__name__, static_folder=None)
    app.config["MAX_CONTENT_LENGTH"] = 1_000_000

    @app.before_request
    def authenticate():
        if not hmac.compare_digest(request.headers.get("Authorization", ""), f"Bearer {secret}"):
            return jsonify(error="unauthorized"), 401

    @app.errorhandler(HTTPException)
    def http_error(error):
        return jsonify(error=error.name), error.code

    @app.get("/v1/health")
    def health():
        return jsonify(status="ok", version=version, models=models)

    def payload():
        body = request.get_json()
        if not isinstance(body, dict):
            raise ValueError("请求必须为对象")
        if body.get("version") != version:
            return body, (jsonify(error="version_conflict", version=version), 409)
        return body, None

    @app.post("/v1/challenges")
    def challenges():
        try:
            body, conflict = payload()
            if conflict:
                return conflict
            rounds = body.get("rounds", 3)
            if type(rounds) is not int or not 1 <= rounds <= 9:
                raise ValueError("轮数必须为 1–9")
            target = body.get("model", "gpt-6-astra")
            if target not in models:
                raise ValueError("指纹库不包含目标模型")
            return jsonify(version=version, rounds=[generate_challenges(3) for _ in range(rounds)])
        except (ValueError, TypeError) as error:
            return jsonify(error=str(error)), 400

    @app.post("/v1/attribute")
    def attribute():
        try:
            body, conflict = payload()
            if conflict:
                return conflict
            outputs = body.get("outputs")
            if not isinstance(outputs, list) or len(outputs) != 3:
                raise ValueError("完整归因必须恰好三份回答")
            for output in outputs:
                if not isinstance(output, dict) or output.get("completion") != "complete":
                    raise ValueError("断流、拒答、截断或未知完成状态不可归因")
                if not isinstance(output.get("text"), str) or len(output["text"]) > 100_000:
                    raise ValueError("回答格式无效")
                count = output.get("expected_count")
                if type(count) is not int or not 80 <= count <= 1000:
                    raise ValueError("预期数字数量无效")
            result = analyze_global_outputs(outputs, bank)
            if result["used_outputs"] != 3:
                raise ValueError("有效回答不足三份")
            return jsonify(version=version, **result)
        except (ValueError, TypeError, KeyError) as error:
            return jsonify(error=str(error)), 422

    return app
