# KG 引擎如何响应一条新事件

`README.md` 的配套文档。README 告诉你包里有什么文件、怎么运行；**本文档跟随一条事件从仿真器提交的那一刻，一直到它的三元组落入 KG 的全过程，并解释 KG 实际存在哪里。**

如果你来这里是想问"KG 文件在哪？"——**简短答案是：没有 CSV**。KG 是一个**内存中的三元组索引**，由一份流式的 **NDJSON 事件日志**不断重建。两者一起构成真值；其他一切（快照、CSV 导出、GNN 输入）都是从它们派生的。

---

## 1. 端到端图

```
┌────────────────────────────┐
│  仿真器 (src/simulator)    │  用户在 GUI 点一个故障
│                            │  ↓
│  src.app.server:           │  POST /api/trigger → engine.expand_cascade
│    /api/trigger            │  ↓
│                            │  src.simulator.db.record(...) ← 一个 SQL 事务
└──────────────┬─────────────┘
               │ 写入
               ▼
┌────────────────────────────┐
│  data/incidents.db         │  incidents 表加一行
│  (SQLite, DELETE journal)  │  + alarms 表加 N 行，全部在一次提交里
└──────────────┬─────────────┘
               │ 每 0.5 秒轮询一次
               ▼
┌────────────────────────────┐
│ SimulatorAdapter           │  SELECT id FROM incidents WHERE id > ?
│ (adapters/simulator.py)    │  把 Event 对象放进 engine.queue
└──────────────┬─────────────┘
               │ asyncio.Queue
               ▼
┌────────────────────────────┐    handlers/
│ Engine 主循环              │      alarms.py        ← 发位置三元组
│ (engine.py)                │      incidents.py     ← 发标签 + 局部性 + 时序
│                            │      topology.py      ← 发拓扑差分
│  取 Event，                │      grouping.py      ← 决定归属哪个 incident
│  挑 handler，              │
│  handler 返回 Delta，      │
│  Delta 应用到              │
│  TripleIndex (state.py)    │
│  并广播给：                │
└──┬──────────┬──────────┬───┘
   │          │          │
   ▼          ▼          ▼
┌────────┐ ┌────────┐ ┌────────────┐
│事件    │ │SSE     │ │规则引擎    │  重跑 src.rules.infer_root
│日志    │ │扇出    │ │订阅者      │  把 rule.root_pick 再投回引擎队列
│(NDJSON)│ │(/api/  │ │            │
│        │ │ events)│ │            │
└────────┘ └────────┘ └────────────┘
   │           │
   ▼           ▼
data/        浏览器
kg_engine/   (EventSource)
events.
ndjson.
YYYYMMDD
```

---

## 2. 流水线，逐阶段

假设用户在仿真器 GUI 上点了 **"Fiber cut on span_OMS3_BC"**。下面是后台逐步发生的事。

### 阶段 1 —— 仿真器把一个事务写进 SQLite

`src/app/server.py::api_trigger` 跑完级联规则后调用
`src/simulator/db.py::record(...)`。该函数执行 **一个事务**：

1. `INSERT INTO incidents (...)` → 拿到一个新的 `incident_id`，比如 `5128`。
2. `INSERT INTO alarms (incident_id, ...)` —— 级联里每条告警一行（光纤切断典型 40–60 行）。
3. `conn.commit()` —— 整个场景**原子**地变为可见。

引擎绝对看不到一个"半个场景"。

### 阶段 2 —— 适配器发现新 id

`SimulatorAdapter._run` 是一个每 0.5 秒轮询的 `async` 循环：

```python
SELECT id FROM incidents WHERE id > {last_id} ORDER BY id
```

当它返回 `[(5128,)]` 时，适配器：

- 取出 5128 的所有告警行（`SELECT * FROM alarms WHERE incident_id=5128`）。
- **每条告警发一个 `alarm.raise` Event**，payload 是 `{"incident_id": 5128, "row": {...}}`（事件绑定场景模式）——row 字典携带 SQL schema 里的每一列（`id`、`alarm_name`、`ne`、`board`、`port`、`severity`、`layer`、`container_id`、`t_offset_ms`、`t_observed_ms`、`is_root`、`group_id`）。
- 在最后一条告警之后**发一个 `incident.boundary` Event**，payload 是 `{"incident_id": 5128}`。

