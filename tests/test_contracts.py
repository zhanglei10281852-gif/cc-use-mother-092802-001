import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
from mountain_response.contracts import ImpactReport, ReportKind, SourceReference


class ContractTests(unittest.TestCase):
    def test_report_keeps_source_observation_time(self):
        instant = datetime(2026, 9, 27, 4, tzinfo=timezone.utc)
        source = SourceReference("边境巡查组", "obs-17", instant)
        report = ImpactReport("ev-1", ReportKind.ROAD, "R-4", source, "桥面受损", 12)
        self.assertEqual(report.source.observed_at, instant)
        self.assertEqual(report.affected_people, 12)

    def test_report_kind_is_explicit(self):
        self.assertEqual(ReportKind.WATER.value, "water")


if __name__ == "__main__":
    unittest.main()
