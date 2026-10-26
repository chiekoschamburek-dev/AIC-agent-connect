# 研枢:智能体互联的受治资源协作网络(AIC 智能体互联项目)

> 依据 GB/Z 185《人工智能 智能体互联》与 ACPs(Agent Collaboration Protocols)协议体系,
> 在梧桐/wt 平台构建"研枢"科研多智能体集群(1 supervisor + 名册 16 个伙伴条目,磁盘 16 个 partner 模块——含本地资源节点、workspace 存储实例与已注册的 citation),
> 从"能用协议"走到"治理协议":让 GPU 算力、评测数据、知识库、算法模型成为
> **可发现、可授权、可计量、可撤销、可审计**的协作资源。

创新点共三项:任务级的 PEP 委托授权(点对点)、资源封装成 agent(被委托的东西)、
域的构建(授权关系的组织方式——并自然引出能力可见范围与三层能力)。

---

## 一、系统总览

```
机构 B(消费方,研枢集群,平台身份)          机构 A(资源提供方,本地)
  supervisor(总控,自主调度)                edge-leader(管理 leader)
  scout/synthesis/writer/critic            ├─ gpu-partner(8460,沙盒+GPU)
  experiment(域内 consumer)                ├─ cpu-partner(8462,沙盒无卡)
        │                                  ├─ eval-partner(8464,评测服务)
        └──── 双签域章程(合同对象) ────────┤
                                           └─ model-partner(8467,算法托管)
```

| 资源 | 执行底座 | 防泄露/隔离特性 |
|---|---|---|
| 算力-GPU | Docker 八条硬边界沙盒 | 跨 CA 云端可达(反向隧道+寻址桩) |
| 算力-CPU | 同一沙盒,use_gpu 参数化 | 与 GPU 同强度 |
| 数据-评测 | host 侧打分 + 受限沙盒读隐藏特征 | 防泄题三闸门 |
| 数据-知识库 | 受限查询 DSL(无代码执行面) | 行级过滤 + 字段级三档脱敏 |
| 算法 | 进程内推理 | 权重不出域,输入域/配额校验 |
| 工作区 | workspace partner(8470,任务级目录) | task_id 白名单+路径逃逸拒绝+配额;ws.* 非公开,凭令牌且限本任务作用域 |

---

## 二、创新点

### 创新 1:任务级 PEP 委托授权——把 AAC 第 10 册"从纸面变代码"

AAC 定义了委托语义,官方 SDK **零实现**。本项目零侵入 wit/acps_sdk(仅用官方扩展点)
补齐完整链路:

- **三级准入 L0/L1/L2**:public 免审(保住社区互联线)/ 白名单(leader 固有权利)/
  令牌(第三方)。裁决普适、放行有据、fail-closed——门的是"卡、库、权、分",不门"文";
- **Ed25519 JWS 委托令牌**:iss/sub/aud/act 信任链/jti/scope,令牌对模型全程不可见
  (剥离钩子+防伪造标记);
- **jti 句柄与授权隧道(remedy-3)**:签发工具只返回句柄(返回树断言不含 "token"),
  JWS 锁进程内保管库,`delegate_continue` 向**运行中任务**注入令牌,capability 桥接
  ContextVar 供业务能力一行出站。量化(demo_fanout,真实 mTLS):较 leader 中转
  **2.94× 加速**、leader 上下文材料 **196KB→0**、并行度 1→3;
- **修复真实越权漏洞**:初版实现允许任意社区成员自签 `iss=leader` 令牌通过全部校验
  (签名证"谁签的",没证"有权签")——补签发者授权检查并回归。

**验证**:102 项单测(test_delegation,2026-10-21 实测);S1 漏洞复现→修复→16/16 场景;云端自主 remedy-3 实弹
(supervisor 签发 jti=dccf1cb9 → 桥接直达本地 GPU 真训练)。

### 创新 2:资源封装成 agent——按风险定制执行底座的五实例

把 AI 生产三要素各自封装为协议可见、凭证准入、全程审计的资源 agent,
**每个实例的执行底座按其资产的风险定制**,而非一个沙盒套所有:

