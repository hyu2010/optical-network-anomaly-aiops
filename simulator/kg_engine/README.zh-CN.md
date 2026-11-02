# src/kg_engine — 实时知识图谱引擎

## 它做什么

`src/kg/` 的流式对应物。`src/kg/builder.py` 把整个 `incidents.db` 从头到尾扫一遍、一次性写出约 900 万条三元组到一个 CSV；本模块则把同样的场景**增量地**消费——每条告警事件产生一份三元组增量,并维护内存里的 KG、磁盘上只追加的事件日志、以及（可选的）批量产物的实时 CSV 镜像。

一句话总结:它把外部事件（新告警、YANG 拓扑变更）实时翻译为 KG 三元组增量,并广播给一切想响应的消费者——规则引擎、SSE Web UI、磁盘上的 CSV 镜像,以及未来的 GNN 推理服务。

具体而言,引擎处理四类输入:

1. **来自模拟器（将来是 NMS 流）的新告警** —— 发每条告警的位置三元组 + 每个事件的标签 / 局部性 / 时间三元组。
2. **来自 YANG 文件编辑（将来是 NETCONF）的拓扑变更** —— 把新拓扑与活跃三元组索引做差分,并相应发出新增 / 删除三元组。
3. **滑动窗口事件分组** —— 在 6 分钟窗口内,按 `Design/08` 的四条 R 规则（R1–R4）决定哪些告警属于同一事件。
4. **快照 / 恢复** —— 每 60 秒把状态 pickle 到磁盘,重启时毫秒级恢复,无需回放完整事件日志。

- **设计文档**：`Design/10_realtime_kg_engine.md`（英文）/ `.zh-CN.md`（中文）
- **Schema**：与批处理共用 `src/kg/schema.py`（Triple 形状、URI 方案、谓词名——全部不变）
- **当前状态**：
  - **R-1 完成** —— 与批处理构建器达成流式对等（500 个场景下 210 万条三元组完全匹配）
  - **R-2 完成** —— Pickle 快照 + 恢复；YANG 文件监视器发出 `topology.snapshot` 事件；拓扑三元组落入 state
  - **R-3 完成** —— 滑动窗口事件分组（R1–R4）；规则引擎订阅者发出 `source="rule:fingerprint"` 的 `RootCause` 三元组（烟雾测试中与仿真器真值 20/20 一致）
  - **R-4 完成** —— SSE 订阅者 + 一个自托管演示 Flask 应用 `web.py`（`python -m src.kg_engine web`）
  - **实时 CSV 镜像完成** —— `CsvMirrorSubscriber` 在每个 `incident.boundary` / 拓扑变更时,把 `data/kg/{topology,alarm}_triples.csv` 与 `incident_manifest.csv` 与引擎状态同步。`web` 默认开启；在 `run` 中通过 `--mirror-csv` 显式启用。
  - **R-5 已搭脚手架** —— `adapters/netconf.py`、`adapters/alarm_stream.py` 是占位文件，含实现纲要；等待具体部署目标
  - **R-6 已编写文档** —— 性能目标、热点、修复方案、测试计划见 `docs/04_kg_engine_perf_plan.md`；等待真实负载数据

---

## 架构总览

```
                        适配器                          引擎                          订阅者
                  ┌─────────────────────┐   ┌──────────────────────────┐   ┌──────────────────────┐
 incidents.db ──▶ │ SimulatorAdapter    │   │                          │   │ EventLogSubscriber   │ ──▶ events.ndjson
 data/yang/ ────▶ │ YangWatchAdapter    │ ─▶│  asyncio.Queue           │   │ RuleEngineSubscriber │ ──▶ 进程内 RootCause
 （R-5：NETCONF/  │ NetconfAdapter      │   │       │                  │ ──▶│ SSESubscriber        │ ──▶ HTTP SSE 客户端
 告警流）         │ AlarmStreamAdapter  │   │       ▼                  │   │ CsvMirrorSubscriber  │ ──▶ data/kg/*.csv
                  └─────────────────────┘   │  在锁内派发               │   └──────────────────────┘
                                            │       │                  │
                                            │       ▼                  │
                                            │  handlers/{alarms,       │
                                            │           incidents,     │
                                            │           topology,      │
                                            │           grouping}.py   │
                                            │       │                  │
                                            │       ▼                  │
                                            │  Delta(adds, removes)    │
                                            │       │                  │
                                            │       ▼                  │
                                            │  StateStore.triples      │
                                            │ （TripleIndex + 按 subject/│
                                            │   object/predicate/      │
                                            │   incident 的索引）       │
                                            │       │                  │
                                            │       ▼                  │
                                            │  每 60 秒做快照 ─────────▶│ ──▶ snapshots/*.pkl
                                            └──────────────────────────┘
```

