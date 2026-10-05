"""接报与去重：重复报文不得产生新说法，更不得触发派遣。"""

import unittest
from datetime import timedelta

from helpers import START, make_report, make_service


class IngestDedupTests(unittest.TestCase):
    def test_first_report_creates_claims_with_source_and_observation_time(self):
        svc, _ = make_service()
        result = svc.receive_report(
            make_report(people=12),
            claims=[{"aspect": "road_status", "value": "blocked"}],
        )
        self.assertFalse(result["deduplicated"])
        self.assertEqual(len(result["claim_ids"]), 2)  # 显式说法 + 受影响人数
        claims = svc.claims_for_location("R-4")
        self.assertTrue(all(c["agency"] == "边境巡查组" for c in claims))
        self.assertTrue(all(c["observed_at"] == START.isoformat() for c in claims))

    def test_duplicate_report_is_idempotent(self):
        svc, _ = make_service()
        first = svc.receive_report(make_report(), claims=[{"aspect": "road_status", "value": "blocked"}])
        second = svc.receive_report(make_report(), claims=[{"aspect": "road_status", "value": "blocked"}])
        self.assertTrue(second["deduplicated"])
        self.assertEqual(first["report_id"], second["report_id"])
        self.assertEqual(len(svc.claims_for_location("R-4")), 1)
        self.assertEqual(svc.state()["audit"]["duplicate_reports_ignored"], 1)

    def test_same_source_new_external_id_is_a_new_report(self):
        svc, _ = make_service()
        svc.receive_report(make_report(external_id="a-1"))
        other = svc.receive_report(make_report(external_id="a-2"))
        self.assertFalse(other["deduplicated"])

    def test_duplicate_report_does_not_change_queue_or_dispatch(self):
        svc, clock = make_service()
        svc.receive_report(
            make_report(people=5),
            claims=[{"aspect": "people_trapped", "value": "5"}],
        )
        queue_before = svc.queue()
        clock.advance(minutes=1)
        svc.receive_report(make_report(people=5), claims=[{"aspect": "people_trapped", "value": "5"}])
        queue_after = svc.queue()
        self.assertEqual([q["need_id"] for q in queue_before], [q["need_id"] for q in queue_after])
        need_id = queue_before[0]["need_id"]
        svc.order_dispatch(need_id, ["救援队A"], "首次", "值班员")
        svc.receive_report(make_report(people=5), claims=[{"aspect": "people_trapped", "value": "5"}])
        self.assertEqual(len(svc.events(event_type="dispatch_ordered")), 1)

    def test_naive_observed_at_rejected(self):
        svc, _ = make_service()
        from datetime import datetime

        from mountain_response.core import ServiceError

        with self.assertRaises(ServiceError):
            svc.receive_report(make_report(observed_at=datetime(2026, 10, 5, 4, 0)))


if __name__ == "__main__":
    unittest.main()
