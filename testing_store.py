"""本地 API 配置和测试记录。此数据库包含密钥，禁止进入版本控制。"""
from __future__ import annotations

import json
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class TestingStore:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS api_configs (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, base_url TEXT NOT NULL,
                    api_models TEXT NOT NULL, api_key TEXT NOT NULL, temperature REAL,
                    stream INTEGER NOT NULL, interval_minutes REAL NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS test_runs (
                    id TEXT PRIMARY KEY, config_id TEXT, config_name TEXT NOT NULL,
                    api_model TEXT NOT NULL, source TEXT NOT NULL, status TEXT NOT NULL,
                    started_at TEXT NOT NULL, finished_at TEXT, duration_seconds REAL,
                    prediction TEXT, probability REAL, used_outputs INTEGER,
                    error TEXT, result_json TEXT, config_snapshot TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS test_runs_started ON test_runs(started_at DESC);
                CREATE INDEX IF NOT EXISTS test_runs_config ON test_runs(config_id, started_at DESC);
            """)
            columns = {row["name"] for row in db.execute("PRAGMA table_info(api_configs)")}
            if "api_model" in columns:
                # 迁移：旧版每个配置只有一个模型，列改名为 api_models 并把值改为 JSON 数组；改名与改值在同一事务内。
                db.execute("BEGIN")
                db.execute("ALTER TABLE api_configs RENAME COLUMN api_model TO api_models")
                db.executemany("UPDATE api_configs SET api_models=? WHERE id=?", [
                    (json.dumps([row["api_models"]], ensure_ascii=False), row["id"])
                    for row in db.execute("SELECT id, api_models FROM api_configs").fetchall()
                ])
            # 单进程服务重启意味着旧请求已终止，不能永远显示“正在测试”。
            db.execute("UPDATE test_runs SET status='error', error=?, finished_at=? WHERE status='running'",
                       ("本地服务已重启，本次测试未完成", utc_now()))

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def decoded_config(row: sqlite3.Row) -> dict:
        value = dict(row)
        value["stream"] = bool(value["stream"])
        value["api_models"] = json.loads(value["api_models"])
        return value

    @classmethod
    def public_config(cls, row: sqlite3.Row) -> dict:
        value = cls.decoded_config(row)
        value["has_api_key"] = bool(value.pop("api_key"))
        return value

    def configs(self) -> list[dict]:
        """配置列表附带记录数、最近一次测试，以及每个模型最近一次测试和最近一次成功测试，供卡片与表格直接展示。"""
        latest = """SELECT * FROM (SELECT config_id, api_model, status, started_at, prediction, probability,
            used_outputs, error, row_number() OVER (
                PARTITION BY config_id, api_model ORDER BY started_at DESC, id DESC) AS position
            FROM test_runs WHERE config_id IS NOT NULL{}) WHERE position=1"""
        with self.connection() as db:
            rows = db.execute("""SELECT c.*, (SELECT count(*) FROM test_runs WHERE config_id=c.id) AS run_count
                FROM api_configs c ORDER BY c.created_at, c.id""").fetchall()
            last_runs = db.execute(latest.format("")).fetchall()
            successes = db.execute(latest.format(" AND status='success'")).fetchall()
        runs = {(row["config_id"], row["api_model"]): {key: row[key] for key in row.keys() if key not in ("config_id", "position")}
                for row in last_runs}
        success = {(row["config_id"], row["api_model"]): {key: row[key] for key in ("started_at", "prediction", "probability", "used_outputs")}
                   for row in successes}
        configs = []
        for row in rows:
            value = self.public_config(row)
            value["models"] = [{
                "api_model": model, "last_run": runs.get((value["id"], model)), "last_success": success.get((value["id"], model)),
            } for model in value["api_models"]]
            # 最近一次测试也统计已从配置移除的模型，与记录数口径一致。
            own = [run for (config_id, _), run in runs.items() if config_id == value["id"]]
            value["last_run"] = max(own, key=lambda run: run["started_at"]) if own else None
            configs.append(value)
        return configs

    def config(self, config_id: str, *, include_key: bool = False) -> dict:
        with self.connection() as db:
            row = db.execute("SELECT * FROM api_configs WHERE id=?", (config_id,)).fetchone()
        if row is None:
            raise LookupError("API 配置不存在")
        return self.decoded_config(row) if include_key else self.public_config(row)

    def save_config(self, values: dict, config_id: str | None = None) -> dict:
        now = utc_now()
        models = json.dumps(values["api_models"], ensure_ascii=False)
        if config_id is None:
            config_id = secrets.token_hex(12)
            with self.connection() as db:
                db.execute("""INSERT INTO api_configs
                    (id,name,base_url,api_models,api_key,temperature,stream,interval_minutes,created_at,updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?)""", (
                    config_id, values["name"], values["base_url"], models, values["api_key"],
                    values["temperature"], values["stream"], values["interval_minutes"], now, now,
                ))
        else:
            with self.connection() as db:
                updated = db.execute("""UPDATE api_configs SET name=?,base_url=?,api_models=?,
                    api_key=CASE WHEN ?='' THEN api_key ELSE ? END,temperature=?,stream=?,interval_minutes=?,updated_at=?
                    WHERE id=?""", (
                    values["name"], values["base_url"], models, values.get("api_key", ""), values.get("api_key", ""),
                    values["temperature"], values["stream"], values["interval_minutes"], now, config_id,
                ))
                if not updated.rowcount:
                    raise LookupError("API 配置不存在")
        return self.config(config_id)

    def delete_config(self, config_id: str) -> int:
        """删除配置及其测试记录，返回删除的记录数。"""
        with self.connection() as db:
            if not db.execute("DELETE FROM api_configs WHERE id=?", (config_id,)).rowcount:
                raise LookupError("API 配置不存在")
            # 测试记录只在所属配置的详情页展示，随配置一起删除，避免留下无法访问的记录。
            return db.execute("DELETE FROM test_runs WHERE config_id=?", (config_id,)).rowcount

    def start_run(self, configuration: dict, source: str) -> str:
        run_id = secrets.token_hex(12)
        snapshot = {key: configuration.get(key) for key in ("id", "name", "base_url", "api_model", "temperature", "stream", "interval_minutes")}
        with self.connection() as db:
            db.execute("""INSERT INTO test_runs
                (id,config_id,config_name,api_model,source,status,started_at,config_snapshot)
                VALUES (?,?,?,?,?,'running',?,?)""", (
                run_id, configuration.get("id"), configuration.get("name") or configuration["api_model"],
                configuration["api_model"], source, utc_now(), json.dumps(snapshot, ensure_ascii=False),
            ))
        return run_id

    def finish_run(self, run_id: str, status: str, duration: float, *, result: dict | None = None, error: str | None = None) -> None:
        result = result or {}
        with self.connection() as db:
            db.execute("""UPDATE test_runs SET status=?,finished_at=?,duration_seconds=?,prediction=?,
                probability=?,used_outputs=?,error=?,result_json=? WHERE id=? AND status='running'""", (
                status, utc_now(), round(duration, 2), result.get("prediction_name"), result.get("probability"),
                result.get("used_outputs"), error, json.dumps(result, ensure_ascii=False) if result else None, run_id,
            ))

    def history(self, config_id: str | None, limit: int, offset: int) -> dict:
        where, args = (" WHERE config_id=?", [config_id]) if config_id else ("", [])
        with self.connection() as db:
            total = db.execute("SELECT count(*) FROM test_runs" + where, args).fetchone()[0]
            rows = db.execute("""SELECT id,config_id,config_name,api_model,source,status,started_at,
                finished_at,duration_seconds,prediction,probability,used_outputs,error FROM test_runs"""
                + where + " ORDER BY started_at DESC, id DESC LIMIT ? OFFSET ?", [*args, limit, offset]).fetchall()
        return {"items": [dict(row) for row in rows], "total": total, "limit": limit, "offset": offset}

    def run(self, run_id: str) -> dict:
        with self.connection() as db:
            row = db.execute("SELECT * FROM test_runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise LookupError("测试记录不存在")
        value = dict(row)
        value["result"] = json.loads(value.pop("result_json") or "null")
        value["config_snapshot"] = json.loads(value["config_snapshot"])
        return value
