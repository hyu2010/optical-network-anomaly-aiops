# `src/simulator` —— 告警 + PPE 生成器

本包为 Huawei OptiX OSN 9800 光传送网络生成**真实的告警雪崩**，并自 v3 起额外生成**功率分布估计（Power Profile Estimation, PPE）轨迹**。注入一个*故障*（光纤断、单板掉电、风扇失效……），仿真器遍历 YANG 拓扑，产出一条真实 NMS 会观测到的告警序列；自 `ppe.py` 落地后，还同时产出对应的逐通道光纤纵向功率曲线 —— 相干光接收机会从其 DBP 中间态重建出来的那条曲线。两条模态都写入 `data/incidents.db`，作为下游 RCA 流水线的有标注训练集。

仿真器**不是**设备固件仿真。物理网络（拓扑、业务、库存）以三份符合 YANG 的 JSON 文件形式存放在 `data/yang/` 下；仿真器把它们当作运行态，并用 Python 规则在其上跑。

更深入的阅读：
- `docs/02_simulator_user_guide.md` —— **运维**用户指南（UI 操作、SQL 用法、ML 工作流）。
- `Design/02_simulator_design.md` —— **架构**设计。
- `Design/05_fault_event_catalog.md` —— 11 个 ★ 故障规则与仿真器实际发射的 21 个告警名。
- `Design/12_ppe_simulation.md` —— PPE 生成器设计。

---

## 1. 快速上手

```bash
# 安装（venv 创建好后，bash / PowerShell 二选一）
python -m venv .venv
.venv/Scripts/activate           # bash
.venv\Scripts\Activate.ps1       # PowerShell
pip install -r requirements.txt

# （A）Web UI —— 一键触发、实时告警与拓扑视图
python -m src.app.server
# 打开 http://127.0.0.1:5000/

# （B）批量 CLI —— 5120 场景 × ~10 万条告警，~11 秒完成
python -m src.simulator.generate_dataset --clear --samples-per-target 20

# （C）PPE 单图绘制 —— 看一个故障的 (通道, 跨段) 曲线
python ppe_demo.py fiber_cut span_OMS3_BC --position-km 25
```

---

## 2. 本包文件清单

```
src/simulator/
├── yang_loader.py          加载 data/yang/*.json → 内存 Topology
├── topology.py             Topology / NE / Board / Port / Link / Tunnel 数据类
├── faults.py               11 条故障规则 → 种子告警
├── engine.py               L1 抑制图前向遍历 + L2 关键词扩展
│                           + apply_polling_delay + apply_suppression
├── ppe.py                  11 个配对 PPE 发射器 + 基线合成器 + 异常检测器
├── format_alm.py           华为 NMS 转储格式渲染器
├── db.py                   SQLite 写入器（incidents + alarms 表）
├── generate_dataset.py     批量生成器（CLI）
├── inspect_db.py           数据集统计 CLI
└── run_demo.py             单故障烟测脚本
```

**输入**（位于 `data/` 下）：

| 文件 | 用途 |
|---|---|
| `data/yang/{topology,tunnels,inventory}.json` | 网络（6 个枢纽 A–F、8 段 OMS、6 条 OCh 隧道） |
| `data/alarm_catalog_9800.json`（+ `alarm_catalog.json` 兜底） | 796 / 816 条告警定义 |
| `data/alarm_suppression.json` | 50 条人工整理的抑制边（L1 输入 + 第 5 阶段） |
| `data/alarms_detail.json` | 解析过的 "Possible Causes" 文本（L2 关键词输入） |

**输出**（位于 `data/` 下）：

- `data/incidents.db` —— 数据集（已 gitignore）。
- `ppe_traces` / `ppe_anomalies` 两张表将在 **P-2** 阶段落地（见 `Design/12 §12`）。

---

## 3. 架构

