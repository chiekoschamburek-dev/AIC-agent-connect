# 问迹 · 梧桐平台接入材料

本目录存放 ACPs 接入所需的配置模板与**生成的 ACS 草稿**（草稿已被 `.gitignore` 排除，
避免把注册过程中的中间状态带进提交包）。

## 两条注册路径（择一，推荐 A）

### A. WIT 自动化（推荐）

```bash
cd backend
cp ../.env.acps.example ../.env.acps         # 填写 ACPS_USERNAME / ACPS_PASSWORD / ACPS_PUBLIC_ENDPOINT
.venv/bin/python -m app.acps.cli check       # 1) 本地校验声明与 ACS
.venv/bin/python -m app.acps.cli show-acs    # 2) 生成 ACS 草稿（可人工检查）
.venv/bin/python -m app.acps.cli register --watch   # 3) 登录→提交审核→按 2 分钟间隔盯批准
# 审核通过后（状态 active / serving）：
.venv/bin/python -m app.acps.cli serve       # 4) 启动 AIP Direct Partner + AMP
```

`register` 到达 `pending_review` 即返回；平台管理员批准后再次执行 `register`/`serve`
会自动继续 EAB→证书→finalize→active。

### B. 注册平台网页 + acps-cli 手工

1. `python -m app.acps.cli show-acs` 生成 ACS 草稿；
2. 登录 ACPs 智能体注册平台，上传 ACS、提交审核，获批后拿到 AIC；
3. 用 `acps-cli.toml.example` 完成 EAB 获取与 mTLS 证书签发（命令见文件头注释）；
4. 把证书材料放入 `ACPS_WORKSPACE`（默认 `.wit/wenji-aip-agent`）后再启动服务。

## 关键约定

| 项 | 说明 |
|---|---|
| `ACPS_PUBLIC_ENDPOINT` | 必须是平台与其它智能体可解析、可访问的 HTTPS 基地址；证书 SAN 会覆盖该主机名/IP |
| `ACPS_LISTEN_HOST/PORT` | 本机实际监听地址；经反向代理/NAT 映射时可与公网地址不同 |
| 私钥材料 | `ACPS_WORKSPACE` 内含 private key，禁止提交/截图/写入报告 |
| AMP 上报 | WIT 在 workspace 下写 `amp_*.jsonl`；平台侧转发要求见 `tools/amp_forwarder/README.md` |
| 访问统计 | Access 日志 `correlationId` 使用 taskId；Message 日志需 `messageId` + `eventType`（平台规则） |
