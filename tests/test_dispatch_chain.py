"""派遣幂等与事件链：哈希链完整、篡改可发现、决策依据随事件固化。"""

import dataclasses
import unittest

from helpers import START, make_report, make_service
from mountain_response.core import ServiceError
from mountain_response.store import EventStore


def _open_need(svc):
    svc.receive_report(make_report(people=8), claims=[{"aspect": "people_trapped", "value": "8"}])
    return svc.queue()[0]["need_id"]


class DispatchChainTests(unittest.TestCase):
    def test_dispatch_is_idempotent_per_need(self):
        svc, _ = make_service()
        need_id = _open_need(svc)
        first = svc.order_dispatch(need_id, ["救援队A"], "首次", "值班员")
        second = svc.order_dispatch(need_id, ["救援队B"], "重复", "值班员")
        self.assertFalse(first["deduplicated"])
        self.assertTrue(second["deduplicated"])
        self.assertEqual(len(svc.events(event_type="dispatch_ordered")), 1)
        self.assertEqual(svc.state()["audit"]["dispatch_requests_deduplicated"], 1)

    def test_dispatch_carries_score_snapshot_as_basis(self):
        svc, _ = make_service()
        need_id = _open_need(svc)
        svc.order_dispatch(need_id, ["救援队A"], "首次", "值班员")
        event = svc.events(event_type="dispatch_ordered")[0]
        snapshot = event["payload"]["basis"]["score_snapshot"]
        self.assertEqual(snapshot["need_id"], need_id)
        self.assertTrue(snapshot["explanation"]["basis_claim_ids"])

    def test_dispatch_rejected_for_unknown_need(self):
        svc, _ = make_service()
        with self.assertRaises(ServiceError) as ctx:
            svc.order_dispatch("need-000000000000", [], "无此需求", "值班员")
        self.assertEqual(ctx.exception.status, 409)

    def test_road_events_form_traceable_chain_with_basis(self):
        svc, _ = make_service()
        svc.receive_report(make_report(), claims=[{"aspect": "road_status", "value": "blocked"}])
        svc.record_road_event("close", "R-4", "封闭", "值班员")
        svc.record_road_event("temp-open", "R-4", "车队通过", "值班员")
        svc.record_road_event("reopen", "R-4", "抢通", "值班员")
        road_events = [e for e in svc.events() if e["event_type"].startswith("road_")]
        self.assertEqual([e["event_type"] for e in road_events],
                         ["road_closed", "road_temporarily_opened", "road_reopened"])
        self.assertEqual(road_events[0]["payload"]["basis"]["judgment_value"], "blocked")
        self.assertEqual(svc.judgment_for_location("R-4")["road_status"]["status"], "reopened")

    def test_hash_chain_verifies_and_detects_tamper(self):
        svc, _ = make_service()
        _open_need(svc)
        ok, _ = svc.store.verify()
        self.assertTrue(ok)
        # 篡改内存中的事件内容，校验必须失败
        victim = svc.store._events[0]
        svc.store._events[0] = dataclasses.replace(
            victim, payload={**victim.payload, "note": "被篡改"}
        )
        ok, detail = svc.store.verify()
        self.assertFalse(ok)
        self.assertIn("哈希不符", detail)

    def test_store_persists_and_reloads(self):
        import tempfile
        from pathlib import Path

        from mountain_response.clock import ManualClock
        from mountain_response.core import CoordinationService

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            clock = ManualClock(START)
            svc = CoordinationService(EventStore(path), clock)
            svc.receive_report(make_report(), claims=[{"aspect": "road_status", "value": "blocked"}])
            reloaded = CoordinationService(EventStore(path), clock)
            self.assertEqual(len(reloaded.claims_for_location("R-4")), 1)
            self.assertTrue(reloaded.verify_chain()["ok"])


if __name__ == "__main__":
    unittest.main()
