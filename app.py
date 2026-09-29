from __future__ import annotations

import json
import math
import os
import re
import secrets
from pathlib import Path
from contextlib import closing
from threading import Event, Lock
from urllib.parse import urlsplit

from flask import Flask, Response, jsonify, render_template, request

from enrollment import bank_summary, enroll_automatic, iter_completion, request_completion
from test_scheduler import TestScheduler
from testing_service import collect_recorded_test, recorded_test
from testing_store import TestingStore
from fingerprint import analyze_global_outputs, generate_challenges, load_bank, parse_numbers
from bank_builder import build_bank, read_rows


app = Flask(__name__)
PROJECT = Path(__file__).resolve().parent
CUSTOM_BANKS_FILE = PROJECT / "data" / "custom_banks.json"
UNIFIED_BANK_FILE = PROJECT / "data" / "unified_bank.json"
DEFAULT_BANK_ID = "claude"
# 测试和临时验证用 MODELTRACE_TESTING_DB 指向独立库；启动时会把本库中未完成的记录标为中断。
testing_store = TestingStore(Path(os.environ.get("MODELTRACE_TESTING_DB") or PROJECT / "data" / "local" / "testing.sqlite3"))


def builtin_configs() -> dict[str, dict]:
    return {
        "gpt": {
            "label": "GPT",
            "bank_file": PROJECT / "data" / "gpt_bank.json",
            "data_file": PROJECT / "data" / "gpt_reference.jsonl",
        },
        "claude": {
            "label": "Claude",
            "bank_file": PROJECT / "data" / "claude_bank.json",
            "data_file": PROJECT / "data" / "claude_reference.jsonl",
        },
    }


def load_configs() -> dict[str, dict]:
    configs = builtin_configs()
    if CUSTOM_BANKS_FILE.exists():
        for item in json.loads(CUSTOM_BANKS_FILE.read_text(encoding="utf-8")):
            bank_id = item["id"]
            configs[bank_id] = {
                "label": item["label"],
                "bank_file": PROJECT / "data" / f"{bank_id}_bank.json",
                "data_file": PROJECT / "data" / f"{bank_id}_reference.jsonl",
                "custom": True,
            }
    return configs


