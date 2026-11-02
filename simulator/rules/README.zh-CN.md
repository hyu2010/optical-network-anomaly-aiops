# src/rules —— 基于规则的根因分析推理器

针对模拟器产出的告警级联，运行确定性、可解释的根因分析（RCA）推理器。给定一个场景的告警列表 + YANG 拓扑，它会挑出 **一个根因告警**、命名 **故障类别**，并把其它告警标注为某个父告警的 **Secondary**（次生）。

本模块是项目 ML 分层路线图（`Design/04 §6.3`）中的 **T1 基线**。它实现了 `Design/08_root_cause_rules.md` 定义的规则族 —— 也是 GNN（`src/ml/`，规划见 `Design/11`）需要超越的对手。

- **设计规范**：`Design/08_root_cause_rules.md`（规则定义）；`docs/03_rules_catalog.md`（按规则展开的清单 + 实现状态）
- **在默认 5120 场景数据集上的当前准确率**：
  - 根因告警：**99.1%**
  - 故障类别：**98.4%**
  - 逐类指标：`b1_exc_high`、`b2_exc_high`、`board_power_off`、`fan_failure`、`fiber_attenuation_high` 全 100%；其余 96–100%。详见 `memory/rules_baseline.md`。

---

## 快速开始

```powershell
# 对 data/incidents.db 中所有场景评估推理器
& .\.venv\Scripts\python.exe -m src.rules evaluate

# 同上，但只评估原始（raw）场景（跳过 L1 抑制视图）
& .\.venv\Scripts\python.exe -m src.rules evaluate --raw-only

# 评估前 200 个场景（冒烟测试）
& .\.venv\Scripts\python.exe -m src.rules evaluate --max-incidents 200

# 打印单个场景的规则触发轨迹 —— 用于解释为什么误判
& .\.venv\Scripts\python.exe -m src.rules explain 42
```

`evaluate` 会打印：
- 按故障类别的准确率表（raw 视图与 suppressed 视图分别列出）。
- 单故障场景的 `fault_type` 混淆矩阵。
- 前 N（默认 10）个失败场景，含预测根因 vs 真实根因。

`explain` 会打印：
- 真实的根因（可能多于一个，复合场景）和推理器的预测。
- 预测的故障类别，以及命中的规则（`source`）。
- 前 25 条 `Inferred` 记录（Secondary / RootCandidate / RootCause），按规则发射顺序排列。

---

## 这里"基于规则"是什么意思

推理器 **不训练**。它读取一组专家手写的谓词，按优先级顺序对告警列表运行。每个谓词都是纯 Python 函数，并带有清晰命名的来源（如 `§8.1.fiber_cut`），所以每一条预测都附带一份"哪条规则触发、为何触发"的轨迹。

这种做法带来两件学习模型不具备的能力：
- **可解释性** —— 每条预测都映射到 `Design/08` 中一条带引用的规则。
- **无需标注数据** —— 规则源自专家知识（OSN 9800 告警手册 + 抑制图），不靠训练场景。即使没有真值也能工作。

它失去的、未来 GNN 应当夺回的能力：**对规则覆盖不到的样本做泛化**。见下文"已知失败模式"。

---

## 架构

```
src/rules/
├── __init__.py            # 导出 infer_root、Inference
├── __main__.py            # CLI：evaluate | explain
├── schema.py              # Inferred、Inference 数据类
├── reasoner.py            # 编排 —— 挑根因，应用所有规则族
├── fingerprints.py        # §8 —— 10 个故障类别指纹（主要的根因挑选）
├── layer_cascade.py       # §3 —— 同端口内的层级顺序（OTS→OMS→OCh→...→client）
├── topology_cascade.py    # §4 —— 跨端口的拓扑驱动传播
├── equipment.py           # §5 —— NE / 板 / 风扇"王牌"
└── evaluate.py            # 与 incidents.db 真值对比的评分
```

### 数据流

