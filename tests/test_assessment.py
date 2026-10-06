import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from mountain_response.service import Service
from mountain_response.timeutil import iso

T0 = datetime(2026, 9, 27, 4, 0, tzinfo=timezone.utc)


def base(**over):
    payload = {
        "event_key": "EV", "kind": "people", "location_code": "V1",
        "agency": "unknown-source", "external_id": "x",
        "observed_at": iso(T0), "summary": "s", "affected_people": 0,
        "severity": "info",
    }
    payload.update(over)
    return payload


class TriageTests(unittest.TestCase):
    def setUp(self):
        self.svc = Service()
        # 边民=目击者；应急局=官方；现场队=救援力量
        self.svc.set_source_trust("应急局", "official")
        self.svc.set_source_trust("现场队", "responder")
        self.svc.set_source_trust("边民", "witness")

    def test_queue_order_and_levels(self):
        self.svc.ingest(base(external_id="a", location_code="V1", affected_people=120,
                             severity="critical", agency="应急局"))
        self.svc.ingest(base(external_id="b", location_code="V2", affected_people=25,
                             severity="minor", agency="边民"))
        self.svc.ingest(base(external_id="c", location_code="V3", affected_people=0,
                             severity="info", agency="边民"))
        queue = self.svc.assessment(
            "EV", as_of=iso(T0 + timedelta(minutes=5)))["queue"]
        self.assertEqual([q["location_code"] for q in queue], ["V1", "V2", "V3"])
        self.assertEqual(queue[0]["level"], "P1")
        self.assertIn(queue[2]["level"], ("P3", "P4"))

    def test_score_is_explainable_component_by_component(self):
        self.svc.ingest(base(external_id="a", location_code="V1", affected_people=30,
                             severity="major", agency="应急局"))
        item = self.svc.assessment(
            "EV", as_of=iso(T0 + timedelta(minutes=5)))["queue"][0]
        names = {c["name"]: c["points"] for c in item["components"]}
        self.assertIn("受影响人口", names)
        self.assertIn("灾情严重度", names)
        # 每一分都能给出原因
        for c in item["components"]:
            self.assertTrue(c["why"])

    def test_contradictory_claims_are_both_kept_and_weighted_by_trust(self):
        # 同一条路：边民群传说封闭，应急局核实可通行
        self.svc.ingest(base(external_id="r1", kind="road", location_code="V1",
                             agency="边民", road_code="R4", road_state="closed",
                             summary="群传 R4 被冰体掩埋"))
        self.svc.ingest(base(external_id="r2", kind="road", location_code="V1",
                             agency="应急局", road_code="R4", road_state="open",
                             summary="现场核实 R4 可通行"))
        item = self.svc.assessment(
            "EV", as_of=iso(T0 + timedelta(minutes=5)))["queue"][0]
        road = item["current_view"]["roads"]["R4"]
        self.assertTrue(road["claim_disputed"])
        # 应急局可信度高于边民 → 采信 open
        self.assertEqual(road["claimed_state"], "open")
        # 矛盾必须显式呈现，且两条原始说法都在证据里
        self.assertTrue(any("R4" in c for c in item["contradictions"]))
        self.assertEqual(len(item["evidence"]), 2)
        self.assertEqual(
            road["claim_credibility_by_state"]["open"],
            max(v["credibility"] for v in item["evidence"]
                if v["road_state"] == "open"),
        )

    def test_stale_information_scores_lower_but_still_visible(self):
        self.svc.ingest(base(external_id="fresh", affected_people=30,
                             observed_at=iso(T0), agency="应急局"))
        fresh = self.svc.assessment("EV", as_of=iso(T0 + timedelta(minutes=10)))["queue"][0]
        stale = self.svc.assessment("EV", as_of=iso(T0 + timedelta(hours=30)))["queue"][0]
        self.assertLess(stale["confidence"], fresh["confidence"])
        self.assertTrue(any("超过24小时" in f for f in stale["confidence_factors"]))

    def test_critical_facility_outage_raises_priority(self):
        self.svc.register_facility("EV", "W-山口供水站", "V1", critical=True)
        self.svc.ingest(base(external_id="w1", kind="water", location_code="V1",
                             agency="现场队", facility="W-山口供水站",
                             facility_state="outage", affected_people=0,
                             summary="供水站取水口损毁"))
        item = self.svc.assessment(
            "EV", as_of=iso(T0 + timedelta(minutes=5)))["queue"][0]
        names = [c["name"] for c in item["components"]]
        self.assertIn("关键供水设施中断", names)
        self.assertEqual(item["current_view"]["facilities"]["W-山口供水站"]["critical"], True)

    def test_people_floor_rule_protects_major_casualty_reports(self):
        self.svc.ingest(base(external_id="p", affected_people=100, severity="major",
                             agency="应急局"))
        item = self.svc.assessment(
            "EV", as_of=iso(T0 + timedelta(minutes=5)))["queue"][0]
        self.assertIn(item["level"], ("P1", "P2"))
        self.assertTrue(
            any("保底" in f for f in item["confidence_factors"])
            or item["level"] == "P1")


if __name__ == "__main__":
    unittest.main()
