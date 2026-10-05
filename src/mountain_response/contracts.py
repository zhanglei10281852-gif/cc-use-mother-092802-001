"""灾情上报与处置所用的数据契约。"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class ReportKind(StrEnum):
    LANDSLIDE = "landslide"
    ROAD = "road"
    WATER = "water"
    PEOPLE = "people"


class ClaimAspect(StrEnum):
    """说法所针对的观测方面。"""

    ROAD_STATUS = "road_status"
    WATER_SUPPLY = "water_supply"
    PEOPLE_TRAPPED = "people_trapped"
    AFFECTED_PEOPLE = "affected_people"
    FACILITY_STATUS = "facility_status"


class ClaimStatus(StrEnum):
    """说法的生命周期状态。被取代/被撤销的说法依然保留在台账中。"""

    ACTIVE = "active"
    SUPERSEDED = "superseded"
    REVOKED = "revoked"


class EventType(StrEnum):
    """事件链上的事件类型。"""

    REPORT_RECEIVED = "report_received"
    DUPLICATE_REPORT_NOTED = "duplicate_report_noted"
    CLAIM_SUPERSEDED = "claim_superseded"
    CLAIM_REVOKED = "claim_revoked"
    SOURCE_RELIABILITY_SET = "source_reliability_set"
    ROAD_CLOSED = "road_closed"
    ROAD_TEMPORARILY_OPENED = "road_temporarily_opened"
    ROAD_REOPENED = "road_reopened"
    DISPATCH_ORDERED = "dispatch_ordered"
    DISPATCH_REQUEST_DEDUPLICATED = "dispatch_request_deduplicated"
    INCIDENT_RESOLVED = "incident_resolved"


@dataclass(frozen=True)
class SourceReference:
    agency: str
    external_id: str
    observed_at: datetime


@dataclass(frozen=True)
class ImpactReport:
    event_key: str
    kind: ReportKind
    location_code: str
    source: SourceReference
    summary: str
    affected_people: int = 0