```
┌──────────────────┐                       ┌──────────────────────────┐
│  YANG 实例文件   │                       │  告警目录                 │
│  (data/yang/     │  加载拓扑 + 隧道 ►   │  alarm_catalog_9800.json │
│   *.json)        │  + 库存               │  alarm_catalog.json(8800)│
└──────────────────┘                       │  alarm_suppression.json  │
        │                                  │  alarms_detail.json      │
        ▼                                  └──────────────────────────┘
┌─────────────────────────────────────────────────────────────────────┐
│ src/simulator/                                                      │
│                                                                     │
│   yang_loader → Topology  (NE / Board / Port / Link / Tunnel)       │
│                                                                     │
│   faults.CATALOG[fault_type] → 种子告警                              │
│                                ─────┬─────────────────────────────  │
│                                     │                               │
│         ┌───────────────────────────┴──────────────────────────┐    │
│         │                                                       │   │
│         ▼ engine.expand_cascade  → 30–60 条告警                 │   │
│           ▸ L1 抑制图前向遍历                                   │   │
│           ▸ L2 原因关键词扩展                                   │   │
│           ▸ apply_polling_delay  → t_observed_ms（轮询抖动）     │   │
│           ▸ apply_suppression    → 原始 + 操作员视图              │   │
│                                                                 │   │
│         ▼ ppe.generate（与同一 fault_type + 参数配对） ───────┘    │
│           ▸ baseline(channel, span) → P_0(z)                        │
│           ▸ 各故障特有 δP 形状（step / slope / null / rx_offset）   │
│           ▸ 高斯噪声 σ(z) = σ_0 + σ_per_km · z_累计                 │
│           ▸ apply_polling_delay → t_observed_ms（30 s 节拍）        │
│                                                                     │
│   db.record  ──────────► data/incidents.db                          │
│                          (incidents、alarms；ppe_* 表在 P-2 落地)    │
└─────────────────────────────────────────────────────────────────────┘
                          │
                          ▼
            src/app/server.py（Flask Web UI）
            ML / KG 管线（src/kg/、src/rules/、src/kg_engine/）
```

告警流水线五个阶段在 `docs/02_simulator_user_guide.md` §2 中逐阶段介绍。PPE 流水线见 `Design/12_ppe_simulation.md`。

---

## 4. 十一条故障规则（告警 + PPE 配对）

每个故障在 `faults.CATALOG` / `ppe.CATALOG` 中是一个 `(target_kind, 告警规则, PPE 发射器)` 三元组。

| 故障 id                   | 目标类型 | 主要告警                                          | PPE 特征                              |
|---|---|---|---|
| `fiber_cut`               | link    | OTS_LOS、MUT_LOS、R_LOS、OTU_SSF、OTU_BDI、ODU_PM_SSF | `pos_km` 处 −30 dB 台阶，之后 NaN     |
| `fiber_attenuation_high`  | link    | IN_PWR_LOW、BEFFEC_EXC、OTU_DEG                    | 区间 `[a, b]` 上的斜率                 |
| `connector_dirty`         | link_z  | 无（亚阈值），或 Minor 级 BEFFEC_EXC（≥ 1.5 dB）  | `z_km` 处 0.5–3 dB 局部台阶           |
| `b1_exc_high`             | link    | B1_EXC、OTU_DEG                                    | 干净（电域 —— 反例）                   |
| `b2_exc_high`             | link    | B2_EXC、OTU_DEG                                    | 干净                                   |
| `fec_pre_cor_high`        | link    | BEFFEC_EXC、FEC_BEF_COR_ER、OTU_DEG                | 干净                                   |
| `rx_power_drift`          | port    | IN_PWR_LOW 或 IN_PWR_HIGH、IN_PWR_ABN              | 仅最后一个样本上 rx_offset ±2 dB       |
| `ne_power_off`            | ne      | POWER_ABNORMAL、OTS_LOS、MUT_LOS、R_LOS……          | 全部 degraded 哨兵                      |
| `board_power_off`         | board   | BD_OFFLINE、R_LOS                                  | OTU 板 → degraded；OA / FIU → 干净     |
| `fan_failure`             | ne      | FAN_FAIL、TEMP_OVER                                | 各下波接收机上 ±0.5 dB 小漂移          |
| `config_mismatch`         | port    | OPU_PLM、OTU_TIM、ODU_PM_TIM                       | 干净                                   |

