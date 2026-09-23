# 水培营养液配比求解 API

用 Python / FastAPI / SQLite 构建的水培营养液配比（mixing）求解服务。用户提交若干**浓缩液**各自的氮（N）、磷（P）、钾（K）及微量元素（Ca / Mg / S / Fe / Mn / Zn / B / Cu / Mo）浓度、可用体积与目标配方体积，服务用 **minimax 线性规划**（scipy / HiGHS）求出各浓缩液与稀释水的用量，目标是：

> **最小化各元素实际配出量相对目标量的最大相对偏差。**

求解中同时满足以下**硬约束**：

- **总量约束**：浓缩液 + 稀释水 = 目标配方体积；浓缩液合计占比可设上限。
- **单液上限**：每瓶浓缩液用量不超过其可用体积（可另设 `max_volume`）。
- **稀释水约束**：可设稀释水最小用量（强制保留稀释空间）。
- **原水矿物质基线**（可选）：稀释水本身可能含有矿物质，可通过 `source_water` 传入原水各元素浓度（mg/L）；每个元素的实际量按 **浓缩液贡献 + 原水浓度 × 求得的稀释水量** 计算（原水贡献随求解出的水量线性变化，**不会**按目标总体积预扣一笔固定原水量）。未传时按零处理。

当容差范围内无可行解时，会在 `violations` 中返回**具体超限元素与原因**（瓶子到顶导致供给不足、或成分配比耦合导致某元素无法压低等），并附带最优折中方案供参考。

## 一键启动（Docker Compose）

```bash
docker compose up --build -d
```

- 服务端口：**`8000`**（映射到宿主机同端口）
- 交互式文档（Swagger UI）：<http://localhost:8000/docs>
- SQLite 数据文件保存在命名卷 `hydroponic-data`（容器内 `/data/hydroponic.db`），**容器启动时自动建表初始化**。

停止 / 清理：

```bash
docker compose down          # 停止（保留数据卷）
docker compose down -v       # 停止并删除数据
```

<details>
<summary>不用 Docker，本地运行</summary>

```bash
pip install -r requirements.txt
DATABASE_PATH=./hydroponic.db uvicorn app.main:app --host 0.0.0.0 --port 8000
```

</details>

## 接口一览

| 方法 | 路径 | 说明 |
| ---- | ---- | ---- |
| POST | `/api/solve` | **试算**：仅求解，不入库 |
| POST | `/api/recipes?force=false` | **保存配方版本**：同名配方自动追加版本号 |
| GET | `/api/recipes` | 配方列表 |
| GET | `/api/recipes/{id}` | 配方详情（含最新版本号） |
| GET | `/api/recipes/{id}/versions` | 某配方的全部版本 |
| GET | `/api/recipes/{id}/versions/{v}` | 读取某版本完整内容（请求 + 结果） |
| GET | `/api/recipes/{id}/versions/{v}/export` | **导出该版本 JSON 文件** |
| POST | `/api/recipes/{id}/versions/{v}/consumption-forecast` | **库存消耗预测**：按灌溉计划逐批次等比例扣减浓缩液，输出每日消耗/剩余与首个不足日期（支持 `suspended_dates` 本地停灌日） |
| POST | `/api/irrigation/plan` | **灌溉计划**：按 IANA 时区生成逐日执行批次（DST/跨午夜/容量校验/本地停灌日） |
| GET | `/health` | 健康检查 |

### 单位约定

- 体积：**mL**；元素浓度：**mg/L**（以纯元素计，如 N、P、K、Ca、Mg…）。
- 目标量（mg）= 目标浓度 × 目标体积 ÷ 1000。
- 传入 `source_water` 时，元素实际量（mg）= 浓缩液浓度 × 浓缩液用量 ÷ 1000 **+ 原水浓度 × 求得稀释水量 ÷ 1000**；响应中按元素分别给出 `concentrate_amount_mg`（浓缩液贡献）、`source_water_amount_mg`（原水贡献）与合计 `actual_amount_mg`。
- 元素键大小写不敏感（`n`、`ca`、`FE` 都会归一化，`source_water` 同理）；支持 `N/P/K/Ca/Mg/S/Fe/Mn/Zn/B/Cu/Mo`；拼错字段名（如把 `concentrations` 写成 `concentrates`）会直接返回 422 而不是被静默忽略。