| 实例 | 资产 | 定制防护 |
|---|---|---|
| gpu-partner | 算力时间 | Docker 八条硬边界(白名单/断网/只读FS/非root/限额/时限) |
| cpu-partner | 算力时间 | 同一沙盒,`use_gpu` 由代码侧决定(模型不可自选隔离级) |
| eval-partner | 评测数据 | 防泄题三闸门:标签永不进容器/stdout 管制/产物不回传;8% 噪声确定性基准(满分不可达);渗透演示三路全挡且渗透者正常得分(**防泄题≠拒服务**) |
| kb-partner | 私有语料 | 受限查询 DSL(无任意代码执行面)+ 行级过滤 + **字段级三档脱敏**(open/members/curator,越权字段静默丢弃并在 meta 列明) |
| model-partner | 模型权重 | 权重不出域只暴露推理;输入域/批量校验;真实权重热替换位 |

**验证**:各伙伴专项单测(全量 245 项);E 系列 9/9;域 grant 直调 eval.submit 真打分入榜、
kb.query 以 consumer 视角实测 curator 档零值泄露。

### 创新 3:域的构建——机构间授权成为协议对象,自然引出可见范围与三层能力

单任务的令牌(创新 1)回答"这一次能不能";域回答"**一段关系怎么管**":

- **双签章程**:成员(provider/consumer/auditor)、签发者、资产表(谁的哪个 skill 对哪种
  角色开放)、配额、revision、状态;**修订权=创建权**(防单方 bump 踢人);
- **GrantToken 与派生继承**:长期凭证(≤30 天)派生短时令牌强制继承
  (`skill∈父资产`、`exp≤父`),并把 `parent_jti/parent_exp/parent_max_uses` 与
  **父 grant JWS** 写进子令牌;PEP 重验父签名/主体/资产并继续扣父配额,
  自报数字不能伪造配额;普通令牌不能调域技能;**双配额**
  (签发侧/使用侧,按 provider 各自记账);
- **冻结即急停**:验签**之前**查域状态(不 fail-open),杀死一切域派生授权含在途令牌;
  revision 升版驱逐旧令牌,跨重启持久;**双签对账**让用量从日志变可仲裁证据;
  语义边界落档:**治理约束 Guest,不约束房东**(provider 白名单不受域冻结影响);
- **自然推论一:能力可见范围**(Phase D):域成员资格决定资产在发现层是否出现——
  `visibility=domain` 仅域成员可见、匿名只见 public、**agent 无可见 skill 则整体不出现
  (存在性不泄露)**;响应内 ACS 体同步脱敏;本仓已有同语义纯函数(`delegation.visibility`)
  与 ACS 透传(registry schema 接受 `visibility/domains`),平台侧由 discovery-server 执行;
  定调为元数据层收窄(访问控制永远在 PEP)。若某个新 agent 的能力全部是 domain/非公开，
  用 `agent_profile.make_agent_info` 加一个 public `agent.info` 作为可发现锚点；
  现有已注册 agent 都已有 public 非 domain 能力，不新增、不改 ACS。
- **自然推论二:三层能力声明**:`public`(橱窗,引流)/ 非公开(保险柜,凭证准入)/
  `visibility=domain`(会员制,域内可见)——三层是运营决策,改声明即翻转,零代码改动。

**验证**:域/修订链/派生硬化/可见性离线回归 62 项(test_domain 31 + hardening 16 +
visibility 15);域×真 GPU G1–G4(allow→配额拒→冻结拒);两机构双边域 S1–S5;
Phase D 上游 20 项单测(含 ACS 体脱敏)并可独立运行。

---

## 三、验证形态(创新点的实证,不另计创新)

- **纯本地**:三阶段复现(Phase A 干跑编排 → Phase B 真训练 → Phase C supervisor 自主
  调度 58 步/73 分钟,含自主 remedy-3);
- **纯云端**:平台身份 supervisor 软链路多次实跑(41 步、review APPROVE 85 分);
- **云端×本地混合**:跨信任链(平台 CA + dev CA 并存)+ 反向隧道 + 域外令牌,本地 GPU
  沙盒真训练 exit 0(两次:env 通道与桥接通道各一);
