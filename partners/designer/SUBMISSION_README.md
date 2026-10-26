# Yanshu Research Designer 提交包

## 核心入口

- `src/research_designer.py`：智能体定义、工具与业务校验
- `src/server_acps.py`：ACPs Direct 服务入口和模型配置
- `src/amp_kafka_forwarder.py`：AMP JSONL 到 Kafka 的断点转发

## Kafka

- heartbeat -> `amp.heartbeat`
- access -> `amp.access`
- broker 使用部署方提供的 PLAINTEXT 地址
- 转发程序按行发送 JSONL，并在 broker 确认后记录读取位置

## 安全说明

本包不包含 API Key、注册密码、ACPs 私钥、证书、任务数据库、
运行日志或 Kafka offset 状态文件。部署时应从 `.example` 文件生成真实配置。

## 当前运行环境

- Python 3.12
- WIT Framework 2.1.0
- ACPs SDK 2.2.0