## curl 示例

### 1. 试算

目标：配 10 L 工作液，N=180、P=50、K=85 mg/L；A 液富含 N/K，B 液富含 P/K；浓缩液最多占总体积 20%，至少加 7 L 稀释水，容差 5%。

下面这组数据恰好存在精确解：**A 液 1500 mL + B 液 125 mL + 水 8375 mL**。

```bash
curl -s http://localhost:8000/api/solve \
  -H 'Content-Type: application/json' \
  -d '{
    "target_volume_ml": 10000,
    "targets": {"concentrations": {"N": 180, "P": 50, "K": 85}},
    "concentrates": [
      {"name": "A液", "available_volume": 2000,
       "concentrations": {"N": 1200, "K": 400}},
      {"name": "B液", "available_volume": 2000,
       "concentrations": {"P": 4000, "K": 2000}}
    ],
    "max_concentrate_fraction": 0.20,
    "min_water_ml": 7000,
    "tolerance": 0.05
  }'
```

成功时返回（节选）：

```json
{
  "feasible": true,
  "optimal": true,
  "max_relative_deviation": 0.0,
  "water_volume_ml": 8375.0,
  "concentrate_total_ml": 1625.0,
  "allocations": [
    {"concentrate": "A液", "volume_ml": 1500.0, "cap_ml": 2000.0},
    {"concentrate": "B液", "volume_ml": 125.0,  "cap_ml": 2000.0}
  ],
  "elements": [
    {"element": "N", "target_concentration": 180.0, "actual_concentration": 180.0,
     "target_amount_mg": 1800.0, "actual_amount_mg": 1800.0,
     "relative_deviation": 0.0, "status": "ok"},
    {"element": "P", "target_concentration": 50.0, "actual_concentration": 50.0,
     "relative_deviation": 0.0, "status": "ok"},
    {"element": "K", "target_concentration": 85.0, "actual_concentration": 85.0,
     "relative_deviation": 0.0, "status": "ok"}
  ]
}
```

### 2. 无可行解时的诊断信息

若浓缩液库存不足以达到目标（如 A 液只剩 50 mL），响应中 `feasible=false`，并给出具体元素与约束原因：

```json
{
  "feasible": false,
  "message": "无可行解：最优最大相对偏差为 96.67%，超过容差 5.00%，详见 violations。",
  "violations": [
    {
      "code": "ELEMENT_SHORTAGE",
      "element": "N",
      "actual": 60.0,
      "limit": 1710.0,
      "message": "元素 N 最多只能配出 60.000 mg，低于目标下限 1710.000 mg（目标 1800.000 mg，容差 5%）；受浓缩液可用体积/单液使用量上限约束，已到上限的浓缩液：A液。"
    }
  ]
}
```

`violations.code` 取值：

- `WATER_MIN_EXCEEDS_VOLUME` —— 稀释水下限超过目标总体积；
- `CONCENTRATE_FRACTION_CONFLICT` —— 稀释水下限与浓缩液最大占比互相冲突；
- `ELEMENT_SHORTAGE` —— 某元素即使所有相关浓缩液用到上限仍不足（附到达上限的瓶子）；
- `ELEMENT_SURPLUS` —— 满足其他元素下限时该元素必然超量（浓缩液成分耦合）；
- `ELEMENT_DEVIATION` —— 多个元素互相制约，最优折中仍超出容差；
- `TOLERANCE_UNREACHABLE` —— 兜底说明。

### 2b. 原水矿物质基线（`source_water`）

自来水 / 井水本身往往含钙镁等矿物质。用 `source_water` 传入原水各元素浓度后，求解器把**原水浓度 × 求得的稀释水量**计入该元素的实际配出量，从而减少（甚至不再需要）相应浓缩液；minimax 容差、体积等式、单液上限等语义完全不变。