三层——适配器、引擎、订阅者——刻意保持解耦：

- **适配器** 把外部世界（文件监视器、DB tail、NETCONF）翻译成统一的 `Event` 流。新增一个来源 = 在 `adapters/` 加一个文件。
- **引擎** 拥有内存中的 KG（`StateStore`）与派发循环。Handler 是纯函数 `(event, state) -> Delta`，易测易扩展。
- **订阅者** 在每个 `Delta` 被应用后消费它。它们从不修改状态，只做响应。新增一个输出通道（新数据库、新 webhook、新指标计数器）= 在 `subscribers/` 加一个文件。

正是这种解耦让 CSV 镜像、SSE 流、规则引擎可以并列存在而互不耦合——每个都是同一事件流之上 50~100 行的订阅者。

---

## 快速上手

```bash
# 把 data/incidents.db 里的所有场景排空，写事件日志，然后退出。
python -m src.kg_engine run --once --log-dir data/kg_engine

# 有界排空（用于对等测试 / 烟雾测试）。
python -m src.kg_engine run --once --max-incidents 10 --log-dir data/kg_engine

# 汇总最新事件日志文件。
python -m src.kg_engine status --log-dir data/kg_engine

# 启用 R-2 + R-3 全部特性（YANG 监视、快照、规则引擎、滑动窗口）。
python -m src.kg_engine run \
    --yang-watch --snapshot --rule-engine --sliding-window \
    --log-dir data/kg_engine

# 实时把引擎状态镜像到 data/kg/*.csv（列结构与批量 CSV 一致）。
python -m src.kg_engine run --mirror-csv --yang-watch --snapshot \
    --log-dir data/kg_engine

# R-4 —— 启动 SSE 演示 UI（默认 http://127.0.0.1:5001）。CSV 镜像在这里
# 默认开启；如需关闭，传 --no-mirror-csv。
python -m src.kg_engine web
```

---

## 增量更新已有的 KG（生产工作流）

你已经有一份完整的 KG（`data/kg/alarm_triples.csv`，来自 `python -m src.kg build`），想让它**实时**保持更新，当以下变化发生时：

- 拓扑变了（运维改了 `data/yang/*.json`，或 R-5 上线后控制器推 NETCONF 通知）
- 新告警到来（仿真器写新场景；或真实 NMS 流接进来）
- 出现新的告警类型 / 故障类别

**不要每次都重跑批处理构建器**。那会把引擎的"连续更新"保证扔掉，而且慢。改成两步用引擎。

### 第一步 —— 一次性引导

把 `incidents.db` 里所有东西排空一遍，启用快照。引擎会重建出批处理构建器同样的 KG，然后把状态 pickle 到磁盘：

```powershell
python -m src.kg_engine run `
    --once --snapshot --yang-watch --rule-engine `
    --log-dir data\kg_engine
```

产物：
- `data\kg_engine\events.ndjson.YYYYMMDD` —— 完整事件日志
- `data\kg_engine\snapshots\snap_<ulid>.pkl` —— `StateStore` 检查点，内存形态的完整 KG

### 第二步 —— 长跑增量模式

从此以后，启动引擎**别加 `--once`**。引擎会在毫秒级从快照恢复，只处理新东西：

```powershell
python -m src.kg_engine run `
    --snapshot --yang-watch --rule-engine `
    --log-dir data\kg_engine
```

引擎现在会自动在后台干这些事：