每个 event 都有新的 `event_id`（可排序的"纳秒+随机后缀"字符串）、`at` 时间戳、`source="simulator"`。

在滑动窗口模式（`--sliding-window`）下，适配器**不**发 `incident_id` 和 boundary 事件——引擎里的分组器负责给事件分配 incident，后台 tick 在 6 分钟静默后关闭它们。

### 阶段 3 —— 引擎派发每个事件

`Engine.run` 从 `asyncio.Queue` 取事件，一次一个。对每个事件：

1. **按 `event_id` 去重** —— 重放安全。如果引擎重启并在回放日志，已应用过的事件会被跳过。
2. **挑 handler** —— `handlers/__init__.py` 的派发表把 event kind 映射到纯函数 `(event, state) -> Delta`。
3. **获取锁**，调用 handler。
4. **应用返回的 Delta** 到 `state.triples`（remove 用 `TripleIndex.remove(...)`，add 用 `.add(...)` —— 都是幂等的）。
5. **盖章** `state.last_event_id = event.event_id`，保证快照 / 回放的正确性。
6. **释放锁**，把 Delta 广播给每个订阅者。

对我们的光纤切断，事件序列是：

```
alarm.raise   (47 条告警里的第 1 条)  → Delta(+5 位置三元组，0 个移除)
alarm.raise   (47 条里的第 2 条)      → Delta(+5, 0)
...
alarm.raise   (47 条里的第 47 条)     → Delta(+5, 0)
incident.boundary                     → Delta(+约 1500 条三元组, 0 个移除)
```

所以每个场景引擎要处理 **N + 1** 个事件：N 个 alarm-raise（Δ 较小）和 1 个 incident-boundary（Δ 很大）。

### 阶段 4 —— 每个 handler 算什么

#### `handle_alarm_raise` (handlers/alarms.py)

调用 `src.kg.alarm_graph.alarm_location_triples(incident_id, row, topology)`。对一条告警行，最多产出 5 条三元组：

```
alarm:5128:101410   IsLayer    layer:OTS                (src=alarm)
alarm:5128:101410   AtNE       ne:C                     (src=alarm)
alarm:5128:101410   AtBoard    board:C.FIU              (src=alarm)
alarm:5128:101410   AtPort     port:C.FIU.line_from_B   (src=alarm)
alarm:5128:101410   AtSpan     span:span_OMS3_BC        (src=alarm)
```

（`AtSpan` / `OnPipe` / `AtChannel` / `Container` 由 `classify_container` 挑——光纤切断的根因坐落于一段 span，所以 `AtSpan` 触发。）

它同时把告警行 dict 追加到 `state.incidents[5128].alarm_rows`，让后面的 boundary handler 能拿到完整集合。

#### `handle_incident_boundary` (handlers/incidents.py)

boundary 到达时，完整告警列表已经在 `state.incidents[5128].alarm_rows` 里。handler 调用 `src/kg/` 里的三个推导函数：

| 函数 | 发什么 | 典型数量 |
|---|---|---|
| `alarm_graph.alarm_label_triples` | 按 `group_id` 键控的 `RootCause(root, root)` 与 `Secondary(non-root, root)` | N（每条告警一个） |
| `locality.derive_locality_triples` | 事件内每对告警之间的 `OnSameNE`、`OnSameBoard`、`OnSamePort`、`OnSameContainer`、`SameSpan`、`OnSameLink` | 最多 N² × 6 |
| `temporal.derive_temporal_triples` | 在 `incident:5128` 上的紧凑 `AllClose` 与 `AllWithinSix` 标志（或 `compact=False` 时的逐对 `Earlier`/`Close`/`WithinSix`） | 0–2 |

对一个 47 条告警的光纤切断，仅局部性就是 ~47 × 46 / 2 = 1081 对 × 每对若干谓词 ——大约**每个场景 1500 条三元组**。

#### `handle_topology_snapshot` (handlers/topology.py)

不在我们这条事件路径上——只在 YANG 监视器检测到文件变化时触发。把新拓扑跟 `state.triples` 里当前 `source="topology"` 的三元组做差分，发出 delta。

### 阶段 5 —— 订阅者消费 Delta

每个订阅者拿到 `(delta, event)`。引擎自带三个：

#### `EventLogSubscriber` (subscribers/event_log.py)

往 `data/kg_engine/events.ndjson.YYYYMMDD` 追加一行 JSON：

