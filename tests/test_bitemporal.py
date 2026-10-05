"""双时间语义：迟到消息不改写历史，as_of 回放区分当前判断与历史依据。"""

import unittest
from datetime import datetime, timedelta, timezone

from helpers import START, make_report, make_service

UTC = timezone.utc


class BitemporalTests(unittest.TestCase):
    def setUp(self):
        self.svc, self.clock = make_service()
        self.svc.set_source_reliability("巡查组", 0.9)
        self.svc.set_source_reliability("监测站", 0.6)
        # 04:00 巡查组观测受困 30 人，04:20 入库
        self.clock.set(datetime(2026, 10, 5, 4, 20, tzinfo=UTC))
        self.svc.receive_report(
            make_report(agency="巡查组", external_id="x-1", people=30),
            claims=[{"aspect": "people_trapped", "value": "30"}],
        )
        self.t_first = self.clock.now()

    def test_late_report_recorded_but_does_not_rewrite_history(self):
        # 05:10 更正为 12 人
        self.clock.set(datetime(2026, 10, 5, 5, 10, tzinfo=UTC))
        claim_id = self.svc.claims_for_location("R-4")[0]["claim_id"]
        self.svc.correct_claim(claim_id, "12", "核实")
        # 05:20 迟到的监测站报告（观测于 03:50）入库
        self.clock.set(datetime(2026, 10, 5, 5, 20, tzinfo=UTC))
        self.svc.receive_report(
            make_report(agency="监测站", external_id="z-9",
                        observed_at=datetime(2026, 10, 5, 3, 50, tzinfo=UTC), people=45),
            claims=[{"aspect": "people_trapped", "value": "45"}],
        )
        # 当前判断：巡查组（0.9）的 12 人仍胜出
        current = self.svc.judgment_for_location("R-4")["judgments"]["people_trapped"]
        self.assertEqual(current["value"], "12")
        # 历史回放：04:20 时刻的判断仍是 30 人
        historical = self.svc.state(as_of=self.t_first)
        judgment_then = historical["locations"]["R-4"]["judgments"]["people_trapped"]
        self.assertEqual(judgment_then["value"], "30")
        self.assertTrue(historical["is_historical"])

    def test_revocation_visible_now_but_not_in_past(self):
        claim_id = self.svc.claims_for_location("R-4")[0]["claim_id"]
        self.clock.set(datetime(2026, 10, 5, 6, 0, tzinfo=UTC))
        self.svc.revoke_claim(claim_id, "撤回")
        now = self.svc.state()
        then = self.svc.state(as_of=self.t_first)
        self.assertNotIn("people_trapped", now["locations"]["R-4"]["judgments"])
        self.assertEqual(
            then["locations"]["R-4"]["judgments"]["people_trapped"]["value"], "30"
        )

    def test_decision_basis_survives_later_revocation(self):
        need_id = self.svc.queue()[0]["need_id"]
        self.svc.order_dispatch(need_id, ["救援队A"], "按 30 人规模派出", "值班员")
        claim_id = self.svc.claims_for_location("R-4")[0]["claim_id"]
        self.clock.set(datetime(2026, 10, 5, 6, 0, tzinfo=UTC))
        self.svc.revoke_claim(claim_id, "撤回")
        event = self.svc.events(event_type="dispatch_ordered")[0]
        snapshot = event["payload"]["basis"]["score_snapshot"]
        people_factor = next(
            f for f in snapshot["explanation"]["factors"] if f["name"] == "affected_people"
        )
        self.assertEqual(people_factor["raw"], 30)  # 决策依据保持当时口径

    def test_state_marks_historical_flag(self):
        current = self.svc.state()
        self.assertFalse(current["is_historical"])
        past = self.svc.state(as_of=self.t_first)
        self.assertTrue(past["is_historical"])
        self.assertEqual(past["as_of"], self.t_first.isoformat())


if __name__ == "__main__":
    unittest.main()
