"""端到端场景：同一事件从接报、复核到解除，指挥端始终能区分当前判断与历史依据。"""

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from mountain_response.service import Service
from mountain_response.storage import Conflict
from mountain_response.timeutil import iso

T0 = datetime(2026, 9, 27, 3, 30, tzinfo=timezone.utc)
EV = "ICEFALL-BORDER-01"


def road(agency, eid, minutes, state, summary, **kw):
    p = {
        "event_key": EV, "kind": "road", "location_code": "V-山口村",
        "agency": agency, "external_id": eid,
        "observed_at": iso(T0 + timedelta(minutes=minutes)),
        "summary": summary, "road_code": "R-边境4号路", "road_state": state,
        "severity": kw.pop("severity", "major" if state == "closed" else "info"),
    }
    p.update(kw)
    return p


def people(agency, eid, minutes, n, summary, severity="major", **kw):
    p = {
        "event_key": EV, "kind": "people", "location_code": "V-山口村",
        "agency": agency, "external_id": eid,
        "observed_at": iso(T0 + timedelta(minutes=minutes)),
        "summary": summary, "affected_people": n, "severity": severity,
    }
    p.update(kw)
    return p


def water(agency, eid, minutes, state, summary, **kw):
    p = {
        "event_key": EV, "kind": "water", "location_code": "V-山口村",
        "agency": agency, "external_id": eid,
        "observed_at": iso(T0 + timedelta(minutes=minutes)),
        "summary": summary, "facility": "W-山口供水站", "facility_state": state,
        "severity": "info" if state == "restored" else "major",
    }
    p.update(kw)
    return p


class FullLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.svc = Service()
        self.svc.set_source_trust("县应急局", "official", note="官方核实渠道")
        self.svc.set_source_trust("边境派出所", "responder")
        self.svc.set_source_trust("水利站", "responder")
        self.svc.set_source_trust("村民转发", "witness")
        self.svc.register_facility(EV, "W-山口供水站", "V-山口村", critical=True)

    def test_report_review_resolution_lifecycle(self):
        as_of = iso(T0 + timedelta(minutes=5))

        # 1) 凌晨先到两条未经核实的转发：道路封死、80人被困
        self.svc.ingest(road("村民转发", "wx-1001", 0, "closed",
                             "群里说四号路被冰埋了"))
        self.svc.ingest(people("村民转发", "wx-1002", 2, 80,
                               "转：山口村八十多口人困在里头"))
        first = self.svc.assessment(EV, as_of=as_of)
        item = first["queue"][0]
        self.assertEqual(item["level"], "P2")  # 重大传闻但可信度低
        self.assertTrue(item["evidence"], "当前判断必须挂着原始依据")
        self.assertLess(item["confidence"], 0.8)

        # 2) 同两条消息被另外两个值班群转发 —— 不得重复派遣
        self.svc.ingest(road("村民转发", "wx-1001", 1, "closed",
                             "群里说四号路被冰埋了"))
        self.svc.ingest(people("村民转发", "wx-1002", 3, 80,
                               "转：山口村八十多口人困在里头"))
        self.assertEqual(len(self.svc.reports(EV)), 2)
        self.assertEqual(len(self.svc.raw_messages(EV)), 4, "四次到达全部留痕")

        # 3) 指挥员先按风险封闭道路、派一组力量核警
        self.svc.road_action({
            "event_key": EV, "road_code": "R-边境4号路", "action": "road.close",
            "actor": "值班指挥员", "reason": "疑似冰体掩埋，先封控",
            "at": iso(T0 + timedelta(minutes=10))})
        self.svc.dispatch({
            "event_key": EV, "unit": "派出所前突组", "target": "R-边境4号路",
            "purpose": "道路核警", "actor": "值班指挥员",
            "at": iso(T0 + timedelta(minutes=12))})
        # 重复报文驱动的"再派一次"必须被挡住
        with self.assertRaises(Conflict):
            self.svc.dispatch({
                "event_key": EV, "unit": "另一支前突组",
                "target": "R-边境4号路", "purpose": "道路核警",
                "at": iso(T0 + timedelta(minutes=13))})

        # 4) 官方复核到场：道路可缓慢通行，但供水站确实中断；被困人数实为 23
        self.svc.ingest(road("县应急局", "of-201", 40, "open",
                             "现场核实：四号路可单向缓行"))
        self.svc.ingest(water("水利站", "wd-301", 45, "outage",
                              "取水口被冰碛损毁，全站停水"))
        self.svc.ingest(people("县应急局", "of-202", 50, 23,
                               "逐户清点：23人受影响，更正网传80人"))
        review = self.svc.assessment(EV, as_of=iso(T0 + timedelta(minutes=55)))
        item = review["queue"][0]
        road_view = item["current_view"]["roads"]["R-边境4号路"]
        self.assertTrue(road_view["claim_disputed"], "矛盾仍需显式呈现")
        self.assertEqual(road_view["claimed_state"], "open",
                         "高可信度官方说法应成为当前判断")
        self.assertEqual(item["people_estimate"], 23)
        self.assertTrue(any("矛盾" in c for c in item["contradictions"]))
        self.assertEqual(len(item["evidence"]), 5, "所有 active 原始说法都在")
        # 历史依据：村民的两条说法未被同源更正，仍是 active（保留矛盾）；
        # 指挥端可以在 contradictions 中看到两方依据

        # 5) 道路改为临时开放（放行送水车 90 分钟）
        self.svc.road_action({
            "event_key": EV, "road_code": "R-边境4号路",
            "action": "road.temporary_open", "actor": "值班指挥员",
            "end_time": iso(T0 + timedelta(minutes=150)),
            "reason": "送水车队单向放行",
            "at": iso(T0 + timedelta(minutes=60))})
        # 前突组转去供水站
        self.svc.dispatch_update({
            "event_key": EV, "dispatch_code": "D-" + EV + "-001",
            "action": "dispatch.redeploy", "new_target": "W-山口供水站",
            "reason": "道路已核，转查供水",
            "at": iso(T0 + timedelta(minutes=65))})
        self.svc.dispatch({
            "event_key": EV, "unit": "送水班组", "target": "V-山口村",
            "purpose": "应急供水", "actor": "值班指挥员",
            "at": iso(T0 + timedelta(minutes=70))})

        # 6) 临时窗口过期：运营态自动恢复封闭，但临时开放的决策不被抹掉
        expired = self.svc.assessment(EV, as_of=iso(T0 + timedelta(minutes=200)))
        op = expired["queue"][0]["current_view"]["roads"]["R-边境4号路"]["operational"]
        self.assertEqual(op["state"], "closed")
        self.assertTrue(op["temporary_window_expired"])

        # 7) 水利站修复并恢复供水；道路恢复开放
        self.svc.ingest(water("水利站", "wd-302", 300, "restored",
                              "取水口抢修完成，恢复供水"))
        self.svc.road_action({
            "event_key": EV, "road_code": "R-边境4号路",
            "action": "road.reopen", "actor": "值班指挥员",
            "reason": "冰碛清理完毕，双向恢复",
            "at": iso(T0 + timedelta(minutes=305))})
        self.svc.dispatch_update({
            "event_key": EV, "dispatch_code": "D-" + EV + "-001",
            "action": "dispatch.standdown", "reason": "供水站任务结束",
            "at": iso(T0 + timedelta(minutes=310))})
        self.svc.dispatch_update({
            "event_key": EV, "dispatch_code": "D-" + EV + "-002",
            "action": "dispatch.standdown", "reason": "应急供水结束",
            "at": iso(T0 + timedelta(minutes=310))})

        # 8) 23 人转移安置完毕：官方以更正形式撤销其受影响说法
        self.svc.ingest(people("县应急局", "of-203", 320, 0,
                               "23人已全部转移安置", severity="info",
                               corrects="of-202"))
        final = self.svc.assessment(EV, as_of=iso(T0 + timedelta(minutes=330)))
        self.assertNotIn("P1", [q["level"] for q in final["queue"]])
        self.assertNotIn("P2", [q["level"] for q in final["queue"]])

        result = self.svc.resolve(EV, actor="值班指挥员", reason="现场全部恢复",
                                  at=iso(T0 + timedelta(minutes=335)))
        self.assertEqual(self.svc.event(EV)["status"], "resolved")

        # 9) 解除后又来一条迟到的新险情 → 事件重开，但解除决策仍在链上
        self.svc.ingest(road("边境派出所", "pb-401", 400, "closed",
                             "支沟再次滑塌，四号路K3处中断"))
        self.assertEqual(self.svc.event(EV)["status"], "active")
        chain = self.svc.chain(EV)
        timeline = chain["timeline"]
        resolve_idx = next(i for i, d in enumerate(timeline)
                           if d["action"] == "event.resolve")
        reopen_idx = next(i for i, d in enumerate(timeline)
                          if d["action"] == "event.reopen")
        self.assertLess(resolve_idx, reopen_idx)
        # 解除时固化的快照在重开后原样可查
        self.assertIn("final_snapshot", timeline[resolve_idx]["basis"])
        resolve_snapshot = timeline[resolve_idx]["basis"]["final_snapshot"]
        self.assertEqual(resolve_snapshot["totals"]["locations"], 1)

        # 10) 全链可追溯：动作顺序、主体链、每步依据齐全
        actions = [(d["subject_type"], d["action"]) for d in timeline]
        self.assertIn(("road", "road.close"), actions)
        self.assertIn(("road", "road.temporary_open"), actions)
        self.assertIn(("road", "road.reopen"), actions)
        self.assertIn(("dispatch", "dispatch.create"), actions)
        self.assertIn(("event", "event.resolve"), actions)
        for d in timeline:
            self.assertTrue(d["basis"], f"决策 {d['id']} 缺少依据快照")

        # 历史报告一条不少：撤销/更正均为状态标记，而非删除
        statuses = sorted(r["status"] for r in self.svc.reports(EV))
        self.assertIn("superseded", statuses)
        self.assertGreaterEqual(len(self.svc.raw_messages(EV)), 10)


if __name__ == "__main__":
    unittest.main()