目标：配 10 L 工作液，Ca=100、Mg=40 mg/L；原水实测 Ca=40、Mg=30 mg/L；浓缩液 C 含 Ca=640、Mg=130 mg/L，最多占总体积 20%。

> 该组数据存在精确解：**C 液 1000 mL + 原水 9000 mL**
> Ca：1000 mL × 640 mg/L ÷ 1000 + 9000 mL × 40 mg/L ÷ 1000 = **640 + 360 = 1000 mg**
> Mg：1000 mL × 130 mg/L ÷ 1000 + 9000 mL × 30 mg/L ÷ 1000 = **130 + 270 = 400 mg**
>
> 注意原水贡献是 **40 mg/L × 求得的 9000 mL = 360 mg**，而不是按目标总体积预扣的 40 mg/L × 10 L = 400 mg——稀释水量本身是求解结果（浓缩液占了 1000 mL）。

```bash
curl -s http://localhost:8000/api/solve \
  -H 'Content-Type: application/json' \
  -d '{
    "target_volume_ml": 10000,
    "targets": {"concentrations": {"Ca": 100, "Mg": 40}},
    "concentrates": [
      {"name": "C液", "available_volume": 2000,
       "concentrations": {"Ca": 640, "Mg": 130}}
    ],
    "source_water": {"concentrations": {"ca": 40, "mg": 30}},
    "max_concentrate_fraction": 0.20,
    "tolerance": 0.05
  }'
```

返回（节选）按元素同时给出**浓缩液贡献、原水贡献与合计实际量**：

```json
{
  "feasible": true,
  "water_volume_ml": 9000.0,
  "concentrate_total_ml": 1000.0,
  "allocations": [
    {"concentrate": "C液", "volume_ml": 1000.0, "cap_ml": 2000.0}
  ],
  "elements": [
    {"element": "Ca", "target_concentration": 100.0, "actual_concentration": 100.0,
     "target_amount_mg": 1000.0,
     "concentrate_amount_mg": 640.0,
     "source_water_amount_mg": 360.0,
     "actual_amount_mg": 1000.0,
     "relative_deviation": 0.0, "status": "ok"},
    {"element": "Mg", "target_concentration": 40.0, "actual_concentration": 40.0,
     "target_amount_mg": 400.0,
     "concentrate_amount_mg": 130.0,
     "source_water_amount_mg": 270.0,
     "actual_amount_mg": 400.0,
     "relative_deviation": 0.0, "status": "ok"}
  ]
}
```

要点：

- `source_water` 可选；不传（或只给部分元素）时对应原水浓度按 **0** 处理，结果与旧版完全一致（各元素 `source_water_amount_mg` 为 0）。
- 元素名同样做大小写归一化（`ca` → `Ca`），未知元素 / 负数浓度返回 422。
- 原水本身可能让某元素**必然超标**：例如目标 Ca 仅 10 mg/L，而原水 Ca=40 mg/L，即使浓缩液一滴不加，原水贡献也有约 400 mg（随求得水量浮动）。此时 `feasible=false`，`violations` 用同一贡献模型给出 `ELEMENT_SURPLUS` 与可达下界；供给不足时 `ELEMENT_SHORTAGE` 的可达上界同样包含原水贡献。
- 保存配方版本与导出 JSON 会原样保留 `source_water` 输入；读取历史版本（无该字段）不受影响。

### 3. 保存配方版本

```bash
curl -s -X POST http://localhost:8000/api/recipes \
  -H 'Content-Type: application/json' \
  -d '{
    "name": "叶菜通用配方",
    "description": "生菜/小白菜通用",
    "note": "v1 基线",
    "request": {
      "target_volume_ml": 10000,
      "targets": {"concentrations": {"N": 180, "P": 50, "K": 85}},
      "concentrates": [
        {"name": "A液", "available_volume": 2000,
         "concentrations": {"N": 1200, "K": 400}},
        {"name": "B液", "available_volume": 2000,
         "concentrations": {"P": 4000, "K": 2000}}
      ],
      "max_concentrate_fraction": 0.20,
      "min_water_ml": 7000,
      "tolerance": 0.05
    }
  }'
```

