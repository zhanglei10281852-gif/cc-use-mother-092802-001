"""灾情上报与处置所用的数据契约（仅类型，不含业务逻辑）。"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class ReportKind(StrEnum):
    LANDSLIDE = "landslide"  # 冰崩/滑坡等地质灾害
    ROAD = "road"            # 道路通行
    WATER = "water"          # 供水设施
    PEOPLE = "people"        # 人员受影响


class ClaimStatus(StrEnum):
    """单条原始说法在归并后的状态。原始行永不删除。"""

    ACTIVE = "active"                # 仍作为当前判断依据
    SUPERSEDED = "superseded"        # 被同一来源的后续更正替代
    RETRACTED = "retracted"          # 被来源撤销
    DUPLICATE = "duplicate"          # 重复/转发报文，归并到首条


class TriageLevel(StrEnum):
    P1 = "P1"  # 立即处置
    P2 = "P2"  # 优先处置
    P3 = "P3"  # 计划处置
    P4 = "P4"  # 观察核实


class RoadAction(StrEnum):
    CLOSE = "road.close"                     # 封闭
    REOPEN = "road.reopen"                   # 恢复开放
    TEMPORARY_OPEN = "road.temporary_open"   # 临时开放（有时间窗）


class FacilityAction(StrEnum):
    OUTAGE = "facility.outage"        # 设施中断
    RESTORED = "facility.restored"    # 设施恢复


class DispatchAction(StrEnum):
    CREATE = "dispatch.create"        # 调派
    ARRIVE = "dispatch.arrive"        # 到达
    REDEPLOY = "dispatch.redeploy"    # 转用至新目标
    STAND_DOWN = "dispatch.standdown"  # 收队/解除


class EventAction(StrEnum):
    RESOLVE = "event.resolve"
    REOPEN = "event.reopen"


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
    # 观测者自述的严重程度：info / minor / major / critical
    severity: str | None = None
    # 道路类报告
    road_code: str | None = None
    road_state: str | None = None  # closed / open
    # 供水设施类报告
    facility: str | None = None
    facility_state: str | None = None  # outage / restored
    # 该报文更正的是本来源此前哪条 external_id
    corrects: str | None = None
    # 调用方提供的幂等键（如转发消息哈希）
    dedupe_key: str | None = None