```
incidents.db         data/yang/
   alarms 表             topology.json
        │              tunnels.json
        ▼              inventory.json
  list[dict]            ▼
   （每行一               Topology 对象
    个告警）             │
        │              │
        └──────┬───────┘
               ▼
        reasoner.infer_root(incident_id, alarms, topology)
               │
               ▼
  ┌─────────── 流水线 ────────────┐
  │                                  │
  │  fingerprints.apply()  ── §8    │  ← 挑根因 + 故障类别
  │  layer_cascade.apply()  ── §3   │  ← 标注 Secondary（同端口内）
  │  topology_cascade.apply() ── §4 │  ← 标注 Secondary（跨端口）
  │  equipment.apply()      ── §5   │  ← NE / 板级王牌
  │                                  │
  │  若没有任何指纹触发：             │
  │     fallback.lowest_layer        │  ← 优雅降级
  └──────────────────────────────────┘
               │
               ▼
   Inference(
     incident_id, root_alarm_id, root_alarm_name,
     fault_type, source,
     inferred=[Inferred(kind, alarm_id, parent_id, ...), ...],
   )
```

### 为什么是这个编排顺序？

按 `reasoner.py` 的 docstring（§8 在先，然后 §3 / §4 / §5）：

1. **指纹（§8）挑根因。** 10 个 ★ 优先故障各有一个特征告警星座。若两个指纹同时命中，权重高（更具体）者胜；并列时按 `FINGERPRINTS` 列表顺序破解 —— 更具体的规则排在前面。
2. **§3 / §4 / §5 标注次生告警。** 这些规则不挑根因 —— 它们只解释"为什么其它告警不是根因"。它们产出的 `Inferred(kind="Secondary")` 记录是信息性的（用于 `explain` 输出，以及在 `src/kg/` 中产生 `Secondary` 三元组）。
3. **没有任何指纹触发时才走 fallback。** 取所有未被标 Secondary 的告警中层级最低的一条。`Design/08 §6` 提醒过："最早时间戳 = 根因"在轮询延迟下不靠谱，因此这个 fallback 故意 **不用** 时间。

---

## 规则族

### §8 —— 故障类别指纹（`fingerprints.py`）

10 个函数，每个对应一个 ★ 优先故障。每个返回零个或多个 `RootCandidate` 记录，权重在 `[1.6, 2.0]` 之间。

| 指纹 | 根因告警 | 核心前提 | 权重 |
|---|---|---|---|
| `fp_fiber_cut` | 跨段上的 `OTS_LOS` | 同跨段上还有 `MUT_LOS` | 2.0 |
| `fp_ne_power_off` | NE 上的 `POWER_ABNORMAL` | 该 NE 至少 60% 出向跨段出现 `OTS_LOS` | 2.0 |
| `fp_board_power_off` | 板上的 `BD_OFFLINE` | 对端口 `R_LOS`，或（同 NE 无 `OTS_LOS`） | 2.0 / 1.7 |
| `fp_fan_failure` | NE 上的 `FAN_FAIL` | 同 NE 另一块板出现 `TEMP_OVER` | 2.0 |
| `fp_config_mismatch` | 端口上的 `OPU_PLM` | 同端口 ≥ 3 个 `{OPU_PLM, OTU_TIM, ODU_PM_TIM}` 共现；该 NE 无 `OTS_LOS` | 1.8 |
| `fp_fiber_attenuation_high` | 跨段上的 `IN_PWR_LOW` | 同 NE 拓扑容器上有 `BEFFEC_EXC`；同跨段无 `OTS_LOS` | 1.8 |
| `fp_rx_power_drift` | 端口上的 `IN_PWR_LOW` | 同端口有 `IN_PWR_ABN`；同跨段无 `OTS_LOS`；同 NE 拓扑容器无 `BEFFEC_EXC` | 1.7 |
| `fp_fec_pre_cor_high` | OTU 端口上的 `BEFFEC_EXC` | 同端口有 `FEC_BEF_COR_ER`；同容器无 `IN_PWR_LOW` | 1.7 |
| `fp_b2_exc_high` | SDH 层 `B2_EXC` | 承载跨段无 `OTS_LOS`；同 NE 无更强根因签名 | 1.6 |
| `fp_b1_exc_high` | SDH 层 `B1_EXC` | 同上 | 1.6 |

