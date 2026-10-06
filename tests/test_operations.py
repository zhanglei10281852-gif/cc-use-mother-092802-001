import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from mountain_response.operations import OperationError
from mountain_response.service import Service
from mountain_response.storage import Conflict
from mountain_response.timeutil import iso

T0 = datetime(2026, 9, 27, 4, 0, tzinfo=timezone.utc)


def people(event="EV", eid="p1", people=30, agency="应急局", minutes=0, severity="major",
           location="V1"):
    return {
        "event_key": event, "kind": "people", "location_code": location,
        "agency": agency, "external_id": eid,
        "observed_at": iso(T0 + timedelta(minutes=minutes)),
        "summary": f"{people}人受困", "affected_people": people, "severity": severity,
    }


class RoadChainTests(unittest.TestCase):
    def setUp(self):
        self.svc = Service()

    def test_close_then_temporary_open_then_expiry(self):
        at0 = iso(T0)
        r1 = self.svc.road_action({
            "event_key": "EV", "road_code": "R4", "action": "road.close",
            "reason": "冰崩堆积", "actor": "指挥员A", "at": at0})
        # 重复封闭幂等
        r2 = self.svc.road_action({
            "event_key": "EV", "road_code": "R4", "action": "road.close",
            "at": at0})
        self.assertTrue(r2["idempotent"])

        end = iso(T0 + timedelta(hours=2))
        opened = self.svc.road_action({
            "event_key": "EV", "road_code": "R4",
            "action": "road.temporary_open", "end_time": end,
            "reason": "救援车队单向放行2小时", "at": at0})
        self.assertFalse(opened["idempotent"])

        during = self.svc.assessment("EV", as_of=iso(T0 + timedelta(minutes=30)))
        # 无报告位置，队列可能为空，直接查决策链验证运营态
        chain = self.svc.chain("EV")
        self.assertEqual([d["action"] for d in chain["timeline"]],
                         ["road.close", "road.temporary_open"])
        # 上一条封闭决策必须仍在且被标记失效（不是删除）
        self.assertEqual(chain["timeline"][0]["active"], 0)
        self.assertEqual(chain["timeline"][0]["deactivated_by"],
                         chain["timeline"][1]["id"])
        # 每条决策都带依据快照
        self.assertIn("as_of", chain["timeline"][0]["basis"])

        # 临时开放过期 → 运营态自动回到 closed
        self.svc.ingest(people(eid="road-claim", people=0, severity="info"))
        # 通过道路报告让 R4 进入评估视图
        self.svc.ingest({
            "event_key": "EV", "kind": "road", "location_code": "V1",
            "agency": "应急局", "external_id": "rc1", "observed_at": iso(T0),
            "summary": "R4 封闭", "road_code": "R4", "road_state": "closed"})
        view = self.svc.assessment("EV", as_of=iso(T0 + timedelta(hours=3)))
        road = view["queue"][0]["current_view"]["roads"]["R4"]
        self.assertEqual(road["operational"]["state"], "closed")
        self.assertTrue(road["operational"]["temporary_window_expired"])

    def test_temporary_open_requires_future_end_time(self):
        self.svc.road_action({"event_key": "EV", "road_code": "R4",
                              "action": "road.close"})
        with self.assertRaises(OperationError):
            self.svc.road_action({
                "event_key": "EV", "road_code": "R4",
                "action": "road.temporary_open",
                "end_time": iso(T0 - timedelta(hours=1))})

    def test_historical_assessment_does_not_leak_future_decisions(self):
        # 道路报告称可通行；第 10 分钟才做出封闭决策
        self.svc.ingest({
            "event_key": "EV", "kind": "road", "location_code": "V1",
            "agency": "应急局", "external_id": "rc1", "observed_at": iso(T0),
            "summary": "R4 可通行", "road_code": "R4", "road_state": "open",
            "severity": "info"})
        self.svc.road_action({"event_key": "EV", "road_code": "R4",
                              "action": "road.close", "reason": "后续封控",
                              "at": iso(T0 + timedelta(minutes=10))})

        before = self.svc.assessment(
            "EV", as_of=iso(T0 + timedelta(minutes=5)))["queue"][0]
        self.assertEqual(
            before["current_view"]["roads"]["R4"]["operational"]["state"], "unknown",
            "复盘封闭前的时刻，不应看到未来的封闭决策")

        after = self.svc.assessment(
            "EV", as_of=iso(T0 + timedelta(minutes=15)))["queue"][0]
        self.assertEqual(
            after["current_view"]["roads"]["R4"]["operational"]["state"], "closed",
            "决策生效后，指挥决策覆盖报告说法，运营态为封闭")


