# Research Designer 改走北邮 wt 平台（交接说明）

给 gjw：现在这条注册排在**官方梧桐 www.ioa.pub**的人工队列里，北邮那套 **wt.ioa.pub** 是另一套平台、审核快得多。下面是把 agent 迁到 wt 的最小改动。所有命令都标注了「在哪台机器、用哪个用户」执行。

## 一、为什么要换（实测依据）

| 平台 | 谁的记录 | 提交时间 | 结果 |
|---|---|---|---|
| 官方梧桐 `www.ioa.pub` | 我们的 arXiv 伙伴 `arxiv-parnter` | 2026-09-21 16:35 | **到 10-02 仍是 PENDING（11 天）**，aic 为 null，不发证书 |
| 官方梧桐 `www.ioa.pub` | 你的 `Yanshu Research Designer` | 2026-09-29 21:40 | PENDING，同一批慢队列 |
| 北邮 `wt.ioa.pub` | 我们的 6 个伙伴 + supervisor | 2026-09-23 深夜 | **当晚全部 APPROVED**，09-24 00:29-00:34 自动领证上线（8445-8450） |

审核是平台外部的人工行为，开发者侧没有任何加速办法。所以同一份材料，换队列是最有效的动作。

## 二、要改的三件事

### 1. 平台地址：`ioa_pub` 预设 → `custom`（改 `server_acps.py` 一个函数）

【服务器 · gjw · 目录 `/home/gjw/research-designer`】把 `build_server_acps_config()` 整体替换成下面这版（只把 `AcpsConfig.ioa_pub(` 换成 `AcpsConfig.custom(` 并加三个地址参数，其余不动）：

```python
def build_server_acps_config() -> AcpsConfig:
    public_endpoint = _required("ACPS_PUBLIC_ENDPOINT")
    if not public_endpoint.startswith("https://"):
        raise SystemExit("正式 ACPs endpoint 必须使用 https://。")
    return AcpsConfig.custom(
        registry_url=_required("ACPS_REGISTRY_URL"),
        ca_url=_required("ACPS_CA_URL"),
        discovery_url=_required("ACPS_DISCOVERY_URL"),
        public_endpoint=public_endpoint.rstrip("/"),
        provider=AgentProviderProfile(
            organization=_required("ACPS_ORGANIZATION"),
            country_code=os.getenv("ACPS_COUNTRY_CODE", "CN"),
        ),
        certificate=_certificate_request(public_endpoint),
    )
```

### 2. `.env.server`：加三行平台地址 + 换账号 + 换工作区名

`ACPS_PUBLIC_ENDPOINT` / `ACPS_BIND_PORT=8590` / `ACPS_ORGANIZATION` / 模型那几行**都不用改**。要动的是：

```bash
export ACPS_REGISTRY_URL="https://wt.ioa.pub/registry-server"
export ACPS_CA_URL="https://wt.ioa.pub/ca-server"
export ACPS_DISCOVERY_URL="https://wt.ioa.pub/discovery/acps-adp-v2/discover"
export ACPS_USERNAME="你在 wt 平台的新账号"
export ACPS_PASSWORD="（直接在服务器上填，不要发到聊天里）"
export WIT_WORKSPACE=".wit/scinexus-research-designer-wt"
```

三点说明：

- **账号必须是 wt 平台新注册的**，控制台入口 `https://wt.ioa.pub/registry/#/my-agents/index`，文档在 `https://wt.ioa.pub/flux/docs`。
- **绝对不要用 `pxyy`**（那是我们 6 个 agent 在用的账号）。wt 平台是**同账号单会话**：你一登录就把我们服务端的注册会话踢掉，一起进程就会互相踢 token、只有最后登录的那一个能走完注册。
- **`WIT_WORKSPACE` 一定要换名**（这是最容易踩的坑，见下一节）。

### 3. 端口不用配

`8590` 的阿里云安全组和主机 firewalld **早就打通了**（2026-09-23 从外网实测过 TCP 可达），你不需要再加任何规则。也别改端口——我们的 8443-8450 都在这台机器上。

## 三、为什么工作区必须换名（不能原地改平台）