- **两机构**:本地算力提供方(edge-leader + GPU/CPU)× 研枢集群,双边章程五场景全过;
- **诚实性**:实验被证伪如实报 `refuted`、三审 REVISE 不放行、伙伴拒绝编造结果;
  云端首跑暴露的 4 个真实缺陷(信任链缺证书/EnvironmentFile 占位符覆盖/中间证书缺失/
  桥接标记遮蔽)全部定位闭环——失败时的行为和成功时一样可信。

## 四、与标准及开源社区的关系

- **落地**:GB/Z 185 七部分架构在科研场景的完整实践(AIC/ATR/ADP/AIP);
- **补齐**:AAC 第 10 册委托语义的参考实现(含与设计文档的 11 处偏差记录);
- **反哺**:RFC 草案《AIP 委托授权与协作域扩展》(拟提交 AIP-PUB);
  discovery-server 域可见性 diff 已按上游风格提交;wit 2.1.0 缺陷(filter 400、
  Py2 风格 except)修复成文回馈。

## 五、量化指标

| 指标 | 数值 |
|---|---|
| 离线单测 | **757 项全绿 + 4 项跳过**(2026-10-26 Windows 本机复跑,0 失败;跳过项=Docker 不可用 1 + Windows 无 symlink 特权 3;以 `python run_all_tests.py` 实测为准。以下分组数字为 2026-10-18 基线;含 2 项依赖 Docker 的 GPU 集成用例[容器就绪/销毁已轮询收敛]、`test_seed_offline_fallback` 8 项[预热离线兜底与缺项可观测性]、对账与归属 11 项[ReconciliationTest 9:窗口按 ts 生效/缺ts 不静默/异构 provider/身份谱系/无域哨兵/技能证据等级分档/历史行按 hint 推断/历史行归桶/双签;+ PEP 审计归属 2:哨兵落盘、三档 skillSource]、`test_arg_shapes` 11 项[工具入参数组宽容化]、`DelegationTargetGateTest` 8 项[委派点标量闸门:编造伙伴/错派能力/未注册/发现缓存/硬隔离]、`RouteBLivenessProbeTest`+`EndpointReachableHelperTest` 9 项[路线 B 端点探活:探不到拒/探得到放/注册成员不探/覆盖表缺失不探/非回调容错/空集合语义/回调收到端点/探活实现本身];跳过数随环境变化——依赖本机 Docker 可用性与 dev 工作区铸造状态;`UnclosedDeliverablesTest` 5 项[run 收尾代签 aip_complete:只扫本次 run 且带产物的出站行/历史遗留不动/非正常收尾不代签/拿不到 runtime 不猜/关单异常只记日志];`PublicDelegateToolTest` 11 项[公开委派纯标量工具 acps_delegate_public:载荷代码侧组装/key与展示名解析为权威AIC/受保护能力拒绝并指路/受保护伙伴缺skill_id不猜/资源伙伴公开能力放行/编造伙伴与错派能力被闸门拦下/缺参/桥未就绪/传输层异常转结构化结果/运行配置已注册本工具];含 P0 安全专项 13 项。手写数字易漂移,以 `python run_all_tests.py` 实时输出为准。**注**:`tests/test_resource_agent_authz.py` 是脚本式验收(main,无 TestCase),`unittest discover` 计 0 项,不计入上面数字。**计数方式**:`tests/` 无 `__init__.py`,`unittest discover -s acps_cluster/tests` 会报 "Start directory is not importable",757 项按模块清单枚举 `python -m unittest acps_cluster.tests.test_*` 实测得到) |
| 调度闸门位置 | 2026-10-16 起 `plan_gate` 由硬前置降为**可选自检**;同一批约束(伙伴必须真实存在、能力必须真由其声明、AIC 取权威值)下沉到委派点 `cluster_roster.check_delegation_target`,不通过则拒绝委派且不签发令牌。**动机**:现网实测模型提供方偶尔把 array 参数丢空,硬前置会让整段编排卡在闸门里(标量参数的委派点不受影响)。ACS 声明文案受指纹保护未改,故"代码侧闸门"的承诺改由委派点兑现。**同一动机的第二处收口**:内置 `acps_delegate` 的 `request` 是数组入参,丢空时框架在参数校验阶段就报 `AipError: Invalid high-level content input`(durationMs=0,请求根本没发出)→ 新增 leader 工具 `acps_delegate_public(partner_aic, skill_id, request)`(三个入参全 string,Part 列表在代码侧组装),同样先过委派点闸门、并**拒绝**受保护的非公开能力(指路 `acps_delegate`,代码侧自动附令牌);playbook 已改为让 leader 用公开工具发公开能力。**路线 B 收口(定案=探活,不做 provenance 标记)**:不在平台注册集合(覆盖表)里的成员即本地 dev 身份 + 反向隧道,其 `available` 只表示"解析得出 AIC",隧道未起时放过去的后果是传输层 ConnectError + 账本非终态垃圾行 + 白烧一轮;故委派点对其端点做 TCP 探活(`supervisor._endpoint_reachable`,0.8s)。两个"不探"边界:平台注册成员不探(存活由心跳反映、mTLS 握手自会暴露不可达)、覆盖表读不到时一律不探(不能因缺配置文件卡住所有委派) |
| 演示场景 | PEP 16/16、域 11/11、商业域 10/10、知识库 9/9、评测 9/9、GPU G 系列 7/7、域×真GPU 4/4、两机构 5/5 |
| DeepResearch 真实业务 | 六阶段闭环:9 篇真实文献检索→萃取→核验前置→11575 字符结构化综述→审稿对账;contradiction 缺失按纪律诚实降级 |
| AutoResearch 真实成果 | 真 AIP 链路 18 轮收敛:leader→experiment→gpu(容器)→eval 全程走协议与委托令牌,**账本出现 `verdictSource=host-score`**(hostScore=0.9 ≥ 判据 0.85,宿主侧精确复算 0.9167/55-60),事后对账 `mismatches=[]`;基准 `demo-cls300sup`(容器内 train 240 行带 label / test 60 行无 label,测试标签不出宿主),`predictions.csv` 内容 sha256 `2e467f652f9f` 在第 12/13/14/16/18 轮逐字节一致;第 14/16 轮对账曾抓到"宿主 supported / 账本 inconclusive"的漏报,第 17 轮拿不到独立分数时如实记 `inconclusive` 且拒编数字。离线直调版(e2e)2 轮收敛同分 0.9167。详见技术报告附录二（五） |
| 引用核验闭环 | 3 篇真实 arXiv 取源全成功(unverified=0):supported/misrepresented/fabricated 三态判定全部正确;review_gate 对账拦截漏报 |
| 委托隧道加速 | 2.94×(脚本版)/1.73×(真实 LLM 版);leader 上下文 196KB→0,并行度 1→3 |
| 资源实例 | 5 类底座 6 实例(GPU/CPU/评测/知识库/算法/HPC 网关);三档能力声明(public/橱窗/域内) |
| 云端实跑 | 平台身份 41 步 APPROVE;云端×本地 GPU 训练 exit 0×2 |
| 迭代 | 28 轮(含 4 轮外部审查修复轮),产物与审计逐轮可追溯 |

