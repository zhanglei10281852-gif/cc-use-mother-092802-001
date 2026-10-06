#!/usr/bin/env python3
"""离线演示：同一冰崩事件从接报、复核到解除、再重开的完整协同过程。

运行：python3 scripts/demo.py
仅使用标准库；默认内存库，加 --db 文件路径可落盘。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mountain_response.service import Service
from mountain_response.storage import Conflict, Store

T0 = datetime(2026, 10, 6, 3, 30, tzinfo=timezone.utc)
EV = "ICEFALL-BORDER-01"


def t(minutes: int) -> str:
    return (T0 + timedelta(minutes=minutes)).isoformat(timespec="seconds")


def show(title: str, obj) -> None:
    print(f"\n===== {title} =====")
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=":memory:")
    args = parser.parse_args()

    svc = Service(Store(args.db))

    # 来源分级与关键设施登记
    svc.set_source_trust("县应急局", "official")
    svc.set_source_trust("边境派出所", "responder")
    svc.set_source_trust("水利站", "responder")
    svc.set_source_trust("村民转发", "witness")
    svc.register_facility(EV, "W-山口供水站", "V-山口村", critical=True)

    # 1) 未经核实的群传先到
    svc.ingest({"event_key": EV, "kind": "road", "location_code": "V-山口村",
                "agency": "村民转发", "external_id": "wx-1", "observed_at": t(0),
                "summary": "群里说四号路被冰埋了", "road_code": "R-4号路",
                "road_state": "closed", "severity": "major"})
    svc.ingest({"event_key": EV, "kind": "people", "location_code": "V-山口村",
                "agency": "村民转发", "external_id": "wx-2", "observed_at": t(2),
                "summary": "转：山口村八十多人被困", "affected_people": 80,
                "severity": "critical"})
    # 同两条消息再次转发 —— 重复，不重复派遣
    dup = svc.ingest({"event_key": EV, "kind": "people", "location_code": "V-山口村",
                      "agency": "村民转发", "external_id": "wx-2",
                      "observed_at": t(2), "summary": "转：山口村八十多人被困",
                      "affected_people": 80, "severity": "critical"})
    print("重复转发判定 duplicated =", dup["duplicated"])

    q1 = svc.assessment(EV, as_of=t(5))
    show("接报后处置队列（传闻可信度低，P1 暂降待核实）", {
        "queue": [{"location": x["location_code"], "level": x["level"],
                   "score": x["score"], "confidence": x["confidence"],
                   "headline": x["headline"],
                   "components": [c["name"] for c in x["components"]],
                   "contradictions": x["contradictions"]}
                  for x in q1["queue"]]})

    # 2) 先封控、派核警力量；重复派遣被拦截
    svc.road_action({"event_key": EV, "road_code": "R-4号路", "action": "road.close",
                     "actor": "值班指挥员", "reason": "疑似掩埋先封控", "at": t(10)})
    svc.dispatch({"event_key": EV, "unit": "派出所前突组", "target": "R-4号路",
                  "purpose": "道路核警", "at": t(12)})
    try:
        svc.dispatch({"event_key": EV, "unit": "另一前突组", "target": "R-4号路",
                      "purpose": "道路核警", "at": t(13)})
    except Conflict as exc:
        print("重复派遣被拦截：", exc)

    # 3) 官方复核：道路可通行、23 人受影响、供水站中断
    svc.ingest({"event_key": EV, "kind": "road", "location_code": "V-山口村",
                "agency": "县应急局", "external_id": "of-1", "observed_at": t(40),
                "summary": "核实：可单向缓行", "road_code": "R-4号路",
                "road_state": "open", "severity": "info"})
    svc.ingest({"event_key": EV, "kind": "water", "location_code": "V-山口村",
                "agency": "水利站", "external_id": "wd-1", "observed_at": t(45),
                "summary": "取水口损毁停水", "facility": "W-山口供水站",
                "facility_state": "outage", "severity": "major"})
    svc.ingest({"event_key": EV, "kind": "people", "location_code": "V-山口村",
                "agency": "县应急局", "external_id": "of-2", "observed_at": t(50),
                "summary": "逐户清点23人", "affected_people": 23,
                "severity": "major"})
    q2 = svc.assessment(EV, as_of=t(55))
    item = q2["queue"][0]
    print("复核后采信道路状态：", item["current_view"]["roads"]["R-4号路"]["claimed_state"])
    print("复核后采信人口：", item["people_estimate"], "，矛盾条目：",
          item["contradictions"])

    # 4) 临时开放送水、力量转用
    svc.road_action({"event_key": EV, "road_code": "R-4号路",
                     "action": "road.temporary_open", "end_time": t(150),
                     "actor": "值班指挥员", "reason": "送水车放行", "at": t(60)})
    svc.dispatch_update({"event_key": EV, "dispatch_code": f"D-{EV}-001",
                         "action": "dispatch.redeploy",
                         "new_target": "W-山口供水站", "at": t(65)})
    svc.dispatch({"event_key": EV, "unit": "送水班组", "target": "V-山口村",
                  "purpose": "应急供水", "at": t(70)})

    # 5) 恢复 → 收队 → 更正人数 → 解除
    svc.ingest({"event_key": EV, "kind": "water", "location_code": "V-山口村",
                "agency": "水利站", "external_id": "wd-2", "observed_at": t(300),
                "summary": "抢修完成恢复供水", "facility": "W-山口供水站",
                "facility_state": "restored", "severity": "info"})
    svc.road_action({"event_key": EV, "road_code": "R-4号路", "action": "road.reopen",
                     "actor": "值班指挥员", "reason": "清理完毕", "at": t(305)})
    svc.dispatch_update({"event_key": EV, "dispatch_code": f"D-{EV}-001",
                         "action": "dispatch.standdown", "at": t(310)})
    svc.dispatch_update({"event_key": EV, "dispatch_code": f"D-{EV}-002",
                         "action": "dispatch.standdown", "at": t(310)})
    svc.ingest({"event_key": EV, "kind": "people", "location_code": "V-山口村",
                "agency": "县应急局", "external_id": "of-3", "observed_at": t(320),
                "summary": "23人已安置，更正of-2", "affected_people": 0,
                "severity": "info", "corrects": "of-2"})
    svc.resolve(EV, actor="值班指挥员", reason="现场全部恢复", at=t(335))
    print("事件状态：", svc.event(EV)["status"])

    # 6) 解除后新险情：重开但解除决策保留
    svc.ingest({"event_key": EV, "kind": "road", "location_code": "V-山口村",
                "agency": "边境派出所", "external_id": "pb-1", "observed_at": t(400),
                "summary": "支沟再次滑塌 K3 中断", "road_code": "R-4号路",
                "road_state": "closed", "severity": "major"})
    print("新消息后事件状态：", svc.event(EV)["status"])

    chain = svc.chain(EV)
    show("可追溯事件链（动作 + 每条决策的依据快照时间）", {
        "timeline": [{"id": d["id"], "subject": f'{d["subject_type"]}:{d["subject_id"]}',
                      "action": d["action"], "actor": d["actor"],
                      "basis_as_of": d["basis"].get("as_of"),
                      "active": d["active"]}
                     for d in chain["timeline"]],
        "raw_message_count": len(svc.raw_messages(EV)),
        "report_statuses": [r["status"] for r in svc.reports(EV)],
    })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
