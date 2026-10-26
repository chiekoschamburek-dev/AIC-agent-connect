# Research Designer 服务器部署

目标环境：Alibaba Cloud Linux 3、Python 3.12、普通用户 `gjw`、端口 `8590`。

## 1. 解压并创建虚拟环境

```bash
cd /home/gjw
unzip research-designer-deploy.zip -d research-designer
cd research-designer
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install ./wheels/acps_sdk-2.2.0-py3-none-any.whl
python -m pip install ./wheels/wit_framework-2.1.0-cp312.cp313.cp314-none-any.whl
```

## 2. 配置环境变量

```bash
cp .env.server.example .env.server
chmod 600 .env.server
```

使用 `vi .env.server` 编辑配置。不要把真实密码或 API Key 发到聊天或提交到仓库。

加载配置：

```bash
source .env.server
```

`ACPS_PUBLIC_ENDPOINT` 填写 endpoint 基地址，不要添加 `/rpc`。例如
`https://8.160.176.215:8590` 会在 ACS 中发布为
`https://8.160.176.215:8590/rpc`。

## 3. 本机校验与注册

```bash
python -m unittest discover -s tests -v
python server_acps.py draft
python server_acps.py enroll
```

如果注册进入平台审核，等待审核通过，不要反复创建注册。

## 4. 审核通过后启动

```bash
source .venv/bin/activate
source .env.server
python server_acps.py status
python server_acps.py serve
```

首次启动先保持终端打开。确认远程访问正常后，再配置普通用户可用的持久化运行方式。
