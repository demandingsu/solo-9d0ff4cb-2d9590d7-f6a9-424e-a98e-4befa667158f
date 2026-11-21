# 空气质量日报

FastAPI + SQLite + Docker Compose 实现的空气监测日报网页应用。监测员上传站点经纬度、
小时污染物 CSV 与分段阈值表，系统按站点和 UTC 日计算日报；任一项缺测即拒绝发布，
并始终保留上一份完整日报。

## 功能

- **站点管理**：编码、名称、经纬度（支持南纬/西经），编码与 CSV 内站点编码交叉校验。
- **CSV 导入**：宽表 `time, PM2.5, PM10, O₃, SO₂, NO₂, CO` 或长表 `time, pollutant, value`；
  兼容 UTF-8 / GB18030、逗号/分号/制表符、中英文列名与带单位的列名（如 `PM2.5(µg/m³)`）。
  空值、`NA`、`缺测` 视为缺测不落库；重复导入覆盖旧值并写入更正记录。
- **阈值表**：CSV 含浓度断点与指数断点，严格校验连续性、单调性、单位与六项齐全；
  每次上传独立保存快照与内容哈希。
- **发布计算**：
  - PM2.5 / PM10：UTC 日 24 小时算术均值，至少 18 个有效小时；
  - O₃：日内连续 8 小时滑动均值的最大值，至少 1 个完整窗口；
  - SO₂ / NO₂ / CO：小时分指数的日最大值，至少 18 个有效小时；
  - 六项全部满足才发布；取最大 IAQI 为首要污染物（并列时全部列出；AQI ≤ 50 时页面注明按标准可称“无首要污染物”）。
- **版本与失败保留**：每次发布冻结读数与阈值快照；补传/更正后重新发布，版本号 +1 并
  指向被替代版本；发布失败写入失败记录，当前版本与历史版本均不受影响。
- **站号互换**：网关贴反两站站号时，登记两站与误标时段（UTC 整点，含起点、不含终点）。
  时段内已有读数立即换到对站并保留各自导入批次号（只有一边有值也照移）；此后网页与 API
  仍按原站号接收补传/重传，时段内的读数按观测时刻自动归到对站，时段外照常入库。
  同一对站与同一时段重复登记只生效一次；任一站时段与其他规则重叠则拒绝登记；
  登记与存量搬移同事务完成，失败时原规则与读数归属不变。
- **网页**：概览、上传、阈值、站号互换、站点、日报详情、查询、演示；另有 JSON API 与 OpenAPI 文档。

## 快速开始（Docker Compose）

```bash
docker compose up --build
```

打开 <http://localhost:8000>。数据持久化在命名卷 `air-quality-data` 的 `/data/air_quality.db`。

```bash
curl -s localhost:8000/healthz
```

停止但保留数据：`docker compose down`；连同数据一起清除：`docker compose down -v`。

## 本地运行（不使用 Docker）

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
AIR_DATABASE_PATH=./data/air_quality.db uvicorn app.main:app --reload
```

首次启动自动建表并写入内置默认阈值表（GB 3095 / HJ 633 近似值）。

## 操作流程

1. **登记站点**：上传页填写编码与经纬度，或 `POST /api/stations`。
2. **上传阈值**（可选）：`POST /api/thresholds` 上传 CSV；不传则使用内置默认阈值。
3. **导入观测**：选择站点上传 CSV，或用 `POST /api/stations/{id}/observations`。
4. **站号互换**（可选）：网关贴反站号时，在“站号互换”页登记两站与误标时段；详见下文。
5. **发布日报**：站点页选择 UTC 日期后点击“计算并发布”，或 `POST /api/reports`。
6. **查询**：`/reports` 按站点编码与日期筛选；`/reports/{id}/readings.json` 取冻结读数与阈值快照。

### 示例文件

`samples/` 下有可直接使用的数据：

| 文件 | 用途 |
| --- | --- |
| `thresholds.csv` | 与内置默认阈值一致的分段阈值表 |
| `observations_complete.csv` | 2026-11-18 ~ 20 三天完整数据 |
| `observations_missing.csv` | 2026-11-19 缺测 00–06 与 20–23 时，用于验证拒绝发布 |
| `observations_correction.csv` | 补齐上者缺测小时并更正一个 O₃ 值，用于验证补传后发布 |
| `swap_station_a.csv` | 站号互换演示：A 站 2026-11-19 全天 24 小时 |
| `swap_station_b_partial.csv` | 站号互换演示：B 站 2026-11-19 仅 00–11 时（单边缺值） |
| `swap_backfill_b.csv` | 站号互换演示：按原站号 B 补传 2026-11-19 12–23 时 |

缺测与补传的最小演示（假设站点 id 为 1）：

```bash
B=http://localhost:8000
curl -s -X POST $B/api/stations/1/observations -F 'file=@samples/observations_missing.csv'
curl -s -X POST $B/api/reports -H 'Content-Type: application/json' \
  -d '{"station_id":1,"report_date":"2026-11-19"}'   # → 422，列出缺测项

