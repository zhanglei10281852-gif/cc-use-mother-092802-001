"""说法台账：矛盾保留、更正取代、撤销留痕。"""

import unittest
from datetime import datetime, timedelta, timezone

from helpers import START, make_report, make_service
from mountain_response.core import ServiceError

UTC = timezone.utc


def _road_report(external_id, agency, value, observed_at):
    return make_report(
        agency=agency,
        external_id=external_id,
        observed_at=observed_at,
        summary=f"{agency} 称 R-4 {value}",
    ), [{"aspect": "road_status", "value": value}]


class ClaimsHistoryTests(unittest.TestCase):
    def test_contradictory_claims_are_all_kept(self):
        svc, _ = make_service()
        svc.set_source_reliability("巡查组", 0.9)
        svc.set_source_reliability("微信群", 0.2)
        r1, c1 = _road_report("a", "巡查组", "blocked", START)
        r2, c2 = _road_report("b", "微信群", "passable", START + timedelta(minutes=5))
        svc.receive_report(r1, c1)
        svc.receive_report(r2, c2)

        claims = svc.claims_for_location("R-4")
        self.assertEqual({c["value"] for c in claims}, {"blocked", "passable"})
        judgment = svc.judgment_for_location("R-4")["judgments"]["road_status"]
        self.assertTrue(judgment["contested"])
        self.assertEqual(judgment["value"], "blocked")  # 高可信来源胜出

    def test_correction_supersedes_but_keeps_original(self):
        svc, clock = make_service()
        svc.receive_report(make_report(people=30), claims=[{"aspect": "people_trapped", "value": "30"}])
        claim_id = svc.claims_for_location("R-4")[0]["claim_id"]
        clock.advance(hours=1)
        svc.correct_claim(claim_id, "12", "逐户核实")

        current = svc.judgment_for_location("R-4")["judgments"]["people_trapped"]
        self.assertEqual(current["value"], "12")
        statuses = {
            c["value"]: c["status"]
            for c in svc.claims_for_location("R-4")
            if c["aspect"] == "people_trapped"
        }
        self.assertEqual(statuses["30"], "superseded")
        self.assertEqual(statuses["12"], "active")

    def test_revoke_keeps_record_and_excludes_from_judgment(self):
        svc, _ = make_service()
        svc.receive_report(make_report(), claims=[{"aspect": "road_status", "value": "blocked"}])
        claim_id = svc.claims_for_location("R-4")[0]["claim_id"]
        svc.revoke_claim(claim_id, "误报")

        self.assertIsNone(svc.judgment_for_location("R-4")["judgments"].get("road_status"))
        claim = svc.claims_for_location("R-4")[0]
        self.assertEqual(claim["status"], "revoked")
        self.assertEqual(claim["revoke_reason"], "误报")

    def test_cannot_correct_or_revoke_twice(self):
        svc, _ = make_service()
        svc.receive_report(make_report(), claims=[{"aspect": "road_status", "value": "blocked"}])
        claim_id = svc.claims_for_location("R-4")[0]["claim_id"]
        svc.revoke_claim(claim_id, "误报")
        with self.assertRaises(ServiceError):
            svc.revoke_claim(claim_id, "再次撤销")
        with self.assertRaises(ServiceError):
            svc.correct_claim(claim_id, "passable", "改口")

    def test_missing_claim_returns_404(self):
        svc, _ = make_service()
        with self.assertRaises(ServiceError) as ctx:
            svc.revoke_claim("clm-999999-0", "不存在")
        self.assertEqual(ctx.exception.status, 404)


if __name__ == "__main__":
    unittest.main()