`connector_dirty` 是 **PPE 优先的演示故障** —— 模拟光纤接头/熔接点处的污染或松动。其幅度（0.5–3 dB）故意设计得很小：大多数情况下**不产生任何告警**（仅靠告警的系统完全看不到），但 PPE 会在接头的 `z_km` 处显示一个清晰的台阶。只有较强的污染（≥ 1.5 dB）才会触发一条 Minor 级别的 `BEFFEC_EXC`，且该告警**不被标记**为 `is_root` —— 诊断信号在 PPE 中，不在告警中。这使得它成为展示 PPE 价值的最佳演示故障：仅靠告警的 RCA 会漏掉所有低幅度案例；启用 PPE 的 RCA 则能全部捕获，并附带 z_km 定位。

五条"干净 PPE"是刻意设计：告警仍然跳，但沿光纤的功率分布无异常 —— 这给 GNN 一个判别基础，让它能把电域 / 接收机 / 配置故障与光纤介质故障区分开。每个 PPE 发射器还会给一条 `PpeAnomaly` 打 `is_root=True` 真值，与告警侧的 `is_root` 保持一致。

---

## 5. 三种驱动方式

### 5.1 Web UI —— 一键触发

```bash
python -m src.app.server                 # http://127.0.0.1:5000/
```

在浏览器中：从下拉框中挑一个故障、挑一个目标（link / NE / board / port，自动按已加载拓扑填充），可选地切换 **Suppress**（运维视图 vs. 原始视图），点击 **Trigger**。每次点击会向 `data/incidents.db` 写两行场景（原始 + 抑制后），并把告警雪崩渲染到实时告警面板。

复合（双根因）场景：勾选 **"Add a second root cause"**，配置 R2 故障，可选填两根因之间的时间间隔，点 Trigger。告警面板上会显示合并的时间线，R1 / R2 chip 区分。完整步骤见 `docs/02_simulator_user_guide.md` §3.1–§3.2。

### 5.2 批量 CLI —— ML 训练规模

```bash
# 默认：5120 场景 × ~10 万条告警，约 11 秒
python -m src.simulator.generate_dataset --clear --samples-per-target 20

# 常用参数
python -m src.simulator.generate_dataset --clear \
    --samples-per-target 100 \   # 每个 (故障, 目标) 组合的样本数
    --jitter-ms 15 \             # 每条告警 ±15 ms 物理时间抖动
    --drop-prob 0.05 \           # 非根因告警 5% 概率掉
    --seed 42                    # 复现种子
```

生成器枚举每一对 `(fault_type × 合法目标)`，每对采 `samples-per-target` 份带抖动的样本，每份再写原始 + 抑制两份视图。PPE 接入在 **P-2** 阶段（`--with-ppe` 开关）。

### 5.3 编程接口 —— 直接调用 engine + ppe

```python
from src.simulator import yang_loader, faults, engine, ppe

t = yang_loader.load()

# 告警侧
seeds = faults.fiber_cut(t, "span_OMS3_BC")
cascade = engine.expand_cascade(seeds, faults.keywords_for("fiber_cut"))
cascade = engine.apply_polling_delay(cascade)
suppressed = engine.apply_suppression(cascade)

for a in cascade:
    print(f"{a.name:18s}  ne={a.ne:2s}  t_phys={a.t_offset_ms:>4d}ms  "
          f"t_obs={a.t_observed_ms:>5d}ms  root={a.is_root}")

# PPE 侧（与同一故障配对）
traces = ppe.generate(t, "fiber_cut", "span_OMS3_BC", position_km=25.0)
traces = ppe.apply_polling_delay(traces)
```

两侧真值对齐 —— 每条 PPE 轨迹的 `group_id` 与告警侧的 `group_id` 相同，`is_root=True` 在两个模态上一致打到对应记录。

### 5.4 PPE 单图绘制 —— 不写数据库

```bash
python ppe_demo.py <故障类型> <目标> [其它参数]
```

