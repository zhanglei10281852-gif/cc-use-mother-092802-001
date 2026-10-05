"""测试公共构造工具。"""

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from mountain_response.clock import ManualClock
from mountain_response.contracts import ImpactReport, ReportKind, SourceReference
from mountain_response.core import CoordinationService
from mountain_response.store import EventStore

UTC = timezone.utc
START = datetime(2026, 10, 5, 4, 0, tzinfo=UTC)


def make_service() -> tuple[CoordinationService, ManualClock]:
    clock = ManualClock(START)
    return CoordinationService(EventStore(), clock), clock


def make_report(
    agency="边境巡查组",
    external_id="obs-1",
    observed_at=None,
    kind=ReportKind.ROAD,
    location="R-4",
    summary="桥面受损",
    people=0,
    event_key="ev-1",
) -> ImpactReport:
    return ImpactReport(
        event_key,
        kind,
        location,
        SourceReference(agency, external_id, observed_at or START),
        summary,
        people,
    )
