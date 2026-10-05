"""处置队列：可解释打分、争议惩罚、确定性排序。"""

import unittest
from datetime import timedelta

from helpers import START, make_report, make_service
from mountain_response.contracts import ReportKind


def _people_report(external_id, location, people, observed_at=START):
    return make_report(
        external_id=external_id,
        kind=ReportKind.PEOPLE,
        location=location,
        observed_at=observed_at,
        people=people,
    ), [{"aspect": "people_trapped", "value": str(people)}]


class QueueTests(unittest.TestCase):
    def test_score_equals_sum_of_factor_contributions(self):
        svc, _ = make_service()
        report, claims = _people_report("p-1", "V-1", 40)
        svc.receive_report(report, claims)
        (item,) = svc.queue()
        factors = item["explanation"]["factors"]
        self.assertAlmostEqual(sum(f["contribution"] for f in factors), item["score"], places=2)
        self.assertEqual({f["name"] for f in factors},
                         {"affected_people", "facility_criticality",
                          "information_confidence", "report_recency"})

    def test_more_people_ranks_higher(self):
        svc, _ = make_service()
        big, big_claims = _people_report("p-1", "V-1", 90)
        small, small_claims = _people_report("p-2", "V-2", 10)
        svc.receive_report(big, big_claims)
        svc.receive_report(small, small_claims)
        queue = svc.queue()
        self.assertEqual(queue[0]["location_code"], "V-1")
        self.assertGreater(queue[0]["score"], queue[1]["score"])

    def test_contested_claim_halves_confidence_and_flags_verification(self):
        svc, _ = make_service()
        svc.set_source_reliability("巡查组", 0.9)
        svc.set_source_reliability("微信群", 0.2)
        svc.receive_report(make_report(external_id="a", agency="巡查组"),
                           claims=[{"aspect": "road_status", "value": "blocked"}])
        svc.receive_report(make_report(external_id="b", agency="微信群"),
                           claims=[{"aspect": "road_status", "value": "passable"}])
        (item,) = svc.queue()
        confidence = next(f for f in item["explanation"]["factors"]
                          if f["name"] == "information_confidence")
        self.assertTrue(item["needs_verification"])
        self.assertEqual(confidence["dispute_penalty"], 0.5)
        self.assertAlmostEqual(confidence["normalized"], confidence["raw"] * 0.5)

    def test_queue_is_deterministic(self):
        svc, _ = make_service()
        for index, loc in enumerate(("V-1", "V-2", "V-3")):
            report, claims = _people_report(f"p-{index}", loc, 20)
            svc.receive_report(report, claims)
        first = [i["need_id"] for i in svc.queue()]
        second = [i["need_id"] for i in svc.queue()]
        self.assertEqual(first, second)

    def test_resolved_incident_leaves_queue(self):
        svc, _ = make_service()
        report, claims = _people_report("p-1", "V-1", 10)
        svc.receive_report(report, claims)
        self.assertEqual(len(svc.queue()), 1)
        svc.resolve_incident("ev-1", "处置完毕")
        self.assertEqual(svc.queue(), [])

    def test_recency_decays_with_observation_age(self):
        svc, clock = make_service()
        report, claims = _people_report("p-1", "V-1", 10)
        svc.receive_report(report, claims)
        clock.advance(hours=24)
        (item,) = svc.queue()
        recency = next(f for f in item["explanation"]["factors"]
                       if f["name"] == "report_recency")
        self.assertAlmostEqual(recency["normalized"], 1 - 24 / 72, places=3)


if __name__ == "__main__":
    unittest.main()
