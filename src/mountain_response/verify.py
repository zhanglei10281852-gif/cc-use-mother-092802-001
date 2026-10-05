"""端到端自动化验证：冰崩协同处置全流程。

场景：2026-10-05 凌晨边境地区冰崩，村镇 V-9、道路 R-4、供水设施 W-2
分属不同部门。多渠道消息陆续到达，包含重复、矛盾、更正、迟到与撤销。

验证的不变量：
 1. 接报保留来源与观测时间，矛盾说法全部留存；
 2. 重复报文幂等去重，不产生新说法、不触发派遣；
 3. 处置队列可解释（因子分解齐全、得分等于因子之和）；
 4. 派遣按需求幂等，重复请求不重复派遣；
 5. 道路封闭/临时开放与调派进入同一条哈希链，可校验、带依据快照；
 6. 更正只改变当前判断，历史说法与历史状态可回放；
 7. 迟到消息入库但不改写已发生的决策；
 8. 撤销不清除历史：封路决策及其依据仍在链上；
 9. 指挥端可随时区分当前判断与历史依据（as_of 回放）；
10. 解除后队列清空，事件链完整。

运行：python -m mountain_response.verify
退出码：0 全部通过；1 存在失败项。
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mountain_response.clock import ManualClock
from mountain_response.contracts import ImpactReport, ReportKind, SourceReference
from mountain_response.core import CoordinationService
from mountain_response.store import EventStore

UTC = timezone.utc
EVENT_KEY = "icefall-2026-10-05"

CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    CHECKS.append((name, condition, detail))
    mark = "PASS" if condition else "FAIL"
    print(f"[{mark}] {name}" + (f" —— {detail}" if detail else ""))


def report(agency, external_id, observed_at, kind, location, summary, people=0):
    return ImpactReport(
        EVENT_KEY,
        kind,
        location,
        SourceReference(agency, external_id, observed_at),
        summary,
        people,
    )


def main() -> int:
    clock = ManualClock(datetime(2026, 10, 5, 4, 0, tzinfo=UTC))
    svc = CoordinationService(EventStore(), clock)

    # ---- 接报：多部门、多渠道 --------------------------------------------
    svc.set_source_reliability("边境巡查组", 0.9, "一线巡查，直接观测")
    svc.set_source_reliability("水利所", 0.8, "设施管理单位")
    svc.set_source_reliability("地质监测站", 0.6, "仪器遥测")
    svc.set_source_reliability("村民微信群", 0.2, "转发消息，未核实")

    clock.set(datetime(2026, 10, 5, 4, 20, tzinfo=UTC))
    r1 = svc.receive_report(
        report("边境巡查组", "xj-001", datetime(2026, 10, 5, 4, 0, tzinfo=UTC),
               ReportKind.ROAD, "R-4", "冰崩碎屑掩埋 R-4 K12 段，双向中断"),
        claims=[{"aspect": "road_status", "value": "blocked"}],
    )
    r2 = svc.receive_report(
        report("边境巡查组", "xj-002", datetime(2026, 10, 5, 4, 5, tzinfo=UTC),
               ReportKind.PEOPLE, "V-9", "V-9 村受困约 30 人", people=30),
        claims=[{"aspect": "people_trapped", "value": "30"}],
    )
    clock.set(datetime(2026, 10, 5, 4, 25, tzinfo=UTC))
    r3 = svc.receive_report(
        report("水利所", "sl-014", datetime(2026, 10, 5, 4, 10, tzinfo=UTC),
               ReportKind.WATER, "W-2", "W-2 加压站停机，供水中断，影响约 500 人", people=500),
        claims=[{"aspect": "water_supply", "value": "cut"}],
    )
    check("01 接报：三部门报告入库并生成说法",
          not r1["deduplicated"] and len(r1["claim_ids"]) == 1
          and len(r2["claim_ids"]) == 2 and len(r3["claim_ids"]) == 2)

    # ---- 重复报文 --------------------------------------------------------
    clock.set(datetime(2026, 10, 5, 4, 30, tzinfo=UTC))
    dup = svc.receive_report(
        report("边境巡查组", "xj-001", datetime(2026, 10, 5, 4, 0, tzinfo=UTC),
               ReportKind.ROAD, "R-4", "冰崩碎屑掩埋 R-4 K12 段，双向中断（重发）"),
        claims=[{"aspect": "road_status", "value": "blocked"}],
    )
    r4_claims = svc.claims_for_location("R-4")
    check("02 重复报文：幂等去重，不产生新说法",
          dup["deduplicated"] and dup["report_id"] == r1["report_id"] and len(r4_claims) == 1)

    # ---- 矛盾说法 --------------------------------------------------------
    clock.set(datetime(2026, 10, 5, 4, 35, tzinfo=UTC))
    r4 = svc.receive_report(
        report("村民微信群", "wx-777", datetime(2026, 10, 5, 4, 15, tzinfo=UTC),
               ReportKind.ROAD, "R-4", "群里说 R-4 还能走（未经核实）"),
        claims=[{"aspect": "road_status", "value": "passable"}],
    )
    judgment = svc.judgment_for_location("R-4")["judgments"]["road_status"]
    both_kept = {c["value"] for c in svc.claims_for_location("R-4")} == {"blocked", "passable"}
    check("03 矛盾说法：两条原始说法都保留，当前判断取高可信来源",
          both_kept and judgment["contested"] and judgment["value"] == "blocked",
          f"当前判断={judgment['value']}（可信度 {judgment['credibility']['score']}）")

    # ---- 处置队列 --------------------------------------------------------
    clock.set(datetime(2026, 10, 5, 4, 40, tzinfo=UTC))
    queue = svc.queue()
    by_need = {item["need_id"]: item for item in queue}
    w2 = next(i for i in queue if i["location_code"] == "W-2")
    r4_item = next(i for i in queue if i["location_code"] == "R-4")
    factors_ok = all(
        abs(sum(f["contribution"] for f in i["explanation"]["factors"]) - i["score"]) < 0.01
        for i in queue
    )
    check("04 处置队列：可解释（因子分解齐全，得分=因子之和）",
          len(queue) == 3 and factors_ok and queue[0]["need_id"] == w2["need_id"],
          f"队首=W-2 供水（{w2['score']} 分），R-4 因说法争议被标记复核={r4_item['needs_verification']}")

    # ---- 派遣与幂等 ------------------------------------------------------
    clock.set(datetime(2026, 10, 5, 4, 45, tzinfo=UTC))
    v9_need = next(i for i in queue if i["location_code"] == "V-9")
    d1 = svc.order_dispatch(v9_need["need_id"], ["救援队A", "卫星电话×2"], "先行搜救", "值班员-李")
    d2 = svc.order_dispatch(v9_need["need_id"], ["救援队B"], "重复请求", "值班员-王")
    dispatches = svc.events(event_type="dispatch_ordered")
    check("05 派遣幂等：同一需求重复请求不重复派遣",
          not d1["deduplicated"] and d2["deduplicated"] and len(dispatches) == 1,
          f"在办调派={d1['dispatch']['resources']}")

    # ---- 道路事件链 ------------------------------------------------------
    clock.set(datetime(2026, 10, 5, 4, 50, tzinfo=UTC))
    closed = svc.record_road_event("close", "R-4", "依据巡查组报告封闭 R-4", "值班员-李")
    road_event = svc.events(event_type="road_closed")[0]
    check("06 道路封闭：事件携带判断依据快照（含争议标记）",
          road_event["payload"]["basis"]["judgment_value"] == "blocked"
          and road_event["payload"]["basis"]["contested"] is True
          and len(road_event["payload"]["basis"]["claim_ids"]) == 2)

    # ---- 更正 ------------------------------------------------------------
    clock.set(datetime(2026, 10, 5, 5, 10, tzinfo=UTC))
    trapped_claim = next(
        c for c in svc.claims_for_location("V-9")
        if c["aspect"] == "people_trapped" and c["status"] == "active"
    )
    svc.correct_claim(trapped_claim["claim_id"], "12", "逐户核实后修正受困人数",
                      observed_at=datetime(2026, 10, 5, 5, 5, tzinfo=UTC))
    v9_now = svc.judgment_for_location("V-9")["judgments"]["people_trapped"]
    v9_all = svc.claims_for_location("V-9")
    check("07 更正：当前判断更新为 12 人，原始说法 30 人保留为 superseded",
          v9_now["value"] == "12"
          and any(c["value"] == "30" and c["status"] == "superseded" for c in v9_all))

    # ---- 迟到消息 --------------------------------------------------------
    clock.set(datetime(2026, 10, 5, 5, 20, tzinfo=UTC))
    late = svc.receive_report(
        report("地质监测站", "dz-090", datetime(2026, 10, 5, 3, 50, tzinfo=UTC),
               ReportKind.PEOPLE, "V-9", "（迟到）震前监测估计受困 45 人", people=45),
        claims=[{"aspect": "people_trapped", "value": "45"}],
    )
    v9_after_late = svc.judgment_for_location("V-9")["judgments"]["people_trapped"]
    history_at_445 = svc.state(as_of=datetime(2026, 10, 5, 4, 45, tzinfo=UTC))
    dispatch_count = len(svc.events(event_type="dispatch_ordered"))
    check("08 迟到消息：入库留痕但不改写当前判断与已发生的派遣",
          not late["deduplicated"] and v9_after_late["value"] == "12"
          and dispatch_count == 1
          and history_at_445["locations"]["V-9"]["judgments"]["people_trapped"]["value"] == "30",
          "as_of(04:45) 仍显示当时的 30 人判断")

    # ---- 撤销 ------------------------------------------------------------
    clock.set(datetime(2026, 10, 5, 5, 30, tzinfo=UTC))
    wx_claim = next(c for c in svc.claims_for_location("R-4") if c["agency"] == "村民微信群")
    svc.revoke_claim(wx_claim["claim_id"], "发布者撤回并致歉")
    r4_now = svc.judgment_for_location("R-4")["judgments"]["road_status"]
    road_event_after = svc.events(event_type="road_closed")[0]
    wx_kept = any(c["status"] == "revoked" for c in svc.claims_for_location("R-4"))
    check("09 撤销：争议消除，但封路决策及其依据仍在链上",
          r4_now["contested"] is False and r4_now["value"] == "blocked" and wx_kept
          and road_event_after["payload"]["basis"]["contested"] is True)

    # ---- 当前判断 vs 历史依据 --------------------------------------------
    clock.set(datetime(2026, 10, 5, 5, 40, tzinfo=UTC))
    svc.record_road_event("temp-open", "R-4", "为救援车队临时单向开放", "值班员-李")
    state_now = svc.state()
    state_then = svc.state(as_of=datetime(2026, 10, 5, 4, 45, tzinfo=UTC))
    check("10 当前与历史可区分：as_of 回放与当前状态各自独立",
          state_now["is_historical"] is False and state_then["is_historical"] is True
          and state_then["locations"]["R-4"]["judgments"]["road_status"]["contested"] is True
          and state_now["locations"]["R-4"]["judgments"]["road_status"]["contested"] is False
          and state_now["locations"]["R-4"]["road_status"]["status"] == "temporarily_open")

    # ---- 解除与链校验 ----------------------------------------------------
    clock.set(datetime(2026, 10, 5, 6, 0, tzinfo=UTC))
    svc.resolve_incident(EVENT_KEY, "受困人员转移完毕，供水恢复，R-4 抢通")
    final_queue = svc.queue()
    chain = svc.verify_chain()
    check("11 解除：队列清空，事件链完整可校验",
          final_queue == [] and chain["ok"], chain["detail"])

    audit = svc.state()["audit"]
    check("12 审计计数：重复报文与重复派遣请求均被记录且仅被记录",
          audit["duplicate_reports_ignored"] == 1 and audit["dispatch_requests_deduplicated"] == 1)

    passed = sum(1 for _, ok, _ in CHECKS if ok)
    total = len(CHECKS)
    print(f"\n结果: {passed}/{total} 通过")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