class DispatchTests(unittest.TestCase):
    def setUp(self):
        self.svc = Service()
        self.svc.ingest(people(people=40))

    def test_duplicate_dispatch_to_same_target_is_rejected(self):
        d1 = self.svc.dispatch({"event_key": "EV", "unit": "搜救一队",
                                "target": "V1", "purpose": "人员转移"})
        self.assertEqual(d1["dispatch"]["status"], "dispatched")
        with self.assertRaises(Conflict):
            self.svc.dispatch({"event_key": "EV", "unit": "搜救二队",
                               "target": "V1", "purpose": "人员转移"})
        # 不同任务允许增援
        d2 = self.svc.dispatch({"event_key": "EV", "unit": "医疗组",
                                "target": "V1", "purpose": "医疗救护"})
        self.assertIsNotNone(d2["dispatch"]["id"])

    def test_duplicate_report_does_not_redispatch(self):
        """同一报文被转发两次 → 只产生一条 canonical 报告 → 只允许一次派遣。"""
        payload = people(eid="same", people=50)
        self.svc.ingest(payload)
        self.svc.ingest(dict(payload))
        self.svc.dispatch({"event_key": "EV", "unit": "搜救一队",
                           "target": "V1", "purpose": "人员转移"})
        with self.assertRaises(Conflict):
            self.svc.dispatch({"event_key": "EV", "unit": "搜救一队B",
                               "target": "V1", "purpose": "人员转移"})

    def test_dispatch_lifecycle_arrive_redeploy_standdown(self):
        at0 = iso(T0)
        at1 = iso(T0 + timedelta(minutes=10))
        at2 = iso(T0 + timedelta(minutes=20))
        at3 = iso(T0 + timedelta(minutes=30))
        at4 = iso(T0 + timedelta(minutes=40))
        self.svc.dispatch({"event_key": "EV", "unit": "搜救一队",
                           "target": "V1", "purpose": "人员转移",
                           "dispatch_code": "D-EV-001", "at": at0})
        self.svc.dispatch_update({"event_key": "EV", "dispatch_code": "D-EV-001",
                                  "action": "dispatch.arrive", "at": at1})
        self.svc.dispatch_update({"event_key": "EV", "dispatch_code": "D-EV-001",
                                  "action": "dispatch.redeploy", "new_target": "R4",
                                  "at": at2})
        # 原目标空出后可再次派遣
        again = self.svc.dispatch({"event_key": "EV", "unit": "搜救三队",
                                   "target": "V1", "purpose": "人员转移",
                                   "at": at3})
        self.assertEqual(again["dispatch"]["unit"], "搜救三队")

        self.svc.dispatch_update({"event_key": "EV", "dispatch_code": "D-EV-001",
                                  "action": "dispatch.standdown",
                                  "reason": "任务结束", "at": at4})
        # 收队不可撤销
        with self.assertRaises(OperationError):
            self.svc.dispatch_update({"event_key": "EV", "dispatch_code": "D-EV-001",
                                      "action": "dispatch.arrive", "at": at4})
        chain = self.svc.chain("EV")
        actions = [d["action"] for d in chain["timeline"]
                   if d["subject_type"] == "dispatch"]
        self.assertEqual(
            actions,
            ["dispatch.create", "dispatch.arrive", "dispatch.redeploy",
             "dispatch.create", "dispatch.standdown"])

    def test_dispatch_blocked_after_resolve(self):
        svc = Service()
        svc.ingest(people(event="E2", people=0, severity="info"))
        svc.resolve("E2")
        with self.assertRaises(OperationError):
            svc.dispatch({"event_key": "E2", "unit": "X", "target": "V1"})


class ResolveTests(unittest.TestCase):
    def setUp(self):
        self.svc = Service()
        self.svc.set_source_trust("应急局", "official")

    def test_resolve_blocked_while_urgent_open(self):
        self.svc.ingest(people(people=80))
        with self.assertRaises(OperationError):
            self.svc.resolve("EV", at=iso(T0 + timedelta(minutes=10)))

    def test_resolve_succeeds_after_dispatch_and_downgrade(self):
        at0 = iso(T0)
        self.svc.ingest(people(people=80))
        self.svc.dispatch({"event_key": "EV", "unit": "搜救一队",
                           "target": "V1", "purpose": "人员转移", "at": at0})
        # 官方更晚核实：实际为空村，同源更正此前 80 人说法
        self.svc.ingest({
            "event_key": "EV", "kind": "people", "location_code": "V1",
            "agency": "应急局", "external_id": "p2",
            "observed_at": iso(T0 + timedelta(minutes=125)),
            "summary": "核实为空村，更正 p1", "affected_people": 0,
            "severity": "info", "corrects": "p1"})
        # 收队后无在途力量、无 P1/P2 项
        self.svc.dispatch_update({
            "event_key": "EV", "dispatch_code": "D-EV-001",
            "action": "dispatch.standdown", "reason": "核实为空村",
            "at": iso(T0 + timedelta(minutes=130))})
        result = self.svc.resolve(
            "EV", reason="全部核实完毕", at=iso(T0 + timedelta(minutes=140)))
        self.assertEqual(result["decision"]["action"], "event.resolve")
        self.assertEqual(self.svc.event("EV")["status"], "resolved")
        # 决策链上仍能看到派遣时的高分快照（决策不可变）
        create = [d for d in self.svc.chain("EV")["timeline"]
                  if d["action"] == "dispatch.create"][0]
        self.assertIn("queue_summary", create["basis"])
        first_q = create["basis"]["queue_summary"][0]
        self.assertIn(first_q["level"], ("P1", "P2"))


if __name__ == "__main__":
    unittest.main()
