# 历史排障脚本（归档）

本目录原为 `acps_cluster/deploy/discovery_diagnostics/`，是 2026-09～10 期间针对
平台注册、CA 切换、KB 重注册、AMP 心跳/转发、可视化直连等问题的**一次性排查脚本**。

- **不参与一键部署**：正式部署入口是仓库根的 `deploy.sh` 与 `deploy/*.service` 模板；
- 脚本内的 `/opt/wit/my-project` 等路径是当时服务器的现场路径，未做 `ACPS_ROOT` 适配；
- 保留原因：事故复盘、审计证据与后续排查参考；不要在干净环境直接批量执行。

现行文档：`docs/deployment.md`、`docs/runbook-add-new-agent.md`、`docs/run-guard.md`。
