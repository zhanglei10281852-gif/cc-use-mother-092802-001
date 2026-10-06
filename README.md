# 边境冰崩 · 灾害协同后端

供值班人员在离线环境使用的灾情协同服务：接收**带来源与观测时间**的影响报告，
保留相互矛盾的原始说法并支持后续更正，按**受影响人口、关键设施、信息可信度**
生成可解释的处置队列；道路封闭/临时开放/恢复与救援力量调派形成**只追加、可追溯**
的事件链。

仅依赖 Python 3.10+ 标准库（`http.server` + `sqlite3`），可在内网/离线主机直接运行。

## 设计原则：当前判断 与 历史依据 严格分离

| 要求 | 实现方式 |
| --- | --- |
| 矛盾说法都保留 | 每条报告是只追加的原始行；当前判断由"可信度投影"实时生成，矛盾在 `contradictions` 中显式呈现 |
| 更正 / 撤销 | `corrects` / `retracts` 只把旧行标记为 `superseded` / `retracted`，**永不删除** |
| 重复报文不重复派遣 | `identity_key`（来源+外部编号 / 显式 `dedupe_key` / 原文哈希）唯一约束；重复只进 `raw_messages`，不产生新报告，派遣另有同目标同任务去重 |
| 撤销/迟到消息不抹掉决策 | `decisions` 表只追加；新决策令旧决策 `active=0` 并记录 `deactivated_by`，`basis_json` 固化决策时的态势快照 |
| 解除后新消息 | 事件自动重开并追加 `event.reopen`，此前 `event.resolve` 及其快照原样保留 |
| 临时开放过期 | 运营态在查询时按 `end_time` 自动回到封闭，不回写历史决策 |

## 可信度与评分（每一分都可解释）

- 来源分级：`official` 1.0 / `responder` 0.85 / `witness` 0.6 / `unknown` 0.4，
  再乘观测时效系数（1h 内 1.0 → 24h 以上 0.4）。
- 同一主题（某条道路 / 某座设施 / 某地人员）出现矛盾说法时，**当前判断采信可信度
  最高的说法**（并列时观测时间更新者胜）；另一方说法仍在 `evidence` 中并写入
  `contradictions`。
- 评分组件：受影响人口、关键/一般供水设施中断、道路阻断、当前采信说法的严重度、
  处置缺口；`score = Σ组件分 × 综合可信度`，映射 P1/P2/P3/P4。
- 护栏：单条未证实说法可信度不足 0.7 时 P1 暂降 P2 待核实；大规模且高可信灾情有
  级别保底。队列每项都返回 `components[].why` 与 `confidence_factors`。

## 运行

```bash
# 启动离线 HTTP 服务（默认 0.0.0.0:8080，SQLite 落盘 data/disaster.db）
PYTHONPATH=src python3 -m mountain_response --port 8080 --db data/disaster.db

# 端到端情景演示（接报→矛盾复核→临时开放→解除→再重开）
python3 scripts/demo.py

# 自动化验证（28 项：契约/接报/评分/处置/全周期/HTTP/持久化/并发）
python3 -m unittest discover -s tests -v
python3 -m compileall -q src tests scripts
```

## HTTP 接口（均为 JSON）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/v1/reports` | 接报，自动去重/更正/撤销/解除后重开 |
| GET  | `/v1/events/{key}/assessment?as_of=...` | 当前判断 + 可解释处置队列（`as_of` 可做历史复盘） |
| POST | `/v1/events/{key}/roads` | `road.close` / `road.reopen` / `road.temporary_open`（需 `end_time`） |
| POST | `/v1/events/{key}/dispatches` | 调派；同目标同任务在途时返回 409 |
| POST | `/v1/dispatches/update` | `dispatch.arrive` / `redeploy` / `standdown` |
| POST | `/v1/events/{key}/resolve[?force=true]` | 解除（仍有 P1/P2 时拒绝，强制解除须给理由并留痕） |
| POST | `/v1/events/{key}/reopen` | 手动重开 |
| GET  | `/v1/events/{key}/chain` | 决策时间线（每步含 `basis` 依据快照）+ 调派记录 |
| GET  | `/v1/events/{key}/reports` | 全部原始说法（含 active/superseded/retracted） |
| GET  | `/v1/events/{key}/raw` | 每次到达的原文（含重复转发） |
| POST | `/v1/facilities` | 登记设施（`critical` 标识关键设施） |
| POST | `/v1/sources/trust` | 设定来源分级 |

处置类请求可传 `at`（ISO-8601）指定决策生效时刻，用于按时间线复盘/演练；
日常值班不传则使用服务器当前时间。

### 接报示例

```json
POST /v1/reports
{
  "event_key": "ICEFALL-1", "kind": "road", "location_code": "V-山口村",
  "agency": "县应急局", "external_id": "of-201",
  "observed_at": "2026-10-06T04:10:00Z",
  "summary": "现场核实四号路可单向缓行",
  "road_code": "R-4号路", "road_state": "open", "severity": "info",
  "corrects": null, "retracts": null, "dedupe_key": null
}
```

## 代码结构

```
src/mountain_response/
  contracts.py   数据契约（报告/状态/动作枚举）
  timeutil.py    UTC 时间处理
  storage.py     SQLite 只追加存储（原始报文、raw 留痕、不可变决策链）
  ingest.py      接报：校验、幂等去重、更正/撤销、解除后重开
  assessment.py  可信度投影、矛盾检测、可解释评分与处置队列
  operations.py  道路/调派/解除决策（含幂等、去重、依据快照）
  service.py     统一服务门面
  http_api.py    零依赖离线 HTTP 接口
scripts/demo.py  端到端情景演示
tests/           自动化验证（28 项）
```