| 触发条件 | 引擎做什么 |
|---|---|
| 运维在仿真器 GUI 点一个故障 | `incidents.db` 加一行 → `SimulatorAdapter` 轮询（0.5 秒）→ 发 `alarm.raise` + `incident.boundary` → handler 添加三元组 → 下次 tick 更新快照 |
| 你改了 `data\yang\topology.json`（或 `tunnels.json` / `inventory.json`） | `YangWatchAdapter` 检测到 mtime 变化（1 秒）→ 重新加载 YANG → 发 `topology.snapshot` → `handle_topology_snapshot` 对比新旧拓扑发 `Delta(adds, removes)` |
| 数据里冒出新告警类型或新 layer | Handler 是数据驱动的 —— 它们直接读 `row['layer']`、`row['ne']` 等。陌生的告警名或新的 layer 字符串会产出合法的三元组（比如 `IsLayer alarm:... layer:newlayer`），**不用改代码** |

### 一个坑 —— `web.py` 的默认值

`python -m src.kg_engine web` 演示用了 `start_from_now=True`，让 UI 启动时是空的（对演示有用：只有新点击的故障才会出现）。如果你的用途是增量更新现有 `incidents.db` 中的行，编辑 `src\kg_engine\web.py::create_app`：

```python
# 去掉 start_from_now，让现有的 incidents.db 行也被处理
engine.register_adapter(SimulatorAdapter(db_path=db_path)(engine))

# 启用快照，让演示重启时能恢复
engine = Engine(snapshots_dir="data/kg_engine/snapshots",
                snapshot_interval_s=60.0)
```

CSV 镜像在 `web` 中**默认开启** —— 每次点击故障都会追加到 `data/kg/alarm_triples.csv` 与 `incident_manifest.csv`；任何 YANG 编辑都会原子地重写 `topology_triples.csv`。如果你不想让磁盘 CSV 被改动（例如把 UI 纯粹当作临时演示用），传 `--no-mirror-csv`。

### 验证是否接好了

```powershell
python -m src.kg_engine status --log-dir data\kg_engine
```

第一步跑完后应该看到几十万条 `triple adds`。然后做点改动 —— 改个 YANG 文件、点个故障 —— 再跑 `status`，事件数和三元组增量都应该升。

### 这个工作流里"当前 KG"在哪儿

| 层 | 易失性 | 什么时候读它 |
|---|---|---|
| 内存里的 `engine.state.triples` | 重启即丢 | 活的消费者 —— 规则引擎、SSE、进程内订阅者 |
| `data/kg_engine/events.ndjson.*` | 持久，只追加 | 长期真值 —— 每次三元组增删都连同 provenance 一起记录 |
| `data/kg_engine/snapshots/*.pkl` | 可丢弃，每 60 秒自动重生 | 仅用于快速重启 —— 任何时候都能删；只要日志在，状态就能重建 |
| `data/kg/{topology,alarm}_triples.csv` + `incident_manifest.csv` | 持久。告警与 manifest 是追加；拓扑是原子重写 | 给下游已经习惯批量 CSV 列结构的消费者（例如规划中的 `src/ml/` 训练器）的可选便利。由 `CsvMirrorSubscriber` 产生。 |

权衡：NDJSON 日志完整的话，KG 能从零状态完整恢复。快照只是限制了恢复时间。你**不必**维护 CSV —— 流式日志更丰富（能捕获删除与事件分组）。当下游工具（GNN 训练器、pandas notebook、批量消费者）希望批量构建器的列结构被持续更新而不是按需重建时，再开启 CSV 镜像。

完整一条事件穿过这条流水线的逐步跟踪，见 `INCIDENT_FLOW.zh-CN.md`。

长跑模式（持续轮询 `incidents.db`，每当仿真器写入新场景就发事件）——去掉 `--once`：

```bash
python -m src.kg_engine run --log-dir data/kg_engine
```

事件日志位于 `data/kg_engine/events.ndjson.YYYYMMDD`（按 UTC 日期一天一文件，一行一条事件）。每行格式为 `{"event": {...}, "delta": {"adds": [...], "removes": [...]}}`。

---

## R-1 做了什么