```json
{"event":{"event_id":"01779...-a3f","kind":"alarm.raise","at":1747241234567,"source":"simulator","payload":{"incident_id":5128,"row":{...}}},
 "delta":{"adds":[["alarm:5128:101410","IsLayer","layer:OTS","alarm",1.0,5128],
                  ["alarm:5128:101410","AtNE","ne:C","alarm",1.0,5128],...],
          "removes":[]}}
```

每行写完都 `f.flush() + os.fsync(...)`。**这个文件是引擎的持久化真值。** 内存状态丢了 → 加载快照 + 回放该文件 → 完全一样的 KG。

#### `SSESubscriber` (subscribers/sse.py)

把序列化后的字符串推到每个连上来的浏览器的队列里。浏览器的 `EventSource("/api/kg-engine/events")` 连接每个事件收到一行 `data: {...}\n\n`。这就是 Web UI 不用轮询就能实时更新的原理。

#### `RuleEngineSubscriber` (subscribers/rule_engine.py)

这个有意思——它不仅观察，还**反馈**回引擎。

每条 `alarm.raise` 上：
1. 看当前事件已累积的告警行。
2. 调用 `src.rules.infer_root(incident_id, alarm_rows, topology)`。
3. 如果挑出的根因 alarm id 跟上次不同：
   - 投一个 `rule.root_pick` Event 进队列，`{old_pick, new_pick}`。

引擎随后像处理任何其他事件一样处理这个合成事件：`handle_rule_root_pick` 返回一个 Delta，**移除** 旧的 `RootCause` 三元组（`source="rule:fingerprint"`）并**添加**新的。所以随着更多告警到来，你看到规则的猜测在变——UI 里就是 `(+1/-1)` 事件。

这就是为什么同一个 `alarm:5128:X` 能有**两条** `RootCause` 三元组—— 一条 `source="alarm"`（仿真器的真值）一条 `source="rule:fingerprint"`（规则引擎的预测）。它们并排存在，下游消费者可以做对比。

### 阶段 6 —— 周期性后台任务

上面跑的同时，两个后台任务在 tick：

- **快照循环**（默认 60 秒，仅在 `--snapshot` 下）：把整个 `StateStore` pickle 到 `data/kg_engine/snapshots/snap_<ulid>.pkl`。默认只保留最近 3 个。
- **关闭 tick**（每 30 秒，仅在 `--sliding-window` 模式下）：扫描 `state.incidents`，对最年轻告警超过 6 分钟的事件发 `incident.boundary`。

---

## 3. KG 实际存在哪里

**没有 kg.csv**。KG 在三个物理表现形式里，每个有不同用途。

| # | 位置 | 类型 | 内容 | 用途 |
|---|---|---|---|---|
| 1 | `engine.state.triples.all_triples`（内存） | Python `set[Triple]` | 当前活跃 KG 里的每条三元组 | 任意时刻的真值。SSE / state API 以及任何进程内消费者读这个。 |
| 2 | `data/kg_engine/events.ndjson.YYYYMMDD` | 只追加 NDJSON，按 UTC 日期一天一文件 | 每条事件一行 JSON，每行携带它的 `Delta(adds, removes)` | **持久化**真值。回放可以精确重建 (1)。能挺过引擎重启和崩溃。 |
| 3 | `data/kg_engine/snapshots/snap_<ulid>.pkl` | Pickle（二进制） | `StateStore` 检查点 | **可选**的快速重启。有快照的话引擎不用从第 0 天开始回放整个日志——只回放比快照 `last_event_id` 新的事件。 |

(1) 是易失的但永远新鲜的。(2) 是长期真值——每条三元组的增删都被连同 provenance 一起记录。(3) 只是 (2) 之上的性能加速器。

### 为什么是 NDJSON 而不是 CSV

CSV 每条三元组一行，对于 KG 的**静态快照**很合适。但流式 KG 不是静态的——三元组有来有去。我们需要表达**移除**，并且需要把同一个事件里同时发生的 adds / removes 分到一组，让回放能产生同样结果。每行 NDJSON 是**一个事件的完整 diff**，且有顺序。CSV 没有辅助列做不到。

你随时可以产出 CSV——`src/kg/builder.py`（批处理构建器）读 `incidents.db` 写一份。流式引擎对任何场景跟批处理 CSV 输出保持对等；区别只是通道。

