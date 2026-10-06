"""并发原子性验证：重复报文与重复派遣在多线程下不得产生重复结果。"""

import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from mountain_response.service import Service
from mountain_response.storage import Conflict
from mountain_response.timeutil import iso

T0 = datetime(2026, 10, 6, 4, tzinfo=timezone.utc)


class ConcurrencyTests(unittest.TestCase):
    def test_concurrent_identical_reports_create_one_canonical(self):
        svc = Service()
        payload = {"event_key": "CC1", "kind": "people", "location_code": "V1",
                   "agency": "应急局", "external_id": "r1", "observed_at": iso(T0),
                   "summary": "30人", "affected_people": 30, "severity": "major"}

        def once(_):
            return svc.ingest(dict(payload))

        with ThreadPoolExecutor(max_workers=16) as pool:
            results = list(pool.map(once, range(32)))
        report_ids = {r["report_id"] for r in results}
        self.assertEqual(report_ids, {1}, "并发重复报文必须只产生一条 canonical 报告")
        self.assertEqual(sum(not r["duplicated"] for r in results), 1)
        self.assertEqual(len(svc.reports("CC1")), 1)
        self.assertEqual(len(svc.raw_messages("CC1")), 32, "32 次到达全部留痕")

    def test_concurrent_dispatch_only_one_succeeds(self):
        svc = Service()
        svc.ingest({"event_key": "CC2", "kind": "people", "location_code": "V1",
                    "agency": "应急局", "external_id": "r1", "observed_at": iso(T0),
                    "summary": "30人", "affected_people": 30, "severity": "major"})

        def send(i):
            try:
                return svc.dispatch({"event_key": "CC2", "unit": f"队{i}",
                                     "target": "V1", "purpose": "人员转移"})
            except Conflict:
                return "conflict"

        with ThreadPoolExecutor(max_workers=16) as pool:
            results = list(pool.map(send, range(16)))
        successes = [r for r in results if r != "conflict"]
        self.assertEqual(len(successes), 1, "并发派遣同目标只能成功一次")
        self.assertEqual(len(svc.chain("CC2")["dispatches"]), 1)


if __name__ == "__main__":
    unittest.main()
