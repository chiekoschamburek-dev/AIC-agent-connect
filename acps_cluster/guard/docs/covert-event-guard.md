# 三维事件通信风险检测器

本模块是只读、旁路的安全审计原型，参考 Covert Event Channel 威胁模型，从载荷、
时间和行为三个维度检查智能体任务事件。它不实现隐蔽通信，也不会自动阻断或修改任务。

## 快速演示

正常样例：

    python -m acps_cluster.covert_event_guard --input acps_cluster/examples/covert_guard_normal.jsonl.example

异常样例：

    python -m acps_cluster.covert_event_guard --input acps_cluster/examples/covert_guard_suspicious.jsonl.example

添加 --json 可输出机器可读报告。

## 检查真实数据库

数据库始终以 SQLite mode=ro 打开，兼容 task_ref/event_data 和
task_id/payload 两套已知字段：

    python -m acps_cluster.covert_event_guard \
      --db /path/to/acps/aip-tasks.sqlite3 --task-ref task_xxxxxxxxxxxx

不指定任务时按最近事件分别报告：

    python -m acps_cluster.covert_event_guard \
      --db /path/to/acps/aip-tasks.sqlite3 --limit 500

## 规则和分级

- storage：超大载荷、长编码高熵数据、重复载荷。
- timing：过度规则的时间间隔、稳定长短交替模式。
- behavior：状态倒退、终态后恢复、机械式事件类型切换。
- 0–29 LOW，30–59 MEDIUM，60–100 HIGH。

检测结果只用于筛查，不是隐蔽通信存在的证明。生产处置需结合任务语义和人工复核。

## 测试

    python -m unittest acps_cluster.tests.test_covert_event_guard -v

## 安全边界

- 不读取或输出 API Key、令牌和私钥。
- 不执行载荷中的代码，不写任务数据库。
- 不自动调用 PEP、冻结协作域或撤销委托令牌。
- 正式接入前应使用历史任务校准阈值并记录误报率。

## 跨任务 Timing 检测（V2）

真实 ACPs 数据中每个任务通常只有 3–5 个事件，因此单任务时间序列不足。V2 从
aip_tasks 读取通信双方，按 direction、local_aic、remote_aic 分组，并以 3600 秒空闲
间隔切分会话：

    python -B -m acps_cluster.covert_event_guard \
      --db /path/to/acps/aip-tasks.sqlite3 \
      --peer-timing --limit 1000

机器可读输出：

    python -B -m acps_cluster.covert_event_guard \
      --db /path/to/acps/aip-tasks.sqlite3 \
      --peer-timing --json

规则边界：

- 每个会话少于 4 个有效间隔：INSUFFICIENT_DATA。
- 4–5 个有效间隔：只观察、不计分。
- 至少 6 个有效间隔：允许进行 Timing 评分。
- CV 不超过 0.10：过度规则，增加 20 分。
- 稳定长短交替：增加 25 分。
- 小于 1 秒的间隔只记为 operational_burst，不计安全风险。
- Peer Timing 评分 20–39 为 MEDIUM，40 及以上为 HIGH。

这些阈值来自首轮 140 个真实任务的基线观察；正式部署前仍需持续复核误报率。
