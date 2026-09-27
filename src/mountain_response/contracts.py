"""灾情上报与处置所用的数据契约。"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class ReportKind(StrEnum):
    LANDSLIDE = "landslide"
    ROAD = "road"
    WATER = "water"
    PEOPLE = "people"


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