> 超出容差的试算默认不允许保存（HTTP 422 并返回求解结果）；确认要留档时加 `?force=true`。
> 同名配方再次保存会自动生成 `version: 2、3 …`，历史版本不可变。

### 4. 查询 / 导出 JSON

```bash
# 列出所有配方
curl -s http://localhost:8000/api/recipes

# 某配方的版本列表
curl -s http://localhost:8000/api/recipes/1/versions

# 读取第 1 版完整内容
curl -s http://localhost:8000/api/recipes/1/versions/1

# 导出为 JSON 文件（Content-Disposition attachment）
curl -s -OJ http://localhost:8000/api/recipes/1/versions/1/export
```

## 灌溉计划（POST /api/irrigation/plan）

给定 **IANA 时区**、日期范围、每日若干开始时间与持续分钟数、每次目标水量以及设备**每日容量**，服务生成逐日执行批次，并处理好三件麻烦事：

- **跨午夜**：批次用真实流逝时间的 UTC 区间表示，结束时间落在次日也能正确显示；
- **夏令时**：春季跳变时**不存在的本地时间**自动顺延（如纽约 `02:30` 缺失 → `03:30 EDT`，批次上标注 `dst_event: "gap"`）；秋季回拨时**重复的本地时间**取第一次出现（`"duplicate"`，并说明第二次出现的 UTC 偏移）；
- **停灌日**：请求可带 `suspended_dates` 本地日期列表（按 `timezone` 解释），列表中的日期**不生成当天排定的灌溉批次**，响应仍在 `day_results` 与顶层 `suspended_dates` 中保留这些日期并标明 `"suspended": true`；停灌只影响该日排定时段，**不取消前一日本地日期开始、跨入该日的跨午夜批次**；
- **合并与容量**：按 UTC 开始时间排序，相邻且**间隔 ≤ 1 分钟（含重叠）**的批次合并（合并可以跨越午夜）；每日容量按**合并前**当日计划总水量校验，超出的日期返回**明确缺口**（`shortfall_ml`），批次照常返回、**绝不截断水量**。

### 请求

```bash
curl -s http://localhost:8000/api/irrigation/plan \
  -H 'Content-Type: application/json' \
  -d '{
    "timezone": "Asia/Shanghai",
    "start_date": "2026-05-01",
    "end_date": "2026-05-03",
    "sessions": [
      {"start_time": "08:00",     "duration_minutes": 10, "water_ml": 500},
      {"start_time": "08:10:30",  "duration_minutes": 5,  "water_ml": 200},
      {"start_time": "23:55",     "duration_minutes": 10, "water_ml": 300}
    ],
    "daily_capacity_ml": 1100,
    "suspended_dates": ["2026-05-02"]
  }'
```

字段说明：

| 字段 | 说明 |
| ---- | ---- |
| `timezone` | IANA 时区名（如 `Asia/Shanghai`、`America/New_York`、`Europe/London`）；非法名称返回 422 |
| `start_date` / `end_date` | 本地日历日期，含首尾；范围最多 370 天，`end < start` 返回 422 |
| `sessions[].start_time` | 每日重复的本地开始时间 `HH:MM` 或 `HH:MM:SS`（24 小时制）；同一日时间不可重复 |
| `sessions[].duration_minutes` | 持续分钟数，必须为正；批次按真实流逝时间结束，可自然跨过午夜与 DST 跳变 |
| `sessions[].water_ml` | 本次目标水量 (mL)，必须为正 |
| `daily_capacity_ml` | 设备每日供水容量 (mL)，非负（`0` 表示当日不可供水，必然产生缺口） |
| `suspended_dates` | 可选，**本地停灌日期**列表（`YYYY-MM-DD`，按 `timezone` 解释）。列表中的日期不排定当日批次、当日计划水量为 0；响应原样回显（升序）并在 `day_results[].suspended` 标 `true`。日期必须落在 `start_date`~`end_date` 内且不可重复，否则 422。前一日开始、跨入停灌日的跨午夜批次**照常执行**，停灌日不产生容量缺口 |

