# 精简归因服务

`scoring_app:create_app()` 是独立 Flask 应用。镜像只包含 Flask、NumPy、Gunicorn、`fingerprint.py` 和统一指纹库；不导入完整 `app.py`，不启动网页、SQLite、监测线程、建库管理或插件，也不保存上游密钥。原完整应用保留。

所有请求使用 `Authorization: Bearer <MODELTRACE_INTERNAL_TOKEN>`，token 至少 32 字符。只通过内网访问；Compose 默认仅绑定本机 `127.0.0.1:8080`，跨容器访问时由部署者显式接入同一私有 Docker 网络。

| 接口 | 契约 |
|---|---|
| GET `/v1/health` | 返回 `status/version/models`，用于确认目标指纹存在 |
| POST `/v1/challenges` | `{version,model,rounds:3}`；返回三轮，每轮三个独立挑战及 `expected_count` |
| POST `/v1/attribute` | `{version,outputs:[{text,completion:"complete",expected_count},…]}`；必须恰好三个有效完整样本，返回 `prediction/results/used_outputs` |
| POST `/v1/validate` | `{version,output:{text,completion,expected_count}}`；校验单份回答，返回 `{version,valid:boolean,reason?:string}` |

单样本校验直接复用 `fingerprint.analyze_outputs` 的解析和有效样本判定：取最长数字序列，只计 1–355 范围内数字，有效数量至少为 `max(80,ceil(expected_count*0.55))`。不要求输出数量恰好等于挑战数量。完整但数字不足的回答返回 HTTP 200、`valid:false`、`reason:"insufficient_valid_numbers"`；完成状态不是 `complete` 时返回 HTTP 200、`valid:false`、`reason:"incomplete_output"`。有效回答返回 HTTP 200、`valid:true`，不返回模型归因结果。

`output.text` 必须为不超过 100000 字符的字符串，`completion` 为非空字符串，`expected_count` 为 80–1000 的整数（不接受布尔值）。请求不是对象或 JSON 格式错误返回 400，样本参数错误返回 422，版本不符返回 409。认证、服务故障及版本冲突均不返回 `valid:false`，调用方不得将它们视为供应账户样本不合格。Portfolio 只补测无效样本；补测耗尽只能形成可用性异常，不能形成模型“不符”结论。

版本包含算法标识、指纹库、算法源码和接口源码摘要；版本不匹配返回 409。无效模型或挑战参数为 400，拒答、截断、断流或有效样本不足为 422，不形成“不符”结论。归因仅给出该轮第一名，三轮多数、重试、预算、15 分钟调度和隔离由 Portfolio/Sub2API 管理。

```sh
# 在部署环境安全设置随机 token，勿提交到仓库。
docker compose -f compose.scoring.yaml up -d --build
.venv/bin/python -m unittest discover -s tests -p test_scoring_app.py
```

与 Portfolio 同机部署时，使用 `compose.scoring.production.yaml` 覆盖文件接入既有私有网络。设置 `MODELTRACE_REVISION` 为部署的 Git SHA，`MODELTRACE_BIND_PORT` 可改为未占用的本机端口（例如 18083）；镜像以 SHA 标记并保存 OCI revision。Portfolio 通过 `http://modeltrace-scoring:8080` 访问，使用同一内部 token。运行 `docker compose -f compose.scoring.yaml -f compose.scoring.production.yaml up -d --build --wait modeltrace-scoring`，不重建数据库或其它应用。

滚动替换指纹库或算法会使旧批次归因明确失败；禁止将不同版本样本拼接。统计指纹提高混合供应的发现概率，不能证明每一次调用的真实模型身份。该服务的周期规则与完整应用“轮次完成后再计时”的旧本机监测器互不相干。