`.wit/scinexus-research-designer/acps/` 里的 `enrollment.json` 和 `secrets/registry-token.json` 是**官方梧桐那条记录的状态**。wit 的恢复逻辑是：本地状态在 + token 文件在 → 直接对**当前配置的 registry** 做只读续跑（`GET /agent/client/{id}`）。你把 registry 换成 wt 却留着这份状态，它就会拿官方那条的 id 去 wt 查 → 查不到 / 401，看起来像"注册失败"，其实是状态串台。

正确做法（【服务器 · gjw】，旧状态整目录挪走、**不要删**，官方那条万一哪天批了还能用）：

```bash
mv /home/gjw/research-designer/.wit/scinexus-research-designer /home/gjw/research-designer/.wit/scinexus-official-pending-20261002
```

顺手清理一个垃圾目录：`.wit/` 下有个 `scinexus-research-designer:wq`（是 vim 的 `:wq` 打进了 shell 生成的），先 `ls -la /home/gjw/research-designer/.wit/` 看清名字再删，**别用通配符**，因为真工作区名字和它只差后缀。

## 四、执行顺序（【服务器 · gjw · `/home/gjw/research-designer`】）

```bash
source .venv/bin/activate
python -m unittest discover -s tests -v
source .env.server
python server_acps.py draft
```

`draft` 的输出先自查两件事再往下：`endPoints[0].url` 是 `https://192.0.2.10:8590/rpc`、`certificate.altNames.ip` 是 `["192.0.2.10"]`（这两项你之前在官方上已经填对了，换平台不该变）。

改完代码/配置后先扫一遍隐形字符（复制粘贴踩过：URL 里夹一个 U+200B 会让 registry 报 `INVALID_ACS endpoint URL format`）：

```bash
grep -nP "[\x{200b}-\x{200f}\x{feff}]" /home/gjw/research-designer/.env.server /home/gjw/research-designer/server_acps.py; echo scan_rc=$?
```

`scan_rc=1` 是好事（没命中）。然后提交：

```bash
python server_acps.py enroll
```

期望停在 `pending_review`。**之后就等，不要反复 enroll** —— 平台对 PENDING 状态的记录拒绝更新（会报 `Agents in PENDING status cannot be updated`），反复提交只会把自己打进 `blocked`。也别删 `secrets/registry-token.json`（token 一小时过期，wit 自己会重新登录）。

## 五、审核通过后

批准**不会自动上线**，得你自己起：

```bash
python server_acps.py status
nohup python server_acps.py serve > /home/gjw/research-designer/serve.log 2>&1 &
```

**关键：启动前必须 `cd /home/gjw/research-designer`**。`WIT_WORKSPACE` 是相对路径，从别的目录启动会在别处再长出一个工作区，等于重新注册一次。

先确认两件事：`ss -tlnp | grep 8590` 有监听；再从你本地 PowerShell 用 `Test-NetConnection 192.0.2.10 -Port 8590` 打一次（返回 True 才算公网链路通）。

跑顺之后再换常驻方式：这台机器**已经给你开了 systemd 用户实例的 linger**（`loginctl enable-linger gjw`，09-24 端到端实测过），用 `systemctl --user enable --now <你的unit>` 可以做到免 root、开机自启、崩溃自动重启，比 `nohup` 可靠（`nohup` 进程重启机器就没了）。

证书不用管：批准后 wit 自动向 `wt.ioa.pub/ca-server` 走 ACME 领，有效期约 49 天、到期前自动续。私钥全程本地生成。

## 六、如果他还要委派给别的 agent（一条已知故障）

wt 平台的 discovery 接口移除了 `onlyAvailable` 字段，而 wit 2.1.0 的客户端还在发这个字段 → 平台恒返回 `400 FilterInvalid` → `acps_discover` 永远失败 → 委派时报 `Partner has no discovered Direct JSON-RPC endpoint`。我们的绕法是预热一个本地 discovery 缓存（`deploy/seed_discovery.py`，把各伙伴的 ACS 直接写进 `.wit/<leader>/acps/discovered/`）。只被委派、不出向调用的话不受影响。

另外提醒一句：**平台上"审核通过"不等于"在线"**。我们在社区里发现过别人家已过审的 agent 端点根本连不上，还有一个只有 amqps 端点的（Direct 协议对不上）。关键链路不要依赖语义发现，直接把对方 AIC 写死。