输出 `ppe_demo.png`，按 (通道, 跨段) 显示一格一图 —— 灰色虚线是基线，红色实线是观测样本，异常用竖线 / 阴影标注。指定 `--seed` 则可复现。算例见 §6。

---

## 6. PPE 算例画廊

三种代表性特征。`--seed 7` 下均可复现。

### 6.1 `fiber_cut(span_OMS3_BC, position_km=25)` —— R4 标准 demo 故障

```bash
python ppe_demo.py fiber_cut span_OMS3_BC --position-km 25
```

**一个故障，两个通道同时受影响：**

| 子图 | 行为 |
|---|---|
| ch2 在 OMS2_AB（受影响跨段上游） | 0 → −20 dBm 线性衰减，α=0.20 dB/km |
| ch2 在 OMS3_BC（被剪断的跨段） | 跟随基线到 z=25 km，硬台阶，之后 NaN（阴影标"no light"） |
| ch4 在 OMS5_DB（上游） | 80 km 内 0 → −16 dBm 线性衰减 |
| ch4 在 OMS3_BC（同一次断纤） | 同样的 25 km 台阶 —— 同一个物理故障，第二个接收机看到 |

右侧两个子图几乎完全相同 —— 同一次断纤被两个不同的 OCh 下波接收机各自观测一次。这正是 R4 想利用的跨通道相关性。

### 6.2 `fiber_attenuation_high(span_OMS3_BC, seg=[15,45], extra=0.15 dB/km)`

```bash
python ppe_demo.py fiber_attenuation_high span_OMS3_BC --seg 15,45 --extra 0.15
```

**软故障的斜率特征：**

- 红色阴影区域 `[15, 45]` km 是受影响的子段。
- 区域内：观测曲线相对基线**向下**弯折（额外 0.15 dB/km 斜率）。
- 过了 45 km：观测曲线与基线**平行但偏低** —— 累计损耗向下游传递（30 km × 0.15 dB/km ≈ 4.5 dB）。
- 上游跨段（OMS2_AB、OMS5_DB）保持干净，斜率仅出现在 OMS3。

### 6.3 `rx_power_drift(C, direction=low)`

```bash
python ppe_demo.py rx_power_drift C --direction low
```

**判别用的反向证据信号：**

6 条轨迹、4 条通道（ch1–ch4 都在 C 下波）。每条轨迹**沿光纤都是干净的** —— 但在每条通道的**最后一段**（即下波那一段）的**最后一个样本**上，出现明显的 −2 dB 凹陷。

这正是规则基线当前拿不到的信号：告警侧 `IN_PWR_LOW` 跳了，但 PPE 证据**排除**了跨段故障 —— 唯一剩下的解释就是接收侧漂移。P-5 的 R7 规则把这条逻辑编码进推理器。

### 其他值得跑一下的故障

```bash
python ppe_demo.py connector_dirty span_OMS3_BC --z-km 25 --magnitude 1.2  # 仅 PPE：零告警，清晰台阶
python ppe_demo.py connector_dirty span_OMS3_BC --z-km 40 --magnitude 2.5  # 超阈值：1 条 Minor 告警 + PPE 台阶
python ppe_demo.py ne_power_off C                       # C 上 5 条 OCh 全部 degraded
python ppe_demo.py b1_exc_high span_OMS3_BC             # 告警跳但 PPE 干净
python ppe_demo.py fan_failure C                        # 每个下波接收机上 ±0.5 dB 漂移
python ppe_demo.py config_mismatch C                    # PPE 干净（纯配置故障）
python ppe_demo.py board_power_off C C.OTU              # 与 ne_power_off 类似但限定到板
```

---

## 7. 数据集（`generate_dataset.py` 的输出）

### 7.1 SQLite Schema —— 当前（P-1）

`data/incidents.db` 有两张表：