curl -s -X POST $B/api/stations/1/observations -F 'file=@samples/observations_correction.csv'
curl -s -X POST $B/api/reports -H 'Content-Type: application/json' \
  -d '{"station_id":1,"report_date":"2026-11-19"}'   # → 200，v1
```

浏览器中也可点击首页“一键生成演示数据”，会创建两个演示站点并导入最近三天的观测。

## 站号互换纠正

网关把两座测站的站号贴反时，在“站号互换”页（或 `POST /api/swaps`）登记两站与误标时段：

- 时段为 UTC 整点半开区间 `[start_at, end_at)`：含起点、不含终点，起点必须早于终点。
- 登记即生效：时段内已有读数换到对站，保留各自的导入批次号；只有一边有值也照移。
  规则写入与读数搬移在同一事务内完成，任一步失败整体回滚，原规则与读数归属不变。
- 重复登记同一对站与同一时段只生效一次（站序颠倒也算同一对），不会把读数换回去。
- 任一站的时段与其他规则重叠时拒绝登记（首尾相接不算重叠）。
- 补传/重传仍用原站号上传：导入事务内一次性读取已提交规则，整批按同一套规则归站——
  时段内的读数归到对站，时段外照常入库；导入记录会标注“N 条按互换规则归入对站”。
- 站点页的小时覆盖与新发布的日报使用纠正后的归属；已发布日报仍保留原快照可查。

最小演示（先创建 A、B 两站，假设 id 为 1、2）：

```bash
B=http://localhost:8000
# 1) 误标期间的数据按贴反的站号入库
curl -s -X POST $B/api/stations/1/observations -F 'file=@samples/swap_station_a.csv'
curl -s -X POST $B/api/stations/2/observations -F 'file=@samples/swap_station_b_partial.csv'

# 2) 登记互换：2026-11-19 全天（含 00:00，不含次日 00:00）
curl -s -X POST $B/api/swaps -H 'Content-Type: application/json' -d '{
  "station_a_id": 1, "station_b_id": 2,
  "start_at": "2026-11-19T00:00:00Z", "end_at": "2026-11-20T00:00:00Z",
  "note": "网关贴反站号"}'
# → 站 1 的 144 条换到站 2，站 2 的 72 条换到站 1

# 3) 仍按原站号补传 B 缺失的 12–23 时 → 读数自动归到站 1
curl -s -X POST $B/api/stations/2/observations -F 'file=@samples/swap_backfill_b.csv'
# → counts.remapped = 72

# 4) 两站分别发布 2026-11-19 日报，均为纠正后的归属
curl -s -X POST $B/api/reports -H 'Content-Type: application/json' \
  -d '{"station_id":1,"report_date":"2026-11-19"}'
curl -s -X POST $B/api/reports -H 'Content-Type: application/json' \
  -d '{"station_id":2,"report_date":"2026-11-19"}'
