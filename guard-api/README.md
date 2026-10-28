# ACPS Guard API

[acps-event-channel-guard](https://github.com/Jingwengao-max/acps-event-channel-guard) 检测器的 HTTP 接口层。检测器本体(`guard_api/covert_event_guard.py`)与上游仓库同源,零改动;本包只做三件事:

1. **HTTP 接口**:把 `load_sqlite / analyze_events / analyze_peer_timing` 检测入口包成 REST + JSON;
2. **报告汇总**:移植 yanshu-AIC `guard_adapter.py` 的 `scan() → GuardReport`(含 `disposition="advisory-only"`、`pep_semantics="untouched"` 契约字段);
3. **两种运行方式**:独立进程(`python -m guard_api`)或**进程内挂载进 supervisor** 的 asyncio 事件循环(与 AIP 8444 服务同模型)。

只读承诺:SQLite 恒以 `mode=ro` 打开,本服务对任务库零写入,不落盘报告文件,不自动处置。

## 安装

```bash
# Python 3.10+,依赖仅 fastapi + uvicorn(测试另需 httpx)
pip install -r requirements.txt

# 若要在 my-project supervisor 里挂载导入,建议装成可编辑包:
pip install -e .
```

## 运行方式一:独立进程

```bash
python -m guard_api --db /path/to/aip-tasks.sqlite3 --port 8778
```

全部参数(均可由 `ACPS_GUARD_*` 环境变量替代):

| 参数 | 环境变量 | 默认 | 说明 |
|---|---|---|---|
| `--db` | `ACPS_GUARD_DB` | 必填 | 任务库路径(SQLite,只读打开) |
| `--host` | `ACPS_GUARD_HOST` | `127.0.0.1` | 监听地址 |
| `--port` | `ACPS_GUARD_PORT` | `8778` | 监听端口(避开 AIP 8444 / viz WS 8777) |
| `--limit` | `ACPS_GUARD_LIMIT` | `1000` | 每次扫描的事件/任务窗口上限 |
| `--min-level` | `ACPS_GUARD_MIN_LEVEL` | `MEDIUM` | 低于该等级不产生告警(明细仍返回) |
| `--session-gap` | `ACPS_GUARD_SESSION_GAP` | `3600` | peer timing 会话切分间隔(秒) |
| (无) | `ACPS_GUARD_TOKEN` | 未启用 | 配置后所有端点要求 `Authorization: Bearer <token>` |

## 运行方式二:挂载进 supervisor(推荐,与研枢一体)

在 `my-project` 的 `partner_supervisor.py` 中已接好开关:启动 supervisor 前设置

```bash
export ACPS_GUARD_API_PORT=8778          # 设了才挂载;不设行为完全不变
export ACPS_GUARD_DB=ws-supervisor/acps/aip-tasks.sqlite3   # 缺省用 supervisor 自己的任务库
```

guard API 会与 supervisor 共享同一个 asyncio 事件循环、同生共死,不产生额外进程。

## 接口

启动后打开 `http://127.0.0.1:8778/docs` 有交互式文档(Swagger UI)。端点一览:

| 端点 | 方法 | 说明 |
|---|---|---|
| `/health` | GET | 存活检查 + db 配置状态 |
| `/api/guard/scan` | GET / POST | 全量扫描:单任务三维检测 + peer timing + 告警汇总(GuardReport) |
| `/api/guard/tasks/{task_ref}` | GET | 单任务 RiskReport;无事件返回 404 |
| `/api/guard/peer-timing` | GET | 跨任务 Timing 检测(按 `direction/local_aic/remote_aic` 聚合) |
| `/api/guard/events` | POST | 内联事件分析,无需数据库(演示/CI 用),接受 JSON 数组或 JSONL |

通用查询参数(GET)或请求体字段(POST `scan`):`limit`、`min_level`、`session_gap`,单次请求可覆盖启动默认值。**db 路径只在启动时确定,不接受请求传入**(防任意文件读取)。

错误约定(`detail` 为 `{code, message}`):

- `400`:事件体无法解析 / 缺 `created_at`;
- `401` / `403`:Bearer Token 缺失 / 错误(仅配置 `ACPS_GUARD_TOKEN` 后);
- `404`:任务无事件(`task_not_found`);
- `422`:查询参数不合法(FastAPI 校验);
- `503`:db 未配置(`db_not_configured`)、文件缺失(`db_not_found`)或 schema 不支持(`unsupported_db`)。

### 示例

```bash
# 健康检查
curl http://127.0.0.1:8778/health

# 全量扫描(等价于 guard_adapter --json 的报告结构)
curl http://127.0.0.1:8778/api/guard/scan

# 只看 HIGH 告警
curl "http://127.0.0.1:8778/api/guard/scan?min_level=HIGH"

# 单任务
curl http://127.0.0.1:8778/api/guard/tasks/task_abc123

# peer timing,自定义会话切分
curl "http://127.0.0.1:8778/api/guard/peer-timing?session_gap=1800"

# 内联事件(无需数据库;也接受 JSONL 文本)
curl -X POST http://127.0.0.1:8778/api/guard/events \
  -H "Content-Type: application/json" \
  -d @examples/covert_guard_suspicious.jsonl.example
```

`scan` 响应骨架(字段与 guard_adapter 完全一致):

```json
{
  "generated_at": "2026-10-27T12:00:00+08:00",
  "db": "…/aip-tasks.sqlite3",
  "window_limit": 1000,
  "task_count": 3,
  "peer_count": 2,
  "highest_level": "HIGH",
  "alerts": [{"channel": "task", "level": "HIGH", "score": 63, "subject": "task-…",
               "rules": ["oversized_payload"], "detail": "…", "suggested_action": "…"}],
  "tasks": [...], "peers": [...], "warnings": [],
  "disposition": "advisory-only",
  "pep_semantics": "untouched"
}
```

## 测试

```bash
python -m unittest discover -s tests -v     # 20 项:端点、错误映射、Bearer、挂载生命周期
```

## 目录结构

```
guard-api/
├── guard_api/
│   ├── covert_event_guard.py   # 检测器(与上游同源,含 peer-timing/operational_burst)
│   ├── report.py               # scan() → GuardReport(移植自 guard_adapter,去掉落盘)
│   ├── app.py                  # create_app():FastAPI 工厂 + 五个端点
│   ├── mount.py                # start_guard_api()/stop_guard_api():supervisor 进程内挂载
│   ├── __main__.py             # 独立进程入口(python -m guard_api)
│   └── _version.py
├── tests/test_api.py           # 20 项测试(临时 SQLite + TestClient + 真实 uvicorn 挂载)
├── examples/                   # 上游正常/可疑 JSONL 样例
├── pyproject.toml / requirements.txt
└── README.md
```

## 安全边界

- 不修改任务数据库(`mode=ro`);不落盘任何文件;不写 viz 快照(留档仍用 guard_adapter CLI 的周期模式);
- 不签发/撤销令牌、不改 PEP 判定;告警里的 `suggested_action` 仅供人工复核;
- 报告不含载荷原文;配置 `ACPS_GUARD_TOKEN` 后所有端点需 Bearer 认证;
- 监听默认 `127.0.0.1`,如需对外暴露请自行加反向代理与网络隔离。