## 六、已知限制(诚实清单)

- 域冻结传播延迟:文件形态章程生效=对端重载,最坏窗口=min(存量派生令牌剩余 TTL, 分发延迟);
- 域可见性身份二选一:默认 fail-closed(无可信身份源→域资产对所有人隐藏);仅显式设
  DOMAIN_VISIBILITY_TRUST_CALLER_METADATA=1 才信任未签名的 callerAic 元数据,该模式
  不是访问控制。目标形态仍是 mTLS SAN/签名令牌提取;策略缓存按文件 mtime 自动刷新。
- 发现层"先 limit 后过滤"以 3× 超取缓解,正解为谓词下推进 SQL;
- 配额计数器跨进程竞态:单连接事务在多进程并发下存在 COUNT→INSERT 竞态
  (生产形态需 BEGIN IMMEDIATE 或单写者服务;已在 RFC §7 声明);
- leader-LLM 长编排步数耗尽:DeepResearch 六阶段在单次 run 内步数紧张
  (wait_partner 已缓解等待轮询;跨 run 状态/检查点恢复为 wit 侧待力);
- cpu/eval/model/kb 等本地实例未上平台(dev 身份,路线 B)。
- AutoResearch 凭据算术未解:令牌 TTL 上限 600s 且 jti 单次使用,与"流水线内重试 + 长循环"
  天然冲突(第 18 轮 wall 5358s 内两次 `deny invalid-token`)。正解=每次出站现签 +
  会话内有界复用(`requireSessionBinding` 那条路),不是把 TTL 调大。
