"""物化视图与当前判断。

StateView 是事件流的投影：可以增量应用事件，也可以从任意历史时刻
（按入库时间 as_of）回放重建，从而区分"当前判断"与"历史依据"。

判断取舍规则（deterministic，可解释）：
1. 只考虑状态为 active 的说法；
2. 可信度（基础可靠度 + 佐证加成）高者优先；
3. 可信度相同，观测时间新者优先；再相同，入库时间新者优先；
4. 若仍有多条相互矛盾的有效说法，则标记 contested（存在争议），
   所有原始说法都随判断一并返回，绝不在视图中删除。
"""

from datetime import datetime, timezone

from .contracts import ClaimStatus, EventType
from .credibility import CredibilityEngine
from .store import StoredEvent

ROAD_EVENT_STATUS = {
    EventType.ROAD_CLOSED: "closed",
    EventType.ROAD_TEMPORARILY_OPENED: "temporarily_open",
    EventType.ROAD_REOPENED: "reopened",
}


def parse_ts(text: str) -> datetime:
    """解析 ISO 时间戳；无时区信息时按 UTC 处理。"""
    instant = datetime.fromisoformat(text)
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=timezone.utc)
    return instant


class StateView:
    """从事件流推导出的物化状态。"""

    def __init__(self) -> None:
        self.reports: dict[str, dict] = {}
        self.report_keys: dict[tuple[str, str], str] = {}
        self.claims: dict[str, dict] = {}
        self.dispatches: dict[str, dict] = {}
        self.road_status: dict[str, dict] = {}
        self.incidents: dict[str, dict] = {}
        self.reliability: dict[str, float] = {}
        self.duplicate_reports: int = 0
        self.deduplicated_dispatch_requests: int = 0

    @classmethod
    def build(cls, events: list[StoredEvent]) -> "StateView":
        view = cls()
        for event in events:
            view.apply(event)
        return view

    def apply(self, event: StoredEvent) -> None:
        etype = event.event_type
        payload = event.payload
        if etype == EventType.REPORT_RECEIVED:
            report = payload["report"]
            self.reports[report["report_id"]] = report
            self.report_keys[(report["agency"], report["external_id"])] = report["report_id"]
            for claim in payload["claims"]:
                self.claims[claim["claim_id"]] = dict(claim)
            self.incidents.setdefault(
                report["event_key"],
                {"resolved": False, "first_reported_at": event.recorded_at.isoformat()},
            )
        elif etype == EventType.DUPLICATE_REPORT_NOTED:
            self.duplicate_reports += 1
        elif etype == EventType.CLAIM_SUPERSEDED:
            old = self.claims[payload["old_claim_id"]]
            old["status"] = ClaimStatus.SUPERSEDED
            old["superseded_by"] = payload["new_claim"]["claim_id"]
            self.claims[payload["new_claim"]["claim_id"]] = dict(payload["new_claim"])
        elif etype == EventType.CLAIM_REVOKED:
            claim = self.claims[payload["claim_id"]]
            claim["status"] = ClaimStatus.REVOKED
            claim["revoke_reason"] = payload["reason"]
        elif etype == EventType.SOURCE_RELIABILITY_SET:
            self.reliability[payload["agency"]] = payload["reliability"]
        elif etype in ROAD_EVENT_STATUS:
            self.road_status[payload["location_code"]] = {
                "status": ROAD_EVENT_STATUS[etype],
                "event_id": event.event_id,
                "at": event.recorded_at.isoformat(),
            }
        elif etype == EventType.DISPATCH_ORDERED:
            self.dispatches[payload["need_id"]] = {
                "need_id": payload["need_id"],
                "dispatch_event_id": event.event_id,
                "event_key": payload["event_key"],
                "location_code": payload["location_code"],
                "need_kind": payload["need_kind"],
                "resources": payload["resources"],
                "ordered_by": payload["ordered_by"],
                "ordered_at": event.recorded_at.isoformat(),
            }
        elif etype == EventType.DISPATCH_REQUEST_DEDUPLICATED:
            self.deduplicated_dispatch_requests += 1
        elif etype == EventType.INCIDENT_RESOLVED:
            incident = self.incidents.setdefault(payload["event_key"], {"resolved": False})
            incident["resolved"] = True
            incident["resolved_at"] = event.recorded_at.isoformat()
        else:
            raise ValueError(f"未知事件类型: {etype}")

    def active_claims(self, location_code: str, aspect: str) -> list[dict]:
        return [
            claim
            for claim in self.claims.values()
            if claim["location_code"] == location_code
            and claim["aspect"] == aspect
            and claim["status"] == ClaimStatus.ACTIVE
        ]

    def locations_for_incident(self, event_key: str) -> set[str]:
        return {
            claim["location_code"]
            for claim in self.claims.values()
            if claim["event_key"] == event_key and claim["status"] == ClaimStatus.ACTIVE
        }


def judge(view: StateView, location_code: str, aspect: str) -> dict | None:
    """对某位置某方面给出当前判断；无有效说法时返回 None。

    返回结构包含取舍理由与全部有效说法（含相互矛盾者），
    供指挥端区分"当前判断"与"各方原始说法"。
    """
    active = view.active_claims(location_code, aspect)
    if not active:
        return None
    engine = CredibilityEngine(view.reliability)

    def corroborators(claim: dict) -> int:
        return len(
            {
                other["agency"]
                for other in active
                if other["claim_id"] != claim["claim_id"] and other["value"] == claim["value"]
            }
        )

    scored = []
    for claim in active:
        credibility = engine.score(claim["agency"], corroborators(claim))
        scored.append({"claim": claim, "credibility": credibility})
    scored.sort(
        key=lambda item: (
            item["credibility"]["score"],
            parse_ts(item["claim"]["observed_at"]),
            parse_ts(item["claim"]["recorded_at"]),
        ),
        reverse=True,
    )
    winner = scored[0]
    contested = len({claim["value"] for claim in active}) > 1
    return {
        "location_code": location_code,
        "aspect": aspect,
        "value": winner["claim"]["value"],
        "winning_claim_id": winner["claim"]["claim_id"],
        "credibility": winner["credibility"],
        "contested": contested,
        "rule": "可信度优先，其次观测时间，其次入库时间；矛盾说法全部保留",
        "claims": scored,
    }