```

浏览器中对应路径：上传页选站导入 → “站号互换”页登记 → 站点页查看小时覆盖变化 → 发布日报。

## CSV 约定

观测文件（宽表）：

```csv
time,PM2.5,PM10,O3,SO2,NO2,CO
2026-11-20T00:00:00Z,38,72,42,12,38,1.1
2026-11-20T01:00:00Z,41,78,45,11,41,1.2
2026-11-20T02:00:00Z,NA,80,48,10,39,1.0
```

- 时间必须对齐整点；带 `Z` 或 `±hh:mm` 偏移均会换算到 UTC；无时区按 UTC 解释并给出提示。
- 单位：PM2.5、PM10、O₃、SO₂、NO₂ 为 µg/m³；CO 为 mg/m³。
- 文件上限 8 MB、20 万数据行。

阈值文件：

```csv
pollutant,period,concentration_low,concentration_high,index_low,index_high,unit,note
PM2.5,24h,0,35,0,50,µg/m³,一级
PM2.5,24h,35,75,50,100,µg/m³,二级
```

规则：每项首个分段浓度与指数下限为 0；相邻段断点首尾相接；指数上限 ≤ 500；
PM2.5 / PM10 时段为 `24h`，O₃ 为 `8h`，SO₂ / NO₂ / CO 为 `1h`。超过最高浓度断点的读数按
最高段指数封顶并在页面标注。

## API 摘要

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/stations` | 站点列表与观测统计 |
| POST | `/api/stations` | `{code,name,latitude,longitude}` |
| POST | `/api/stations/{id}/observations` | multipart `file`，观测 CSV |
| GET/POST | `/api/thresholds` | 列表 / 上传阈值 CSV |
| GET/POST | `/api/swaps` | 互换规则列表 / 登记 `{station_a_id,station_b_id,start_at,end_at,note?}`；重叠返回 409 |
| POST | `/api/reports` | `{station_id,report_date,threshold_table_id?,note?}`；缺测返回 422 |
| GET | `/api/reports` | `station`、`report_date`、`limit` 过滤 |
| GET | `/api/reports/{id}` | 完整快照 |
| GET | `/api/reports/{id}/versions` | 同站同日全部版本 |
| POST | `/api/demo` | 生成演示数据 |
| GET | `/healthz` | 健康检查 |

交互式文档：<http://localhost:8000/docs>。

## 测试

```bash
pip install -r requirements-dev.txt
python -m pytest
```

覆盖：IAQI 插值与分段连续性校验、24 小时均值 18 小时边界、O₃ 8 小时窗口（含跨日不可用、
缺口打断窗口、窗口贴近日界）、其余污染物小时分指数峰值、首要污染物并列、缺测拒绝、
版本递增与旧快照冻结、失败保留上一份完整日报、导入幂等/更正/编码/长表、API 与页面渲染。

## 目录

```
app/
  main.py        FastAPI 应用与模板环境
  routes.py      网页与 JSON API 路由
  services.py    导入、发布、演示数据等业务事务
  calc.py        日报计算引擎与 AQI 分级
  thresholds.py  阈值 CSV 校验与 IAQI
  importer.py    观测 CSV 解析
  db.py          SQLite 模式与查询
  config.py      污染物定义与内置默认阈值
  templates/     Jinja2 页面
  static/        样式与脚本
samples/         示例 CSV
tests/           pytest 用例
```

## 已知限制

- 无用户认证与权限模型，假定部署在受控内网；公网使用需自行加反向代理鉴权。
- 单进程 SQLite（WAL）适合单机；多副本横向扩展需换成 PostgreSQL 等共享数据库。
- 分子分母均为原始读数，未实现异常值剔除或仪器有效性标记（仅支持缺测标记）。
- 时间断点按整点识别，不接受半小时数据或跨 UTC 日的 O₃ 窗口。
- 互换规则一经登记不可撤销/编辑（只能新增不重叠的规则）；如需纠正请先确认时段再登记。
- `observation_revisions` 更正审计记录保持写入时的原始站号，不随互换搬移。