- 作业化交路本机无法验证:`jobs_runner` 起的后台进程需 registry 凭据,本机 4-5 秒即
  `needs_login` 失败(第 8/9 轮实测)→ 长任务改道的收益只能在现网证实。
- 网关 429(配额)会让整轮异常终止而非收敛为"可交付的失败"(agents.py 响应看门狗只管总时长,
  第 15 轮即因此丢summary);缺一条"配额失败→如实产出失败产物"的降级路径。
- 宿主侧评测目录可被同机其他会话改写:`eval-private/demo-cls` 曾被改成 5 行导致评分错位,
  现靠评分前逐字节 sha 校验拦截(并改用独立 id `demo-cls300sup`),属规避而非治理。
- wit 2.1.0 预证书对 IP 端点不写 IP SAN:IP 直连形态的 agent 无法走平台签发
  (server_public_endpoint_san_mismatch,三种配置复现,证据见
  docs/issue-wit-2.1.0-ip-endpoint-san.md);workspace partner 注册挂起于此,
  现以路线 B(本地 CA 身份)在集群内服务;
- 10-23 曾发生部署覆盖服务器侧计量钩子事故(榜单清零一天),已恢复并固化
  部署前门禁(deploy/discovery_diagnostics/scan_server_only_patches.py),
  复盘见 docs/incident-2026-10-23-amp-hooks-lost.md。
- 伙伴工作区访问的粒度限制(2026-10-26 上线):一次委派只签一个 `ws.*` 令牌
  (`ws.create` 与 `ws.clone_repo` 要分两次委派);伙伴令牌 TTL 上限 600s,超过
  10 分钟的任务在末尾调 `ws_*` 会拿到"令牌已过期"的 awaiting;bundle 令牌文件
  只读不删(过期条目读取时过滤,暂无清理 timer);supervisor 自身最终产物不再
  自动归档,需要时由编排显式 `ws.write`。验收与外发记录见
  docs/server-rollout-2026-10-26.md。

## 七、证据索引

| 主题 | 路径 |
|---|---|
| 委托授权层设计/实现/迭代记录 | `../PEP_design.md`、`../PEP-result.md`（在父目录，与 `my-project/` 同级） |
| RFC 草案 | `../RFC_delegation_domain_draft.md` |
| 三阶段复现 | `acps_cluster/demo_run/repro-phase{A,B,C}/` |
| 域×真 GPU / 两机构 | `acps_cluster/demo_run/{domain-real-gpu,two-org-domain}/` |
| 云端×本地混合 | `acps_cluster/demo_run/cloud-gpu/`、`acps_cluster/demo_run/cloud_run{4,6,8,9}/` |
| 评测/知识库/投影 | `acps_cluster/demo_run/{eval,kb}/`、`acps_cluster/policy-projections/` |
| 委托隧道量化 | `acps_cluster/demo_run/fanout/fanout-report.json`、`acps_cluster/demo_run/fanout/real/fanout-real-{report.json,结论.md}` |
| DeepResearch/AutoResearch | `acps_cluster/demo_run/deepresearch/`、`autoresearch/`(真链路: `ledger-<runId>.v1.jsonl`、`gpu-audit.jsonl`、逐轮 `run-20261018-round*.log` 与 `autoresearch-prev-*` 归档;离线直调: `autoresearch-e2e.json`) |
| 引用核验端到端 | `acps_cluster/demo_run/citation/`(verdict 表+真实取源记录) |
| L2 作业化交路(受理-作业-取件) | `docs/l2-job-pipeline.md`、`acps_cluster/tests/test_jobs_store.py` |
| wit 缺陷反馈素材 | `docs/issue-wit-2.1.0-discovery-filter-400.md` |