| 阶段 | 职责 | 代码位置 |
|---|---|---|
| 适配器 | 轮询 `incidents.db` 找 `id > last_id`，每条告警行发一个 `alarm.raise`，每个场景发一个 `incident.boundary` | `adapters/simulator.py` |
| 引擎 | 从 `asyncio.Queue` 拉事件、按 kind 派发到 handler、在锁内应用 `Delta`、广播给 subscribers | `engine.py` |
| 告警 handler | 发每条告警的位置三元组（`IsLayer`、`AtNE`、`AtBoard`、`AtPort`、`AtSpan`/`OnPipe`/`AtChannel`/`Container`）；把行累积到打开的事件上 | `handlers/alarms.py` |
| 事件 handler | 在 `incident.boundary` 时跑标签（`RootCause`/`Secondary`）+ 局部性（`OnSameNE`、`OnSameBoard` …）+ 时序（`AllClose`、`AllWithinSix`）推导，覆盖事件内所有行 | `handlers/incidents.py` |
| 订阅者 | 把 `(event, delta)` 作为一行 NDJSON 追加；每次写入都 fsync | `subscribers/event_log.py` |

**对等基准（已证明）**：对同一份 `incidents.db`，流式引擎产生的 `(subject, predicate, object, source, incident_id)` 元组集合，等于批处理构建器 `alarm_triples` 的集合（在排除拓扑三元组和 `rule:R*` 关联边之后）。

---

## R-2 / R-3 / R-4 新增功能

| 阶段 | 功能 | 代码位置 |
|---|---|---|
| R-2 | Pickle 快照 + 恢复 | `snapshots.py`，接入到 `engine.py` |
| R-2 | YANG 文件监视器（mtime 轮询，无新依赖） | `adapters/yang_watch.py` |
| R-2 | 通过 `topology.snapshot` 事件做拓扑差分 | `handlers/topology.py` |
| R-3 | R1–R4 滑动窗口事件分组 | `handlers/grouping.py`（引擎参数 `sliding_window=True`） |
| R-3 | 后台 tick 关闭超过 6 分钟窗口的事件 | `engine.py::_closure_loop` |
| R-3 | 规则引擎订阅者跑 `src.rules.infer_root`，发 `source="rule:fingerprint"` 的 `RootCause` | `subscribers/rule_engine.py` |
| R-4 | Server-Sent Events 扇出订阅者 | `subscribers/sse.py` |
| R-4 | 独立演示 Flask 应用（`python -m src.kg_engine web`） | `web.py` |
| 实时 CSV 镜像 | `CsvMirrorSubscriber` —— 拓扑变更时原子重写 `data/kg/topology_triples.csv`；每个 `incident.boundary` 追加到 `data/kg/alarm_triples.csv` 与 `incident_manifest.csv`。通过读 manifest 做去重，支持重启续写。 | `subscribers/csv_mirror.py` |

## 仍然延后的事

| 阶段 | 功能 | 文档 |
|---|---|---|
| R-5 | NETCONF + alarm-stream 适配器 | `Design/10 §11`；纲要见 `adapters/netconf.py`、`adapters/alarm_stream.py` |
| R-5 | UI 实时流接入到 `src/app/server.py`（R-4 演示应用是独立的） | — |
| R-6 | 性能加固（有界队列、批量 fsync、局部性分桶、规则引擎防抖） | `docs/04_kg_engine_perf_plan.md` |

包结构里已经留出了对应的接缝（`adapters/`、`subscribers/`、`handlers/` 的派发表），后续每个阶段只用填代码，不必重组结构。

---

## 模块布局

```
src/kg_engine/
├── __init__.py            # 导出 Engine, run
├── README.md              # 英文版
├── README.zh-CN.md        # 本文件
├── events.py              # Event, Delta, EventKind, Source 常量, new_event_id
├── state.py               # StateStore, TripleIndex（幂等 add/remove）, Incident
├── engine.py              # 异步主循环：queue → dispatch → broadcast，
│                          # 快照 + 关闭 tick 循环
├── snapshots.py           # R-2：pickle 快照 + 恢复
├── web.py                 # R-4：独立 Flask SSE 演示应用
├── handlers/
│   ├── __init__.py        # 派发表 {kind → handler}
│   ├── alarms.py          # alarm.raise（clear/update 是占位 stub）
│   ├── incidents.py       # incident.boundary → 标签 + 局部性 + 时序
│   ├── topology.py        # R-2：topology.snapshot（通过索引做差分）
│   └── grouping.py        # R-3：滑动窗口 R1-R4 事件归属
├── adapters/
│   ├── __init__.py
│   ├── simulator.py       # 跟踪 incidents.db
│   ├── yang_watch.py      # R-2：data/yang/*.json mtime 轮询
│   ├── netconf.py         # R-5：占位（构造时 NotImplementedError）
│   └── alarm_stream.py    # R-5：占位（构造时 NotImplementedError）
├── subscribers/
│   ├── __init__.py
│   ├── event_log.py       # NDJSON 只追加日志，按日轮转，fsync
│   ├── rule_engine.py     # R-3：封装 src.rules.infer_root；发 rule:fingerprint
│   ├── sse.py             # R-4：HTTP SSE 的每读者队列扇出
│   └── csv_mirror.py      # 从引擎状态镜像 data/kg/*.csv
└── __main__.py            # CLI：run | status | web | replay/export（R-2+ stub）
```