### 响应（节选）

`08:00` 的批次在 `08:10` 结束，与 `08:10:30` 开始的批次只隔 30 秒（≤ 1 分钟），被合并成一个 700 mL、`08:00–08:15:30` 的批次；`23:55` 的批次跨到次日 `00:05`。下例把 `2026-05-02` 列为停灌日：当天不排定任何批次（3 个原始时段全部不生成），但 **`05-01` 晚 `23:55` 开始、跨入 `05-02 00:05` 的批次照常执行**：

```json
{
  "timezone": "Asia/Shanghai",
  "start_date": "2026-05-01",
  "end_date": "2026-05-03",
  "days": 3,
  "suspended_dates": ["2026-05-02"],
  "batch_count_before_merge": 6,
  "batch_count_after_merge": 4,
  "merge_gap_seconds": 60,
  "planned_water_ml": 2000.0,
  "daily_capacity_ml": 1100,
  "capacity_ok": true,
  "capacity_violations": [],
  "day_results": [
    {
      "date": "2026-05-01",
      "suspended": false,
      "batch_count_before_merge": 3,
      "batch_count_after_merge": 2,
      "planned_water_ml": 1000.0,
      "daily_capacity_ml": 1100,
      "within_capacity": true,
      "shortfall_ml": 0.0,
      "dst_events": []
    },
    {
      "date": "2026-05-02",
      "suspended": true,
      "batch_count_before_merge": 0,
      "batch_count_after_merge": 0,
      "planned_water_ml": 0.0,
      "daily_capacity_ml": 1100,
      "within_capacity": true,
      "shortfall_ml": 0.0,
      "dst_events": []
    }
  ],
  "batches": [
    {
      "start_utc": "2026-05-01T00:00:00Z",
      "end_utc": "2026-05-01T00:15:30Z",
      "local_start": "2026-05-01T08:00:00+0800",
      "local_end": "2026-05-01T08:15:30+0800",
      "duration_seconds": 930,
      "water_ml": 700.0,
      "merged": true,
      "batch_count_before_merge": 2,
      "dst_event": null,
      "crosses_midnight": false,
      "sources": [
        {"day": "2026-05-01", "requested_time": "08:00:00",
         "local_start": "2026-05-01T08:00:00+0800", "water_ml": 500.0},
        {"day": "2026-05-01", "requested_time": "08:10:30",
         "local_start": "2026-05-01T08:10:30+0800", "water_ml": 200.0}
      ]
    },
    {
      "start_utc": "2026-05-01T15:55:00Z",
      "end_utc": "2026-05-01T16:05:00Z",
      "local_start": "2026-05-01T23:55:00+0800",
      "local_end": "2026-05-02T00:05:00+0800",
      "duration_seconds": 600,
      "water_ml": 300.0,
      "merged": false,
      "batch_count_before_merge": 1,
      "crosses_midnight": true,
      "sources": [/* 05-01 排定、跨入停灌日 05-02：不取消，只在 05-01 计一次 */]
    }
  ]
}
```

停灌语义要点：

- **只抑制当日排定的时段**：`suspended_dates` 中的本地日期不生成任何以该日为计划日（`sources[].day`）的批次，`day_results` 中该日 `suspended=true`、批次计数与 `planned_water_ml` 均为 0；顶层 `suspended_dates` 原样回显（自动升序）。
- **跨午夜批次不受影响**：前一日本地日期开始的批次按真实流逝时间执行，结束时间落在停灌日也照常返回（上例 `2026-05-01T15:55Z → 16:05Z`），水量计入开始日；反向亦然——若停灌的是开始日本身，则该日 23:55 的批次也不会生成。
- **不计容量缺口**：停灌日计划水量为 0，恒为 `within_capacity=true`，即使 `daily_capacity_ml=0` 也不会出现在 `capacity_violations` 中。
- 日期按请求 `timezone` 的本地日历解释；超出 `start_date~end_date`、重复或格式非法均返回 422；不带该字段（或空列表）时行为与原来完全一致。


### 夏令时与容量缺口