- **`incidents`** —— 每个（故障、目标、样本、视图）一行。列：`id`、`created_at`、`fault_type`、`fault_target`、`target_kind`、`suppressed`、`topology_id`、`note`。
- **`alarms`** —— 每个场景多行告警。列：`id`、`incident_id`、`alarm_name`、`ne`、`board`、`port`、`severity`、`layer`、`container_id`、`t_offset_ms`、`t_observed_ms`、`is_root`、`group_id`。

PPE 存储（`ppe_traces` + `ppe_anomalies` 两张表）将在 **P-2** 阶段落地，详见 `Design/12_ppe_simulation.md` §5。

### 7.2 如何使用数据集

完整流程 —— SQL 用法、训练时间列的选择、KG 构建、复合场景生成、训练建议：见 `docs/02_simulator_user_guide.md` §§4–6。

---

## 8. 常用操作

### 清空重生成
```bash
python -m src.simulator.generate_dataset --clear --samples-per-target 30
```

### 切换拓扑（指向其他 YANG 目录）
```bash
export FMA_YANG_DIR=/path/to/your/yang        # bash
$env:FMA_YANG_DIR = "C:\path\to\your\yang"    # PowerShell
python -m src.simulator.generate_dataset --clear
```

### 查看单个场景
```bash
python -m src.simulator.format_alm 42       # 华为 NMS dump 格式
python -m src.simulator.inspect_db          # 数据集整体统计
```

### 单个故障的 PPE 图
```bash
python ppe_demo.py fiber_cut span_OMS3_BC --position-km 25 --out my_cut.png
```

### 跑测试套件
```bash
python -m pytest tests/
# 仅跑 PPE 生成器测试：
python -m pytest tests/test_ppe.py -v
```

---

## 9. PPE 生成器 —— API 速查

```python
from src.simulator import ppe

# 故障目录与 faults.CATALOG 的键完全对齐。
ppe.CATALOG.keys()
# dict_keys(['fiber_cut', 'fiber_attenuation_high', 'b1_exc_high', ...])

# 派发 —— 参数与 faults.<fault_type> 完全一致。
traces = ppe.generate(t, "fiber_cut", "span_OMS3_BC",
                      position_km=25.0, rng=random.Random(7))

# 基线合成器（内部使用，外部也可直接调）。
z_km, p0_dbm = ppe.baseline(t, t.wdm_tunnels["och_ch2"], "span_OMS3_BC")

# 异常检测器 —— 在含噪样本上运行；输出 is_root=False。
detector_view = ppe.detect_anomalies(traces[1])

# 轮询延迟抖动（方案 A：在 [0, 30 s] 上均匀抖动）。
delayed = ppe.apply_polling_delay(traces, rng=random.Random(99))
```

数据类（`PpeTrace`、`PpeAnomaly`）定义在 `ppe.py` 头部。每条 `samples` 是一个 `float32[N]` 的 numpy 数组（N = `length_km / Δz_m`），NaN 表示"无光"。`baseline` 是无噪参考曲线。`anomalies` 携带发射器埋的真值（`is_root=True`）。

---

## 10. 延伸阅读

| 主题 | 文档 |
|---|---|
| 运维指南（UI 操作、SQL 用法、ML 工作流） | `docs/02_simulator_user_guide.md` |
| 仿真器架构（告警流水线详解） | `Design/02_simulator_design.md` |
| YANG schema（含 PPE 相关的 `nfms:*` augmentation） | `Design/03_yang_topology_plan.md` |
| KG schema + ML 方案 | `Design/04_knowledge_graph_plan.md` |
| 故障目录与告警名清单 | `Design/05_fault_event_catalog.md`（特别是 §8） |
| OSN 9800 vs 8800 告警差异 | `Design/06_osn9800_supplement.md` |
| 实验室 mesh 拓扑（v3 6 枢纽） | `Design/07_lab_mesh_topology.md` |
| 规则基线（R1–R4 + 计划中的 R5–R7 PPE 规则） | `Design/08_root_cause_rules.md` |
| 实时流式 KG 引擎 | `Design/10_realtime_kg_engine.md` |
| GNN 训练方案 | `Design/11_gnn_training_plan.md` |
| **PPE 设计 + 接入方案** | `Design/12_ppe_simulation.md` |