运行时数据（首次运行时生成，应进 `.gitignore`）：

```
data/kg_engine/
├── events.ndjson.YYYYMMDD       # 一天一个文件（UTC）
└── snapshots/                   # 仅在使用 --snapshot 时生成
    └── snap_<ulid>.pkl          # 最近的状态 pickle（R-2）
```

---

## 编程式接入

```python
import asyncio
from src.kg_engine.engine import Engine
from src.kg_engine.adapters import SimulatorAdapter
from src.kg_engine.subscribers import EventLogSubscriber

async def main():
    engine = Engine()                              # 自动从 data/yang/ 加载 YANG
    engine.register_subscriber(EventLogSubscriber("data/kg_engine"))
    engine.register_adapter(SimulatorAdapter(once=True, max_incidents=50)(engine))
    await engine.run()
    print(f"末态：{len(engine.state.triples.all_triples)} 条三元组")

asyncio.run(main())
```

订阅者只需实现一个方法：

```python
class MySubscriber:
    async def on_delta(self, delta, event):
        for t in delta.adds:    ...
        for t in delta.removes: ...
```

通过 `engine.register_subscriber(MySubscriber())` 注册。Handler 是纯函数 `(event, state) -> Delta`；自定义 handler 用 `engine.register_handler(kind, fn)` 注册。

---

## 承重设计决策

来自 `Design/10 §16`。要改的话，对应常量隔离在 `events.py` / handlers 里，方便覆盖。

| 编号 | 决策 | 理由 |
|---|---|---|
| Q1 | 引擎内部用 `incident_id`；批处理 CSV 保留 `incident_id` | 不动批处理产物；R-1 里两者 1:1 对应 |
| Q3 | 适配器 + 主循环纯 `asyncio`。CPU 密集型订阅者后续可走 `loop.run_in_executor` 卸载 | 单事件循环，简单可推理 |
| Q5 | 规则引擎的根因预测用 `source="rule:fingerprint"`；仿真器真值保留 `source="alarm"`；模型输出预留 `source="model:rgcn"` | 来源隔离干净；同一条告警可以有多条 `RootCause` 三元组、只在 source 上区分 |

---

## 验证

把对等测试当独立脚本跑，或者用 pytest（如果装了的话）：

```bash
python tests/test_kg_engine_parity.py --max-incidents 5     # 快
python tests/test_kg_engine_parity.py --max-incidents 500   # 重
```

或者直接走 CLI：

```bash
python -m src.kg_engine run --once --max-incidents 10 --log-dir /tmp/kg
python -m src.kg_engine status --log-dir /tmp/kg
```

流式三元组与 `src.kg.builder.build_kg(...)` 输出有任何偏离（除已声明的排除项外），对等测试都会大声失败。

---

## 相关代码

- `src/kg/` —— 本引擎替代的批处理构建器。冷启动 / 全库导出时仍然有用。
- `src/kg/schema.py` —— `Triple`、URI 辅助函数、谓词名。引擎原样导入。
- `src/kg/alarm_graph.py` —— `alarm_location_triples` 和 `alarm_label_triples` 在 R-1 阶段被抽取到这里，让批处理和流式共用每行三元组的代码。
- `src/kg/locality.py`、`src/kg/temporal.py` —— 由 `handlers/incidents.py` 原样复用。
- `src/simulator/db.py` —— `incidents.db` 的 schema（适配器读什么）。
- `src/simulator/yang_loader.py` —— 加载引擎保存在 `StateStore.topology` 里的 `Topology` 对象。
- `src/rules/` —— 基于规则的 RCA 推理器。R-3 阶段会被串成进程内订阅者，发 `source="rule:fingerprint"` 的 `RootCause` 三元组。