**为什么 `NOT` 子句关键。** 模拟器的 L2 关键词扩展会把 `POWER_ABNORMAL`、`BEFFEC_EXC`、`IN_PWR_HIGH` 这类告警拉进它们并不属于的级联（匹配"input power" / "FEC"等描述文字）。若依字面如 `Design/08 §5` 那样按名字直接 trump，会出现大量误报。上表的 `NOT Exists ...` 守卫正是让指纹保持精确的关键。

### §3 —— 层级级联（`layer_cascade.py`）

在同一端口内，上游层抑制下游层：

```
OTS → OMS → OCH → OTU → SDH/FEC/ODU/OPU → client
```

两条精化规则：
- `OTS_LOS` 和 `MUT_LOS` 是 **支配性** 告警 —— 它们抑制同端口的所有其它告警，无论层级（LOS 意味着比特流已断，上层的位错统计已无意义）。
- 对其它层级对：若同端口 `layer(a1) < layer(a2)`，则 `Secondary(a2, a1)`。

### §4 —— 拓扑驱动级联（`topology_cascade.py`）

只有当拓扑允许时才触发的跨端口传播：

| 规则 | 结论 |
|---|---|
| 对端口 `R_LOS` | 跨段另一端的 `R_LOS` 是 `OTS_LOS` 的 `Secondary` |
| Supporting-link 级联 | 所有以该跨段为 `supporting-link` 的 OCH/OTU 告警都是 `OTS_LOS` 的 `Secondary` |
| 隧道级联 | 底层 OCH 经过该跨段的 ODU 上出现的 `ODU_PM_SSF` / `ODU_PM_BDI` 是 `OTS_LOS` 的 `Secondary` |

### §5 —— 设备王牌（`equipment.py`）

硬规则：设备级事件抑制其位置上的一切。

| 王牌告警 | 抑制范围 |
|---|---|
| `POWER_ABNORMAL` | 同一 NE 的所有告警 |
| `BD_OFFLINE`     | 同一板的所有告警 |
| `HARD_BAD`       | 同一板的所有告警 |
| `FAN_FAIL`       | 同一 NE 的所有 `TEMP_OVER` |

**重要：** §5 的规则 **只发** `Secondary` 记录。它们本身 **不挑根因** —— 只有 §8 指纹做。这是 `reasoner.py` 顶部记录的关键设计决策：单凭名字 trump（即 `Design/08 §5` 字面写法）会把 L2 扩展进来的 `POWER_ABNORMAL` 噪声错误地升格为根因。

---

## 程序化用法

```python
from src.simulator import db as simdb, yang_loader
from src.rules import infer_root

topo = yang_loader.load()
conn = simdb.open_db("data/incidents.db")
alarms = conn.execute(
    "SELECT id, alarm_name, ne, board, port, severity, layer, "
    "       container_id, t_offset_ms, t_observed_ms, is_root, group_id "
    "FROM alarms WHERE incident_id=? ORDER BY id",
    (42,),
).fetchall()
# 实际使用时：把行转换为推理器需要的 dict

inf = infer_root(incident_id=42, alarms=alarms, topo=topo)
print(f"根因  = {inf.root_alarm_name}（id {inf.root_alarm_id}）")
print(f"故障  = {inf.fault_type}")
print(f"原因  = {inf.source}")
for x in inf.inferred[:10]:
    print(f"  {x.kind} alarm={x.alarm_id} parent={x.parent_id} src={x.source}")
```

推理器是 **无状态** 的 —— 每次调用相互独立，无需初始化，v3 规模（约 50 告警）下每个场景约 10 ms。

### 与实时 KG 引擎的集成

