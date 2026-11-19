# 问题反馈：wit 2.1.0 `discovery_filter()` 默认构造已废弃的 `onlyAvailable` 条件，wt.ioa.pub 一律返回 400

> 建议投递：AIP-PUB 社区问题反馈（规则原文鼓励问题反馈）
> 分类：框架缺陷（wit）× 平台兼容（wt.ioa.pub 发现服务）
> 发现日期：2026-10-02 · 实测环境：wit 2.1.0 + acps_sdk 2.2.0 · CPython 3.13

---

## 1. 现象

Leader 侧的 `acps_discover` 工具（LLM 侧）与 `WitRuntime.aip_discover` /
`AcpsDiscoveryClient.discover`（代码侧）三条发现路径**全部失败**，平台返回：

```
HTTP 400 {"code":"invalid_parameter", ...}
```

导致 supervisor **发现不了任何 agent**，依赖发现的调度链路整体不可用。

## 2. 根因

wit 2.1.0 的 `wit.acps.discovery.discovery_filter()` 在 `include_unavailable=True`
（**这是它的默认值**）时会构造：

```json
{"conditions": [{"field": "onlyAvailable", "op": "eq", "value": false}]}
```

并放进发现请求体的 `filter` 字段。而 wt.ioa.pub 的发现服务**已整体废弃 `filter` 字段**：
实测**任何非空 filter 对象都返回 400**——连 `{"conditions": []}` 也拒；替代物是**顶层
`extFilter`**（`{"alive": true|false}` 均 200）。

连锁放大：LLM 工具 `acps_discover` 的参数 schema 里 `include_unavailable` 默认 true，
模型不显式传参时必然走默认值 → 必然构造出非法 filter → 必然 400。

## 3. 复现步骤

1. `AcpsConfig.ioa_pub(...)` 常规注册一个 agent（wt.ioa.pub 平台）；
2. 代码侧直调（不经 LLM，排除模型因素）：

```python
result = await runtime.aip_discover("文献检索", limit=5)   # → 400
# 或
client = AcpsDiscoveryClient(...)
client.discover("文献检索", limit=5)                        # → 同样 400
```

3. 抓请求体可见 `"filter": {"conditions": [{"field": "onlyAvailable", ...}]}`；
4. 手工把 `filter` 置 null / 删除该字段重放同一请求 → 200；
   换成顶层 `"extFilter": {"alive": true}` → 200。

## 4. 影响面

- 默认参数即触发（不是误用）：任何用 wit 2.1.0 对接 wt.ioa.pub 的 leader/消费者；
- `include_unavailable` 语义在平台侧已无对应实现（`onlyAvailable` 字段不存在了）。

## 5. 建议修复（上游任选其一）

1. `discovery_filter()` 默认返回 `None`（不带 filter——对任何平台版本都合法，最小改动）；
2. 或改用顶层 `extFilter: {"alive": <not include_unavailable>}` 表达同一语义。

## 6. 我们侧的临时补丁与回归（供参考）

业务侧 monkey-patch：`wit.acps.discovery.discovery_filter = lambda **_: None`
（`discovery()` 在调用时从模块全局取该函数，import 后重放即生效）。代价是放弃 wit 侧
`include_unavailable` 过滤（可达性在调用时天然验证，且关键调度本就走本地名册）。
回归测试：`acps_cluster/tests/test_discovery_patch.py`（默认参数/显式传值/自带 filter/
幂等重放四例），实测修复后 `aip_discover` 200 且发现的 agent 正常落
`<workspace>/acps/discovered/` 缓存。

---
*本反馈由研枢集群（AIP 委托授权与协作域扩展，见 RFC_delegation_domain_draft.md）对接
梧桐平台时实测发现；补丁已在生产路径运行。*