纽约 2024-03-10 春季跳变，请求 `02:30` 浇水 30 分钟：该本地时间不存在，批次顺延到 `03:30 EDT`（UTC `07:30Z`）并在批次与 `day_results[].dst_events` 上标注：

```json
{
  "start_utc": "2024-03-10T07:30:00Z",
  "end_utc": "2024-03-10T08:00:00Z",
  "local_start": "2024-03-10T03:30:00-0400",
  "dst_event": "gap",
  "dst_note": "请求的本地时间 2024-03-10T02:30:00 不存在（夏令时跳变，缺失 60 分钟），已顺延到 2024-03-10T03:30:00-04:00 执行。"
}
```

当某日计划总量超过 `daily_capacity_ml` 时，接口仍返回 **200**（这是计划结果而非请求错误），并在顶层与每日结果中给出缺口，批次水量保持完整：

```json
{
  "capacity_ok": false,
  "capacity_violations": [
    {
      "date": "2026-05-01",
      "planned_water_ml": 1000.0,
      "daily_capacity_ml": 900,
      "shortfall_ml": 100.0,
      "message": "2026-05-01 计划用水 1000 mL，超出设备日容量 900 mL，缺口 100 mL；批次未截断，请扩容或减少当日批次。"
    }
  ]
}
```

以下情况一律返回 **422**：非法时区（`Mars/Olympus`、`+08:00`、空串）、日期顺序颠倒或超过 370 天、时间格式非法（`25:00`）、`sessions` 为空或开始时间重复、持续分钟数/水量非正、容量为负、`suspended_dates` 日期重复/超出范围/格式非法、多余字段。

## 库存消耗预测（POST /api/recipes/{id}/versions/{v}/consumption-forecast）

给定一个已保存的**配方版本**、一套灌溉计划参数（时区 / 日期范围 / 每日时段与水量）以及各浓缩液**当前库存**，服务读取该版本的求解结果（`allocations` / `total_volume_ml`），按

> 每升灌溉水需浓缩液 i 的 mL 数 = 该版本中浓缩液 i 的分配体积 ÷ 配方目标总体积

对**每个实际灌溉批次（合并后）**的水量等比例扣减库存，并按本地日汇总：

- **跨午夜 / 合并批次只扣一次**：批次按 UTC 开始时间排序，相邻 ≤ 1 分钟（含重叠）的批次已合并；每个合并后批次整体计入其**本地开始日期**，绝不会因为跨到次日而重复扣减；
- **停灌日不扣减**：请求可带 `suspended_dates` 本地日期列表（按 `timezone` 解释），列表中的日期不排定批次、不消耗库存；响应顶层回显 `suspended_dates`，`day_results[]` 中对应日期标 `"suspended": true`、水量/需求均为 0。停灌只影响当日排定时段，**前一日本地日期开始、跨入停灌日的批次仍照常扣减一次**（归属前一日）；
- **不足时保留完整预测**：任一浓缩液不够本批使用时，批次水量与各液需求量保持完整，实际扣减到 0 为止、缺口标记在该批次（`shortfall_ml`）和当日汇总上，**绝不出现负库存**，后续批次仍按原计划照常预测；
- 输出每液全程汇总（`mix_ml_per_l`、总需求/总扣减/总缺口、剩余、`first_shortfall_date`）、逐日消耗与日末剩余，以及顶层 `first_shortfall_date`（首个出现任一液不足的本地日期；全程充足为 `null`）。

### 请求

```bash
curl -s -X POST \
  http://localhost:8000/api/recipes/1/versions/1/consumption-forecast \
  -H 'Content-Type: application/json' \
  -d '{
    "timezone": "Asia/Shanghai",
    "start_date": "2026-05-01",
    "end_date": "2026-05-03",
    "sessions": [
      {"start_time": "08:00",     "duration_minutes": 10, "water_ml": 500},
      {"start_time": "08:10:30",  "duration_minutes": 5,  "water_ml": 200},
      {"start_time": "23:55",     "duration_minutes": 10, "water_ml": 300}
    ],
    "stock": [
      {"concentrate": "A液", "stock_ml": 100},
      {"concentrate": "B液", "stock_ml": 10}
    ]
  }'
```