`src/kg_engine/subscribers/rule_engine.py` 把 `infer_root` 包装为订阅者：在每个 `incident.boundary` 上，发出标签为 `source="rule:fingerprint"` 的 `RootCause` 三元组。这样，从模拟器 GUI 点出的实时告警级联就能在实时获得根因预测。

### 与批量 KG 构建器的集成

`src/kg/correlation.py` 单独消费 §2 的 R1–R4 关联逻辑（不经本模块 —— R1–R4 在 KG 构建期作为 `Correlated` 边产出，而不是在 RCA 推理期）。

---

## 推理器期望的输入

### Alarms —— dict 列表，对应 `alarms` 表的每一行

必填字段（与 `src/simulator/db.py` schema 对齐）：

| 字段 | 类型 | 谁用 |
|---|---|---|
| `id`            | int | 所有规则（告警身份） |
| `alarm_name`    | str | 所有规则（如 `OTS_LOS`、`BEFFEC_EXC`） |
| `ne`            | str | §5、§8（NE 级范围） |
| `board`         | str | §5、§8（板级范围） |
| `port`          | str | §3、§4、§8（端口级范围） |
| `layer`         | str | §3（层级顺序） |
| `container_id`  | str | §4、§8（跨段 / OTU 管道 / 通道） |
| `t_offset_ms`   | int | 仅 fallback 使用（同层平局打破） |
| `t_observed_ms` | int | **不使用** —— 保留以便与 ML 流水线对齐 |
| `is_root`       | int | 仅 `evaluate.py` 校对真值 |
| `group_id`      | str | 仅 `evaluate.py` 拆分复合场景 |

推理器有意忽略 `t_observed_ms`。原因详见 `Design/08 §6` 与项目的轮询延迟告诫：约 80% 的场景里，根因告警 **不是** 时间最早的那条；任何基于时间的规则在多数情况下都是错的。

### Topology —— `src.simulator.topology.Topology`

加载自 `data/yang/*.json` 的完整 YANG 拓扑对象。§4 与 §8 在跨段 / OTU 管道 / OCh 隧道推理中会用到：

- `topo.optical_links` —— 跨段列表（OMS 跨段）
- `topo.otn_links` —— OTU 管道（其 `supporting_link_id` 指向所支撑的跨段）
- `topo.otn_tunnels` —— ODU 隧道（其 `ero` 是 OTU 管道名列表）
- `topo.nes` —— NE 清单（板、端口、对端口推断）

---

## 已知失败模式

来自当前 0.9% 的误判（见 `memory/rules_baseline.md`）：

1. **`config_mismatch` 90% 类别准确率。** 失败的 10% 是 `OPU_PLM` 被 drop_prob=5% 丢掉，§8.8 的"三告警共现"前提因此不能触发。fallback 仍能正确挑出最低层非次生告警，只是 `fault_type` 为空。
2. **`fiber_cut` 96% 类别准确率。** 失败的 4% 同样源于 drop_prob —— `MUT_LOS` 被丢，§8.1 指纹无法触发；但 `OTS_LOS` 仍是正确的根因。
3. **`fec_pre_cor_high` 97%、`rx_power_drift` 98%、`ne_power_off` 99%。** 都是长尾的 drop_prob 漏判。fallback 通常仍能挑对根因，只是缺 `fault_type`。

这些恰恰是 GNN 能取胜的场景 —— 结构信号（拓扑 + 局部性 + 时间）即使丢了一条告警仍然存在，但规则指纹的前提不存在了。详见 `Design/11_gnn_training_plan.md` §10 风险。

---

## 相关代码

- `src/simulator/` —— 产出本模块消费的告警行。
- `src/kg_engine/subscribers/rule_engine.py` —— 把本模块包装为实时订阅者。
- `src/kg/correlation.py` —— 实现 §2 R1–R4 关联边（与 RCA 推理是不同关注点）。
- `docs/03_rules_catalog.md` —— 把 `Design/08` 中每条规则与实现它的代码相互对应的实现状态表。
