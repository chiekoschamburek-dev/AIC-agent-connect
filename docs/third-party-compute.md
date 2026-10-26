# 第三方算力接入（GPU/CPU 属于算力公司）

> 2026-10-19 定案：GPU/CPU 是**算力公司的私有资源**，不是研枢自有节点。
> 研枢 supervisor 不是 token 签发方；GPU/CPU 不注册梧桐公开目录；`experiment`
> 作为已注册的公开入口，算力节点放在它后面。

## 1. 拓扑

```text
叮当
  → 梧桐发现 supervisor（已注册）
      → supervisor 调用 experiment（已注册，8461，exp.run 公开）
          → experiment 持算力公司签发的 grant
              → 算力公司私有 GPU / CPU（未注册梧桐）
```

- 不需要在梧桐注册 GPU/CPU；
- 若评委要求“梧桐里能看到算力节点”，只注册一个 **compute-gateway**，
  不要把 GPU/CPU 两个都注册；
- 不要让 `ACPS_PEP_LEADER_AICS` 包含研枢 supervisor，`ACPS_PEP_LEADER_DIRECT=0`
  必须由算力方设置。

## 2. 鉴权：算力方签发 Grant

算力方建域（`compute-gpu` / `compute-cpu`）：

- `members`: 算力方 provider + 研枢 experiment（consumer）；
- `issuers`: **只有算力方**；
- `assets`: `gpu.run` / `gpu.data.fetch` / `cpu.run`，`allowedMembers=["consumer"]`；
- 域章程双签：算力方 + 任一域当事方（研枢 supervisor 可作为 auditor 签章程，
  但**不签调用 token**）。

算力方用**自己的私钥**签发：

```python
issue_grant(
    provider_signing,
    domain="compute-gpu",
    domain_revision=1,
    subject_aic=<研枢 experiment AIC>,
    assets=[{"providerAic": <算力 GPU AIC>, "skill": "gpu.run"}],
    max_uses=...,
    ttl_seconds=...,
)
```

grant 的关键约束：

- `iss` = 算力方 AIC（不是研枢 supervisor）；
- `sub` / `aud` = 研枢 experiment AIC；
- `acps_domain` = 算力域；
- `acps_assets` = 目标算力 AIC + skill；
- PEP 校验 `iss ∈ domain.issuers`、`sub == request.leader_aic`、域成员角色、
  资产范围、配额、过期。

研枢 supervisor 只负责调度，不参与 grant 签名。

## 3. 消费 grant

experiment 通过安全带外通道读取 grant 文件：

```bash
ACPS_COMPUTE_GRANT_FILE=/secure/path/compute-gpu.grant.json
```

文件格式（0600，不进入模型上下文）：

```json
{
  "token": "<JWS>",
  "targetAic": "<算力 GPU AIC>",
  "skill": "gpu.run"
}
```

`partner_experiment.exp_run` 的令牌来源优先级已改为：

1. **算力方 grant 文件**（`ACPS_COMPUTE_GRANT_FILE`）；
2. leader remedy-3 的桥 ContextVar；
3. host 令牌文件；
4. `ACPS_EXP_GPU_TOKEN`（仅离线兜底）。

第三方算力模式下，只应走 1；不要用研枢 leader 自签的 `issue_delegation_token`。

## 4. 本地模拟

本地用 `ws-gpu` 模拟算力公司（provider 身份），`ws-experiment` 为 consumer：

```bash
cd my-project
python acps_cluster/deploy/make_compute_grant.py \
    --provider-workspace gpu \
    --consumer-workspace experiment \
    --domain compute-gpu \
    --max-uses 5 --ttl 3600
```

- 域章程写入 `ws-gpu/acps/domains/compute-gpu.r1.json`；
- grant 写入 `ws-experiment/acps/grants/compute-gpu.grant.json`；
- `iss` 是 GPU provider，`sub` 是 experiment；supervisor 不签 grant。

启动本地 GPU（PEP 必须开）：

```powershell
$env:ACPS_PEP_ENABLED="1"
$env:ACPS_PEP_LEADER_AICS=""
$env:ACPS_PEP_LEADER_DIRECT="0"
$env:ACPS_PEP_DOMAINS_DIR="$PWD\ws-gpu\acps\domains"
..\..\.venv\Scripts\python.exe partner_gpu.py --local
```

实验端：

```powershell
$env:ACPS_COMPUTE_GRANT_FILE="$PWD\ws-experiment\acps\grants\compute-gpu.grant.json"
python partner_experiment.py --local
```

## 5. 如果要在梧桐上暴露算力

只注册一个 **compute-gateway**（算力公司账号）：

- gateway 暴露 `gpu.run` / `cpu.run` 或统一的 `compute.run`；
- 后面接私有 GPU/CPU；
- supervisor 只发现 gateway；
- gateway 的 PEP 只信任算力方 issuer，不信任研枢 supervisor。

不要用共享 `pxyy` 账号注册 GPU/CPU；梧桐单账号单会话会把现有 supervisor/伙伴踢下线。

## 6. 作业 worker

`jobs_runner.py` 已固定 `ACPS_SUPERVISOR_PORT=8444`（`serve=False` 不绑端口），
避免 8544 与 enrollment 的 8444 不一致导致 `acps_config_changed_after_submission`。