def save_custom_configs() -> None:
    items = [
        {"id": bank_id, "label": config["label"]}
        for bank_id, config in BANK_CONFIGS.items()
        if config.get("custom")
    ]
    CUSTOM_BANKS_FILE.write_text(json.dumps(items, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_configured_bank(bank_id: str) -> dict | None:
    config = BANK_CONFIGS[bank_id]
    if not config["bank_file"].exists():
        return None
    bank = load_bank(config["bank_file"])
    bank["family_name"] = config["label"]
    return bank


BANK_CONFIGS = load_configs()
banks = {bank_id: load_configured_bank(bank_id) for bank_id in BANK_CONFIGS}


def active_banks() -> dict[str, dict]:
    return {
        bank_id: bank
        for bank_id, bank in banks.items()
        if bank is not None and bank.get("models")
    }


def global_reference_rows() -> list[dict]:
    rows = []
    for bank_id in active_banks():
        config = BANK_CONFIGS[bank_id]
        rows.extend(
            {
                **row,
                "family_id": bank_id,
                "family_name": config["label"],
            }
            for row in read_rows(config["data_file"])
        )
    return rows


def rebuild_global_bank() -> dict:
    global unified_bank
    unified_bank = build_bank(global_reference_rows())
    UNIFIED_BANK_FILE.write_text(
        json.dumps(unified_bank, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return unified_bank


unified_bank = load_bank(UNIFIED_BANK_FILE) if UNIFIED_BANK_FILE.exists() else None
if unified_bank is None:
    rebuild_global_bank()


def requested_bank_id(payload: dict | None = None) -> str:
    bank_id = (payload or {}).get("bank_id") or request.args.get("bank_id") or DEFAULT_BANK_ID
    if bank_id not in BANK_CONFIGS:
        raise ValueError(f"未知指纹库：{bank_id}")
    return bank_id


def requested_temperature(payload: dict) -> float | None:
    value = payload.get("temperature")
    return None if value in (None, "") else float(value)


def test_configuration(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("请求内容必须是 JSON 对象")
    configuration = {}
    for field in ("base_url", "api_key", "api_model"):
        value = payload.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"请填写 {field}")
        configuration[field] = value.strip()
    url = urlsplit(configuration["base_url"])
    if url.scheme not in {"http", "https"} or not url.netloc:
        raise ValueError("Base URL 必须是有效的 HTTP 或 HTTPS 地址")
    stream = payload.get("stream", False)
    if not isinstance(stream, bool):
        raise ValueError("stream 必须为布尔值")
    temperature = requested_temperature(payload)
    if temperature is not None and (not math.isfinite(temperature) or not 0 <= temperature <= 2):
        raise ValueError("温度必须在 0 到 2 之间")
    return {**configuration, "temperature": temperature, "stream": stream, "api_format": "auto"}


def run_scheduled_test(configuration: dict, cancel: Event) -> dict:
    return collect_recorded_test(testing_store, configuration, "scheduled", unified_bank, summarized_unified_bank(), cancel)


test_schedule = TestScheduler(run_scheduled_test)
# 开启定时与删除配置互斥，避免定时任务落在刚删除的配置上。
schedule_config_lock = Lock()


def load_test_config(payload: dict) -> dict:
    config_id = payload.get("config_id") if isinstance(payload, dict) else None
    if not isinstance(config_id, str) or not config_id:
        raise ValueError("请选择已保存的 API 配置")
    return testing_store.config(config_id, include_key=True)


def saved_config_values(payload: dict, config_id: str | None = None) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("请求内容必须是 JSON 对象")
    existing = testing_store.config(config_id, include_key=True) if config_id else {}
    values = {"stream": True, "interval_minutes": 60, **existing, **payload}
    if not values.get("api_key") and existing:
        values["api_key"] = existing["api_key"]
    name = values.get("name")
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 100:
        raise ValueError("配置名称需为 1–100 个字符")
    interval = float(values["interval_minutes"])
    if not math.isfinite(interval) or not 1 <= interval <= 1440:
        raise ValueError("测试间隔必须在 1 到 1440 分钟之间")
    configuration = test_configuration(values)
    configuration.pop("api_format")
    # 编辑时空密钥交给 UPDATE 原子保留，避免并发修改将旧密钥写回。
    if config_id and not payload.get("api_key"):
        configuration["api_key"] = ""
    return {**configuration, "name": name.strip(), "interval_minutes": interval}


@app.route("/api/test/configs", methods=["GET", "POST"])
def test_configs():
    if request.method == "GET":
        return jsonify({"configs": testing_store.configs(), "key_storage": "local"})
    try:
        config = testing_store.save_config(saved_config_values(request.get_json()))
        return jsonify({"config": config}), 201
    except (ValueError, TypeError) as error:
        return jsonify({"error": str(error)}), 400


@app.route("/api/test/configs/<config_id>", methods=["PATCH", "DELETE"])
def test_config_detail(config_id: str):
    try:
        if request.method == "DELETE":
            with schedule_config_lock:
                status = test_schedule.status()
                if status["state"] != "stopped" and status["config_id"] == config_id:
                    return jsonify({"error": "请先停止使用此配置的定时任务"}), 409
                deleted_runs = testing_store.delete_config(config_id)
            app.logger.info("test_config_deleted config_id=%s deleted_runs=%s", config_id, deleted_runs)
            return jsonify({"deleted": True, "deleted_runs": deleted_runs})
        values = saved_config_values(request.get_json(), config_id)
        return jsonify({"config": testing_store.save_config(values, config_id)})
    except LookupError as error:
        return jsonify({"error": str(error)}), 404
    except (ValueError, TypeError) as error:
        return jsonify({"error": str(error)}), 400


@app.post("/api/test/runs")
def start_test_run():
    try:
        configuration = load_test_config(request.get_json())
    except LookupError as error:
        return jsonify({"error": str(error)}), 404
    except (ValueError, TypeError) as error:
        return jsonify({"error": str(error)}), 400
    bank, summary = unified_bank, summarized_unified_bank()

    def generate():
        with closing(recorded_test(testing_store, configuration, "manual", bank, summary)) as events:
            for event in events:
                yield json.dumps(event, ensure_ascii=False) + "\n"
    return Response(generate(), mimetype="application/x-ndjson", headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@app.get("/api/test/history")
def test_history():
    try:
        limit = int(request.args.get("limit", 20))
        offset = int(request.args.get("offset", 0))
        if not 1 <= limit <= 100 or offset < 0:
            raise ValueError("分页参数无效")
        return jsonify(testing_store.history(request.args.get("config_id"), limit, offset))
    except ValueError as error:
        return jsonify({"error": str(error)}), 400


@app.get("/api/test/history/<run_id>")
def test_history_detail(run_id: str):
    try:
        return jsonify({"run": testing_store.run(run_id)})
    except LookupError as error:
        return jsonify({"error": str(error)}), 404


def summarized_bank(bank_id: str) -> dict:
    config = BANK_CONFIGS[bank_id]
    bank = banks.get(bank_id)
    summary = bank_summary(bank) if bank is not None else {
        "model_count": 0,
        "response_count": 0,
        "number_count": 0,
        "models": [],
        "calibration": {},
    }
    summary.update({"id": bank_id, "label": config["label"]})
    return summary


def summarized_unified_bank() -> dict:
    summaries = {bank_id: summarized_bank(bank_id) for bank_id in active_banks()}
    return {
        "id": "unified",
        "label": "全部指纹",
        "model_count": sum(item["model_count"] for item in summaries.values()),
        "response_count": sum(item["response_count"] for item in summaries.values()),
        "family_count": len(summaries),
        "families": [
            {"id": bank_id, "label": item["label"], "model_count": item["model_count"]}
            for bank_id, item in summaries.items()
        ],
    }


def replace_bank(bank_id: str) -> None:
    banks[bank_id] = load_configured_bank(bank_id)
    rebuild_global_bank()


@app.get("/")
def index():
    summaries = {bank_id: summarized_bank(bank_id) for bank_id in BANK_CONFIGS}
    return render_template(
        "index.html",
        banks=summaries,
        bank=summaries[DEFAULT_BANK_ID],
        unified=summarized_unified_bank(),
        default_bank_id=DEFAULT_BANK_ID,
    )


@app.get("/api/challenges")
def challenges():
    return jsonify({"challenges": generate_challenges(3)})


@app.post("/api/analyze")
def analyze():
    try:
        payload = request.get_json()
        result = analyze_global_outputs(payload["outputs"], unified_bank)
        result["bank"] = summarized_unified_bank()
        return jsonify(result)
    except ValueError as error:
        return jsonify({"error": str(error)}), 400


@app.post("/api/test/auto")
def automatic_test():
    payload = request.get_json()
    try:
        configuration = load_test_config(payload) if isinstance(payload, dict) and payload.get("config_id") else test_configuration(payload)
        result = collect_recorded_test(testing_store, configuration, "manual", unified_bank, summarized_unified_bank())
        return jsonify(result)
    except (ValueError, TypeError) as error:
        return jsonify({"error": str(error)}), 400
    except LookupError as error:
        return jsonify({"error": str(error)}), 404


@app.post("/api/test/probe")
def automatic_test_probe():
    payload = request.get_json() or {}
    try:
        configuration = test_configuration(payload)
        prompt = payload["prompt"]
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("挑战内容不能为空")
        expected_count = int(payload["expected_count"])
        if expected_count <= 0:
            raise ValueError("预期数字数必须大于 0")
    except (KeyError, TypeError, ValueError) as error:
        return jsonify({"error": str(error)}), 400

    def result_for(text: str) -> dict:
        parsed_numbers = len(parse_numbers(text))
        minimum_numbers = max(80, math.ceil(expected_count * 0.55))
        return {"text": text, "parsed_numbers": parsed_numbers, "minimum_numbers": minimum_numbers, "accepted": parsed_numbers >= minimum_numbers}

    if configuration["stream"]:
        def generate():
            yield json.dumps({"type": "start"}) + "\n"
            parts = []
            try:
                with closing(iter_completion(**configuration, prompt=prompt)) as chunks:
                    for text in chunks:
                        parts.append(text)
                        yield json.dumps({"type": "delta", "text": text}, ensure_ascii=False) + "\n"
                yield json.dumps({"type": "result", **result_for("".join(parts))}, ensure_ascii=False) + "\n"
            except Exception as error:
                app.logger.warning("probe_failed stream=true error_type=%s", type(error).__name__)
                message = str(error).replace(configuration["api_key"], "[已隐藏]")
                yield json.dumps({"type": "error", "error": message}, ensure_ascii=False) + "\n"
        return Response(generate(), mimetype="application/x-ndjson", headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})
    try:
        return jsonify(result_for(request_completion(**configuration, prompt=prompt)))
    except Exception as error:
        return jsonify({"error": str(error).replace(configuration["api_key"], "[已隐藏]")}), 502


@app.route("/api/test/schedule", methods=["GET", "POST", "DELETE"])
def scheduled_test():
    if request.method == "GET":
        return jsonify(test_schedule.status())
    if request.method == "DELETE":
        return jsonify(test_schedule.stop())
    try:
        payload = request.get_json() or {}
        if not isinstance(payload, dict):
            raise ValueError("请求内容必须是 JSON 对象")
        configuration = load_test_config(payload) if payload.get("config_id") else test_configuration(payload)
        interval = float(payload.get("interval_minutes", configuration.get("interval_minutes", 60)))
        if not math.isfinite(interval) or not 1 <= interval <= 1440:
            raise ValueError("测试间隔必须在 1 到 1440 分钟之间")
        with schedule_config_lock:
            if configuration.get("id"):
                testing_store.config(configuration["id"])  # 加锁后复查，配置已删除则返回 404
            return jsonify(test_schedule.start(configuration, interval * 60)), 201
    except (TypeError, ValueError) as error:
        return jsonify({"error": str(error)}), 400
    except LookupError as error:
        return jsonify({"error": str(error)}), 404
    except RuntimeError as error:
        return jsonify({"error": str(error)}), 409


@app.get("/api/bank")
def get_bank():
    try:
        return jsonify(summarized_bank(requested_bank_id()))
    except ValueError as error:
        return jsonify({"error": str(error)}), 400


@app.get("/api/banks")
def get_banks():
    return jsonify({bank_id: summarized_bank(bank_id) for bank_id in BANK_CONFIGS})


@app.post("/api/banks")
def create_bank():
    payload = request.get_json()
    label = payload["label"].strip()
    if not label:
        return jsonify({"error": "请输入指纹库名称"}), 400
    bank_id = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-") or f"bank-{secrets.token_hex(3)}"
    if bank_id in BANK_CONFIGS:
        return jsonify({"error": "同名指纹库已存在"}), 400
    config = {
        "label": label,
        "bank_file": PROJECT / "data" / f"{bank_id}_bank.json",
        "data_file": PROJECT / "data" / f"{bank_id}_reference.jsonl",
        "custom": True,
    }
    config["data_file"].parent.mkdir(parents=True, exist_ok=True)
    config["data_file"].touch()
    BANK_CONFIGS[bank_id] = config
    banks[bank_id] = None
    save_custom_configs()
    return jsonify(
        {
            "bank": summarized_bank(bank_id),
            "banks": {item: summarized_bank(item) for item in BANK_CONFIGS},
            "unified": summarized_unified_bank(),
        }
    )


@app.post("/api/enroll/auto")
def automatic_enrollment():
    payload = request.get_json()
    try:
        bank_id = requested_bank_id(payload)
        config = BANK_CONFIGS[bank_id]
        result = enroll_automatic(
            base_url=payload["base_url"].strip(),
            api_key=payload["api_key"],
            api_model=payload["api_model"].strip(),
            model_label=payload["model_label"].strip(),
            sample_count=int(payload.get("sample_count", 36)),
            temperature=requested_temperature(payload),
            api_format="auto",
            data_file=config["data_file"],
            bank_file=config["bank_file"],
            bank_id=bank_id,
            provider="api",
        )
        replace_bank(bank_id)
        result["bank"] = summarized_bank(bank_id)
        result["unified"] = summarized_unified_bank()
        return jsonify(result)
    except (RuntimeError, ValueError) as error:
        return jsonify({"error": str(error)}), 400


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=7860, debug=False)
