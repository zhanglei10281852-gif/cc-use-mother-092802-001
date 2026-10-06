import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from mountain_response.service import Service
from mountain_response.timeutil import iso

T0 = datetime(2026, 9, 27, 4, 0, tzinfo=timezone.utc)


def people_report(event, agency, external_id, minutes, people, severity="major",
                  location="V1", corrects=None, retracts=None, dedupe_key=None):
    return {
        "event_key": event, "kind": "people", "location_code": location,
        "agency": agency, "external_id": external_id,
        "observed_at": iso(T0 + timedelta(minutes=minutes)),
        "summary": f"{agency} 报 {people} 人",
        "affected_people": people, "severity": severity,
        "corrects": corrects, "retracts": retracts, "dedupe_key": dedupe_key,
    }


class IngestTests(unittest.TestCase):
    def setUp(self):
        self.svc = Service()

    def test_exact_forward_is_duplicate_and_archived(self):
        payload = people_report("EV-1", "边民甲", "wx-1", 0, 60)
        first = self.svc.ingest(payload)
        again = self.svc.ingest(dict(payload))  # 群里二次转发，原文一致
        third = self.svc.ingest({**payload, "summary": payload["summary"]})

        self.assertFalse(first["duplicated"])
        self.assertTrue(again["duplicated"])
        self.assertTrue(third["duplicated"])
        self.assertEqual(again["report_id"], first["report_id"])

        raws = self.svc.raw_messages("EV-1")
        self.assertEqual(len(raws), 3, "每次到达都要留痕，包括转发")
        self.assertIsNotNone(raws[1]["duplicate_of_raw"])
        self.assertEqual(len(self.svc.reports("EV-1")), 1, "重复报文不得产生第二条 canonical 报告")

    def test_explicit_dedupe_key_dedupes_across_wording(self):
        a = people_report("EV-2", "应急局", "of-1", 0, 10, dedupe_key="flash-777")
        b = people_report("EV-2", "应急局", "of-1", 1, 10, dedupe_key="flash-777")
        b["summary"] = "措辞不同但是同一条快讯"
        self.svc.ingest(a)
        self.assertTrue(self.svc.ingest(b)["duplicated"])

    def test_retraction_keeps_original_but_removes_from_current_view(self):
        rid = self.svc.ingest(people_report("EV-3", "边民甲", "wx-9", 0, 120))["report_id"]
        # 队列先采信该说法
        q = self.svc.assessment("EV-3")["queue"]
        self.assertEqual(q[0]["people_estimate"], 120)

        ret = people_report("EV-3", "边民甲", "wx-9r", 30, 0, severity="info",
                            retracts="wx-9")
        ret["summary"] = "此前视频系去年旧影像，现撤销"
        outcome = self.svc.ingest(ret)
        self.assertEqual(outcome["retracted_report_id"], rid)

        reports = self.svc.reports("EV-3")
        self.assertEqual(reports[0]["status"], "retracted", "原始说法必须保留且标注 retracted")
        self.assertEqual(reports[1]["status"], "active")

        snap = self.svc.assessment("EV-3")
        # 撤销后当前判断不再按 120 人准备
        self.assertEqual(snap["totals"]["affected_people_upper_bound"], 0)
        item = snap["queue"][0]
        self.assertEqual(item["people_estimate"], 0)
        # 但历史依据仍可查
        self.assertEqual(len(item["superseded_history"]), 1)
        self.assertEqual(item["superseded_history"][0]["status"], "retracted")

    def test_correction_supersedes_same_source_claim(self):
        self.svc.ingest(people_report("EV-4", "巡查组", "obs-1", 0, 120))
        out = self.svc.ingest(
            people_report("EV-4", "巡查组", "obs-2", 20, 30, corrects="obs-1"))
        self.assertEqual(out["superseded_report_id"], 1)
        snap = self.svc.assessment("EV-4")
        self.assertEqual(snap["queue"][0]["people_estimate"], 30)
        statuses = {r["id"]: r["status"] for r in self.svc.reports("EV-4")}
        self.assertEqual(statuses, {1: "superseded", 2: "active"})

    def test_late_correction_pointing_missing_claim_is_held_not_dropped(self):
        out = self.svc.ingest(
            people_report("EV-5", "巡查组", "obs-late", 90, 5, corrects="obs-ghost"))
        self.assertIsNotNone(out["target_missing"])
        report = self.svc.reports("EV-5")[0]
        self.assertIn("pending-correct:obs-ghost", report["note"])
        self.assertEqual(report["status"], "active")

    def test_new_report_after_resolution_reopens_but_keeps_resolution(self):
        self.svc.ingest(people_report("EV-6", "巡查组", "obs-1", 0, 0, severity="info"))
        resolved = self.svc.resolve("EV-6", reason="现场核实无灾情")
        self.assertEqual(self.svc.event("EV-6")["status"], "resolved")

        out = self.svc.ingest(people_report("EV-6", "巡查组", "obs-2", 120, 80))
        self.assertTrue(out["event_reopened"])
        self.assertEqual(self.svc.event("EV-6")["status"], "active")

        actions = [d["action"] for d in self.svc.chain("EV-6")["timeline"]]
        self.assertEqual(actions, ["event.resolve", "event.reopen"],
                         "解除与重开都必须留在链上")
        # resolve 时的依据快照不动
        resolve_decision = self.svc.chain("EV-6")["timeline"][0]
        self.assertEqual(resolve_decision["id"], resolved["decision"]["id"])


if __name__ == "__main__":
    unittest.main()