计划参数（`timezone` / `start_date` / `end_date` / `sessions` / `suspended_dates`）的含义与校验规则与 [灌溉计划](#灌溉计划post-apirrigationplan) 完全一致（同日重复开始时间、非法时区/日期、空时段、非正时长/水量、停灌日期重复或超出范围等均返回 422）；本接口不接收 `daily_capacity_ml`。`stock` 为各浓缩液当前库存（mL），名称必须与该版本求解结果中的 `allocations[].concentrate` **一一对应**：多给未知液体、缺少某液、名称重复、库存为负均返回 422。

`suspended_dates` 为可选的本地停灌日期列表。例如，把中间一天停灌，则该日不排定批次、不扣减库存，顶层与逐日结果都会标明停灌；前一日晚开始的跨午夜批次照常扣减：

```bash
curl -s -X POST \
  http://localhost:8000/api/recipes/1/versions/1/consumption-forecast \
  -H 'Content-Type: application/json' \
  -d '{
    "timezone": "Asia/Shanghai",
    "start_date": "2026-05-01",
    "end_date": "2026-05-03",
    "sessions": [
      {"start_time": "08:00",     "duration_minutes": 10, "water_ml": 500},
      {"start_time": "08:10:30",  "duration_minutes": 5,  "water_ml": 200},
      {"start_time": "23:55",     "duration_minutes": 10, "water_ml": 300}
    ],
    "suspended_dates": ["2026-05-02"],
    "stock": [
      {"concentrate": "A液", "stock_ml": 100000},
      {"concentrate": "B液", "stock_ml": 100000}
    ]
  }'
```

该请求只在 `05-01`、`05-03` 灌溉（合计 2000 mL，共 6 个原始批次→4 个合并批次）：响应顶层回显 `"suspended_dates": ["2026-05-02"]`，`day_results` 中 `2026-05-02` 为 `{"date": "2026-05-02", "suspended": true, "batches": 0, "water_ml": 0.0, ...}`，各液当日需求/扣减均为 0、日末剩余与前一日相同；`05-01 23:55` 跨入 `05-02 00:05` 的批次仍在 `batches[]` 中，`attributed_date` 为 `2026-05-01`、只扣一次。

### 响应（节选与说明）

上例配方每 10 L 用 A 液 1500 mL、B 液 125 mL，即每升水 A=0.15 mL、B=0.0125 mL。3 天共 3000 mL 水（每日合并后 2 个批次：08:00/08:10:30 合并成 700 mL，23:55 的 300 mL 跨午夜），共需 A 450 mL、B 37.5 mL；库存仅 100/10 mL，故首日即出现缺口：

```json
{
  "recipe_id": 1,
  "version": 1,
  "recipe_feasible": true,
  "warning": null,
  "timezone": "Asia/Shanghai",
  "start_date": "2026-05-01",
  "end_date": "2026-05-03",
  "days": 3,
  "suspended_dates": [],
  "batch_count_before_merge": 9,
  "batch_count_after_merge": 6,
  "planned_water_ml": 3000.0,
  "sufficient": false,
  "first_shortfall_date": "2026-05-01",
  "liquids": [
    {
      "concentrate": "A液",
      "initial_stock_ml": 100.0,
      "mix_ml_per_l": 150.0,
      "total_required_ml": 450.0,
      "total_consumed_ml": 100.0,
      "total_shortfall_ml": 350.0,
      "remaining_ml": 0.0,
      "first_shortfall_date": "2026-05-01"
    },
    {
      "concentrate": "B液",
      "initial_stock_ml": 10.0,
      "mix_ml_per_l": 12.5,
      "total_required_ml": 37.5,
      "total_consumed_ml": 10.0,
      "total_shortfall_ml": 27.5,
      "remaining_ml": 0.0,
      "first_shortfall_date": "2026-05-01"
    }
  ],
  "day_results": [
    {
      "date": "2026-05-01",
      "suspended": false,
      "batches": 2,
      "water_ml": 1000.0,
      "sufficient": false,
      "liquids": [
        {"concentrate": "A液", "required_ml": 150.0, "consumed_ml": 100.0,
         "shortfall_ml": 50.0, "remaining_ml": 0.0},
        {"concentrate": "B液", "required_ml": 12.5, "consumed_ml": 10.0,
         "shortfall_ml": 2.5, "remaining_ml": 0.0}
      ]
    }
  ],
  "batches": [
    {
      "index": 1,
      "start_utc": "2026-05-01T00:00:00Z",
      "local_start": "2026-05-01T08:00:00+0800",
      "local_end": "2026-05-01T08:15:30+0800",
      "attributed_date": "2026-05-01",
      "water_ml": 700.0,
      "merged": true,
      "batch_count_before_merge": 2,
      "crosses_midnight": false,
      "sufficient": false,
      "liquids": [
        {"concentrate": "A液", "required_ml": 105.0,
         "consumed_ml": 100.0, "shortfall_ml": 5.0},
        {"concentrate": "B液", "required_ml": 8.75,
         "consumed_ml": 8.75, "shortfall_ml": 0.0}
      ]
    },
    {
      "index": 2,
      "start_utc": "2026-05-01T15:55:00Z",
      "local_start": "2026-05-01T23:55:00+0800",
      "local_end": "2026-05-02T00:05:00+0800",
      "attributed_date": "2026-05-01",
      "water_ml": 300.0,
      "merged": false,
      "batch_count_before_merge": 1,
      "crosses_midnight": true,
      "sufficient": false,
      "liquids": [/* 整批 300 mL 只在 05-01 扣一次，不在 05-02 重复计 */]
    }
  ]
}
```

要点：

- `batches[]` 按执行顺序给出每个实际批次；合并批次的需求按**合并后总水量**折算（上例 700 mL → A 105 mL），两个源时段不会各扣一次；跨午夜批次 `attributed_date` 取本地开始日。
- 停灌日不出现在 `batches[]` 的 `attributed_date` 中；`day_results[]` 中对应行 `suspended=true`、`batches=0`，但前一日开始跨入停灌日的批次仍归属前一日并正常扣减。
- 库存耗尽后的批次仍保留完整 `required_ml`，`consumed_ml=0`、`shortfall_ml=required_ml`，所有 `remaining_ml` 恒 ≥ 0。
- `day_results[]` 覆盖日期范围内每一天（即使某天因计划形态没有归属批次也会出现，水量为 0），`remaining_ml` 为该日结束时的累计剩余。
- 库存恰好能撑到计划结束（最后一批扣完正好为 0）视为充足，`sufficient=true`。
- 通过 `?force=true` 保存的不可行版本也可预测：返回 `recipe_feasible=false` 与 `warning`，按其折中方案 `allocations` 折算，结果仅供参考。

错误码：

- **404**：配方不存在、或该配方下版本不存在（如 `/api/recipes/1/versions/99/...`）；
- **422**：库存名称未知（`未知浓缩液：C液`）、缺少某液（`库存缺少…浓缩液`）、库存为负或名称重复；以及灌溉计划参数的全部校验失败（非法时区/日期、空或重复时段、非正时长/水量、`suspended_dates` 重复或超出日期范围、多余字段）。

## 项目结构

```
.
├── app/
│   ├── main.py       # FastAPI 路由（试算 / 保存 / 版本 / 导出 / 灌溉计划 / 消耗预测）
│   ├── solver.py     # minimax LP 建模与无可行解诊断
│   ├── irrigation.py # 灌溉批次生成：时区/DST 解析、跨午夜、合并、容量校验
│   ├── forecast.py   # 库存消耗预测：按实际批次等比例扣减、缺口标记、逐日汇总
│   ├── models.py     # Pydantic 请求/响应模型
│   ├── database.py   # SQLite 初始化与配方/版本存取
│   └── config.py     # 元素清单、默认容差、DB 路径
├── Dockerfile
├── docker-compose.yml
├── smoke_test.py     # TestClient 端到端冒烟测试
└── requirements.txt
```
