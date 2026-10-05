# 山地灾害协同

面向边境地区冰崩等突发灾害的协同处置后端。村镇、道路、供水设施分属不同部门，
灾情消息常在正式核实前经多渠道转发；本系统帮助值班人员回答三个问题：

1. **现在判断如何？** —— 相互矛盾的说法全部保留，按来源可信度给出当前判断；
2. **先处置哪里？** —— 按受影响人口、设施关键度、信息可信度、报告时效生成
   可解释的处置队列，每一分都能看出处；
3. **当时依据是什么？** —— 接报、复核、封路、派遣、解除全部进入只增不改的
   哈希链事件存储，任何时刻都能按入库时间回放历史视图。

## 设计要点

- **事件溯源**：所有事实只追加、不修改。撤销与更正只是新事件，已发生的
  派遣与封路决策连同其依据快照永远留在链上，可用 `/events/verify` 校验完整性。
- **双时间**：每条说法同时携带 `observed_at`（来源观测时刻）与
  `recorded_at`（系统入库时刻）。迟到消息按原观测时刻入库，不改写历史；
  `GET /state?as_of=<时间>` 回放任一历史时刻指挥端所见的判断。
- **幂等**：报文按 `(来源机构, 外部编号)` 去重；派遣按确定性需求编号
  `need_id = f(事件, 位置, 需求类型)` 去重。重复报文既产生不了新说法，
  也触发不了第二次派遣。
- **可信度**：来源基础可靠度（可经接口调整，调整本身入链）+ 独立机构
  佐证加成；说法存在争议时当前判断标记 `contested`，队列项标记
  `needs_verification` 并减半置信因子。
- **纯标准库**：不依赖任何第三方包，不访问外部网络，离线可运行、可验证。

## 运行

```bash
# 启动服务（事件落盘为本地 JSONL；缺省仅内存）
PYTHONPATH=src python -m mountain_response.service --host 127.0.0.1 --port 8080 --data data/events.jsonl

# 端到端自动化验证（冰崩全流程：接报→复核→派遣→更正→迟到→撤销→解除）
PYTHONPATH=src python -m mountain_response.verify

# 单元测试
python -m unittest discover -s tests -v

# 编译检查
python -m compileall -q src tests
```

## 接口一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/reports` | 接收影响报告（来源、观测时间、说法列表），幂等去重 |
| GET | `/reports/{id}` | 报告原文及其全部说法 |
| GET | `/locations/{code}/claims` | 该位置全部说法（含被取代、被撤销的原始记录） |
| GET | `/locations/{code}/judgment` | 该位置当前判断（含取舍理由与矛盾各方） |
| POST | `/claims/{id}/correct` | 更正说法：新说法取代旧说法，旧说法保留 |
| POST | `/claims/{id}/revoke` | 撤销说法：当前判断不再采用，记录留痕 |
| PUT | `/sources/{agency}` | 设定来源基础可靠度（0~1） |
| GET | `/queue` | 可解释处置队列（因子分解、争议标记、派遣状态） |
| POST | `/dispatches` | 按 `need_id` 调派力量，重复请求自动去重 |
| POST | `/roads/{code}/close` `/temp-open` `/reopen` | 道路封闭 / 临时开放 / 恢复，事件携带依据快照 |
| POST | `/incidents/{event_key}/resolve` | 解除事件，队列清空 |
| GET | `/state` `?as_of=` | 当前状态或任一历史时刻状态 |
| GET | `/events` `?type=&since=` | 事件链查询 |
| GET | `/events/verify` | 哈希链完整性校验 |

### 接报示例

```json
POST /reports
{
  "event_key": "icefall-2026-10-05",
  "kind": "road",
  "location_code": "R-4",
  "source": {"agency": "边境巡查组", "external_id": "xj-001",
             "observed_at": "2026-10-05T04:00:00+08:00"},
  "summary": "冰崩碎屑掩埋 R-4 K12 段，双向中断",
  "affected_people": 0,
  "claims": [{"aspect": "road_status", "value": "blocked"}]
}
```

同一 `(agency, external_id)` 再次报送时返回 `{"deduplicated": true, ...}`，
不产生新说法、不改变队列、不触发派遣。

### 队列项示例（节选）

```json
{
  "need_id": "need-…",
  "location_code": "W-2",
  "need_kind": "water_restore",
  "score": 93.93,
  "needs_verification": false,
  "explanation": {
    "formula": "score = 100 * Σ(权重 × 归一化因子)",
    "factors": [
      {"name": "affected_people", "raw": 500, "weight": 0.45, "contribution": 45.0},
      {"name": "facility_criticality", "raw": 0.9, "weight": 0.30, "contribution": 27.0},
      {"name": "information_confidence", "raw": 0.8, "weight": 0.15, "contribution": 12.0},
      {"name": "report_recency", "raw": 0.5, "weight": 0.10, "contribution": 9.93}
    ],
    "basis_claim_ids": ["clm-000004-0", "clm-000004-1"]
  }
}
```

## 目录结构

```
src/mountain_response/
  contracts.py    数据契约（报告、来源、说法方面、事件类型）
  clock.py        可注入时钟（验证用手动时钟，保证离线可复现）
  store.py        只增不改的哈希链事件存储（JSONL 落盘）
  state.py        物化视图与当前判断（矛盾保留、取舍规则）
  credibility.py  来源可信度（基础可靠度 + 佐证加成）
  queueing.py     可解释处置队列
  core.py         协同核心（接报/复核/派遣/道路/解除/回放）
  service.py      离线 HTTP 接口（标准库 http.server）
  verify.py       端到端自动化验证
tests/            单元测试（unittest，31 项）
```