### 从你自己的代码读 KG

引擎运行时：

```python
# 进程内——任意订阅者或 REPL 调用者：
for t in engine.state.triples.by_predicate["RootCause"]:
    print(t)

# 跨进程——读 NDJSON 文件：
import json
with open("data/kg_engine/events.ndjson.20260520") as f:
    for line in f:
        rec = json.loads(line)
        for triple in rec["delta"]["adds"]:
            ...
```

Triple schema 跟批处理构建器（`src/kg/schema.py`）完全相同，所以消费批处理 CSV 的代码可以平凡地消费 NDJSON。

---

## 4. 具体演练 —— 47 条告警产出什么

让我们逐字节追踪一个 `fiber_cut(span_OMS3_BC)` 场景（47 条告警）写到 NDJSON 的内容。

**第 1 步 —— 47 条 `alarm.raise` 事件。** 每行约 600 字节：

- `event.payload` 携带完整告警行 dict。
- `delta.adds` 有 5 条 Triple（仅位置）。
- `delta.removes` 为空。

合计：raises 部分约 28 KB NDJSON。

**第 2 步 —— 1 条 `incident.boundary` 事件。** 这一行约 120 KB：

- `event.payload` 很小（`{"incident_id": 5128}`）。
- `delta.adds` 携带 **1 RootCause + 46 Secondary + ~1080 局部性 pair × 4-6 谓词 ≈ 1500 条三元组**。
- `delta.removes` 为空。

**第 3 步 —— 最多 N 条 `rule.root_pick` 事件。** 每条约 250 字节。第一次没有前置根因 (`+1/-0`)；后续每次换 (`+1/-1`)。对我们的光纤切断，规则引擎通常在 2–3 条告警到达后就收敛到正确的根因。

**最终状态**：一次光纤切断给内存索引添加了 **~1550 条三元组**，给 NDJSON 文件写了 **~150 KB**。

你可以用这个理智检查：

```bash
python -m src.kg_engine status --log-dir data/kg_engine
```

它会打印事件总数、adds 总数、removes 总数，以及按事件 kind 分桶。

---

## 5. 重启时会发生什么

引擎重启（干净的 SIGINT、操作系统重启、硬件崩溃—— 引擎看不出差别）：

1. `Engine.__init__` 看 `snapshots_dir`（如果用了 `--snapshot`）。
2. **如果有快照**：`pickle.load(...)` → `StateStore` 一步重建完成。`state.last_event_id` 告诉引擎日志读到哪里了。
3. **如果没有快照**：从空 `TripleIndex` 起步，让 YangWatchAdapter 发初始 `topology.snapshot`，174 条拓扑三元组就回来了。
4. 引擎从队列恢复处理新事件。适配器重连（仿真器适配器只需从 `_last_id` 继续轮询）。

无论哪种路径，**只要 NDJSON 日志完整，三元组就不会丢**：快照 + 实时事件流之间，每一次状态转移都可重现。

（`replay` CLI 命令 —— `python -m src.kg_engine replay --from EVENT_ID` —— 是 R-2+ 工作，目前抛 `NotImplementedError`。基于快照的恢复已实现；纯粹的"从日志回放"是下一步。）

---

## 6. 实时 vs 批处理 CSV —— 何时用哪个

| 你想做 ... | 用 |
|---|---|
| 在冻结的、推导完整的 KG 上训练 GNN | `python -m src.kg build` → `data/kg/alarm_triples.csv` |
| 演示故障到来时 KG 的实时变化 | `python -m src.kg_engine web` → http://127.0.0.1:5001 |
| 构建一个消费新告警的 RCA 服务 | 引擎 + `RuleEngineSubscriber` + 你自己监听 deltas 的订阅者 |
| 审计"事件 X 的时候引擎发了哪些三元组" | `grep '"incident_id":5128' data/kg_engine/events.ndjson.*` |
| 崩溃后恢复 | 引擎启动时挑 `data/kg_engine/snapshots/` 里最新的 pickle |
| 把活的 KG 导成 CSV 给别人 | 未来的 `python -m src.kg_engine export`（今天的 R-2+ stub）；暂时可以回放 NDJSON，或者直接重跑批处理构建器 |

两条流水线共用 `src/kg/schema.py` 和 `src/kg/{alarm_graph,locality,temporal,correlation,topology_graph}.py`，所以它们的输出可互换。
