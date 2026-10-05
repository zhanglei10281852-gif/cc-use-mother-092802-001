"""协同核心：接报、更正、撤销、队列、派遣、道路事件、解除。

所有写操作都表现为向事件存储追加事件，再同步到物化视图；
任何历史状态都可以通过按入库时间回放事件流重建。
"""

import hashlib
import threading
from datetime import datetime

from .clock import Clock, SystemClock
from .contracts import ClaimAspect, ClaimStatus, EventType, ImpactReport
from .queueing import build_queue
from .state import StateView, judge
from .store import EventStore


class ServiceError(Exception):
    """业务错误，携带 HTTP 状态码供接口层映射。"""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _require_tz(instant: datetime, field: str) -> None:
    if instant.tzinfo is None:
        raise ServiceError(400, f"{field} 必须携带时区信息")


class CoordinationService:
    """值班人员调用的协同服务入口。"""

    def __init__(self, store: EventStore, clock: Clock | None = None):
        self.store = store
        self.clock = clock or SystemClock()
        self._lock = threading.RLock()
        self._view = StateView.build(store.all())

    # ------------------------------------------------------------------ 内部

    def _append(self, event_type: str, payload: dict, at: datetime):
        event = self.store.append(event_type, at, payload)
        self._view.apply(event)
        return event

    # ------------------------------------------------------------------ 接报

    def receive_report(self, report: ImpactReport, claims: list[dict] | None = None) -> dict:
        """接收影响报告。同一来源同一外部编号重复报送时幂等去重：
        不产生新说法、不改变队列、不触发派遣，仅留一条审计事件。"""
        _require_tz(report.source.observed_at, "observed_at")
        if report.affected_people < 0:
            raise ServiceError(400, "affected_people 不能为负")
        claims = list(claims or [])
        for claim in claims:
            if "aspect" not in claim or "value" not in claim:
                raise ServiceError(400, "每条说法必须包含 aspect 与 value")
        with self._lock:
            key = (report.source.agency, report.source.external_id)
            if key in self._view.report_keys:
                existing_id = self._view.report_keys[key]
                self._append(
                    EventType.DUPLICATE_REPORT_NOTED,
                    {
                        "agency": key[0],
                        "external_id": key[1],
                        "existing_report_id": existing_id,
                        "note": "重复报文已忽略：不产生新说法，不触发派遣",
                    },
                    self.clock.now(),
                )
                return {"deduplicated": True, "report_id": existing_id, "claim_ids": []}

            now = self.clock.now()
            if report.affected_people > 0 and not any(
                c["aspect"] == ClaimAspect.AFFECTED_PEOPLE for c in claims
            ):
                claims.append(
                    {"aspect": ClaimAspect.AFFECTED_PEOPLE, "value": str(report.affected_people)}
                )
            report_id = "rpt-" + hashlib.sha1(
                f"{report.source.agency}|{report.source.external_id}".encode("utf-8")
            ).hexdigest()[:12]
            seq = self.store.next_seq()
            claim_dicts = [
                self._new_claim(
                    claim_id=f"clm-{seq:06d}-{index}",
                    report_id=report_id,
                    report=report,
                    aspect=str(claim["aspect"]),
                    value=str(claim["value"]),
                    summary=claim.get("summary", report.summary),
                    observed_at=report.source.observed_at,
                    recorded_at=now,
                )
                for index, claim in enumerate(claims)
            ]
            report_dict = {
                "report_id": report_id,
                "event_key": report.event_key,
                "kind": str(report.kind),
                "location_code": report.location_code,
                "agency": report.source.agency,
                "external_id": report.source.external_id,
                "observed_at": report.source.observed_at.isoformat(),
                "recorded_at": now.isoformat(),
                "summary": report.summary,
                "affected_people": report.affected_people,
            }
            self._append(
                EventType.REPORT_RECEIVED, {"report": report_dict, "claims": claim_dicts}, now
            )
            return {
                "deduplicated": False,
                "report_id": report_id,
                "claim_ids": [c["claim_id"] for c in claim_dicts],
            }

    @staticmethod
    def _new_claim(
        claim_id: str,
        report_id: str,
        report: ImpactReport,
        aspect: str,
        value: str,
        summary: str,
        observed_at: datetime,
        recorded_at: datetime,
    ) -> dict:
        return {
            "claim_id": claim_id,
            "report_id": report_id,
            "event_key": report.event_key,
            "location_code": report.location_code,
            "aspect": aspect,
            "value": value,
            "summary": summary,
            "agency": report.source.agency,
            "observed_at": observed_at.isoformat(),
            "recorded_at": recorded_at.isoformat(),
            "status": ClaimStatus.ACTIVE,
            "corrects": None,
        }

    # ------------------------------------------------------------------ 复核

    def correct_claim(
        self,
        claim_id: str,
        new_value: str,
        reason: str,
        agency: str | None = None,
        observed_at: datetime | None = None,
    ) -> dict:
        """更正说法：生成一条新说法取代旧说法。旧说法保留，状态变为 superseded。"""
        with self._lock:
            old = self._view.claims.get(claim_id)
            if old is None:
                raise ServiceError(404, f"说法不存在: {claim_id}")
            if old["status"] != ClaimStatus.ACTIVE:
                raise ServiceError(409, f"只能更正有效说法，当前状态: {old['status']}")
            now = self.clock.now()
            observed = observed_at or now
            _require_tz(observed, "observed_at")
            new_claim = {
                **old,
                "claim_id": f"clm-{self.store.next_seq():06d}-0",
                "value": str(new_value),
                "summary": f"更正自 {claim_id}：{reason}",
                "agency": agency or old["agency"],
                "observed_at": observed.isoformat(),
                "recorded_at": now.isoformat(),
                "status": ClaimStatus.ACTIVE,
                "corrects": claim_id,
            }
            self._append(
                EventType.CLAIM_SUPERSEDED,
                {"old_claim_id": claim_id, "new_claim": new_claim, "reason": reason},
                now,
            )
            return {"superseded_claim_id": claim_id, "new_claim_id": new_claim["claim_id"]}

    def revoke_claim(self, claim_id: str, reason: str) -> dict:
        """撤销说法：当前判断不再采用它，但说法与撤销行为都留在链上。"""
        with self._lock:
            claim = self._view.claims.get(claim_id)
            if claim is None:
                raise ServiceError(404, f"说法不存在: {claim_id}")
            if claim["status"] != ClaimStatus.ACTIVE:
                raise ServiceError(409, f"只能撤销有效说法，当前状态: {claim['status']}")
            self._append(
                EventType.CLAIM_REVOKED, {"claim_id": claim_id, "reason": reason}, self.clock.now()
            )
            return {"revoked_claim_id": claim_id}

    def set_source_reliability(self, agency: str, reliability: float, note: str = "") -> dict:
        if not 0.0 <= reliability <= 1.0:
            raise ServiceError(400, "reliability 必须在 [0, 1] 区间")
        with self._lock:
            self._append(
                EventType.SOURCE_RELIABILITY_SET,
                {"agency": agency, "reliability": reliability, "note": note},
                self.clock.now(),
            )
            return {"agency": agency, "reliability": reliability}

    # ------------------------------------------------------------------ 队列

    def queue(self) -> list[dict]:
        with self._lock:
            return build_queue(self._view, self.clock.now())

    # ------------------------------------------------------------------ 派遣

    def order_dispatch(self, need_id: str, resources: list[str], note: str, ordered_by: str) -> dict:
        """下达调派。按需求编号幂等：同一需求已有在办调派时，
        返回既有调派并留一条审计事件，绝不重复派遣。"""
        with self._lock:
            existing = self._view.dispatches.get(need_id)
            if existing is not None:
                self._append(
                    EventType.DISPATCH_REQUEST_DEDUPLICATED,
                    {
                        "need_id": need_id,
                        "existing_dispatch_event_id": existing["dispatch_event_id"],
                        "note": "该需求已有在办调派，重复请求已忽略",
                    },
                    self.clock.now(),
                )
                return {"deduplicated": True, "dispatch": existing}
            item = next((q for q in self.queue() if q["need_id"] == need_id), None)
            if item is None:
                raise ServiceError(409, f"需求不在当前处置队列（不存在或已解除）: {need_id}")
            now = self.clock.now()
            event = self._append(
                EventType.DISPATCH_ORDERED,
                {
                    "need_id": need_id,
                    "event_key": item["event_key"],
                    "location_code": item["location_code"],
                    "need_kind": item["need_kind"],
                    "resources": list(resources),
                    "note": note,
                    "ordered_by": ordered_by,
                    "basis": {
                        "claim_ids": item["explanation"]["basis_claim_ids"],
                        "score_snapshot": item,
                    },
                },
                now,
            )
            return {
                "deduplicated": False,
                "dispatch": self._view.dispatches[need_id] | {"dispatch_event_id": event.event_id},
            }

    # ------------------------------------------------------------------ 道路

    def record_road_event(self, action: str, location_code: str, note: str, operator: str) -> dict:
        """记录道路封闭 / 临时开放 / 恢复通行，事件携带当时的判断依据快照。"""
        mapping = {
            "close": EventType.ROAD_CLOSED,
            "temp-open": EventType.ROAD_TEMPORARILY_OPENED,
            "reopen": EventType.ROAD_REOPENED,
        }
        if action not in mapping:
            raise ServiceError(400, f"不支持的道路操作: {action}")
        with self._lock:
            judgment = judge(self._view, location_code, ClaimAspect.ROAD_STATUS)
            basis = None
            if judgment is not None:
                basis = {
                    "judgment_value": judgment["value"],
                    "contested": judgment["contested"],
                    "claim_ids": [c["claim"]["claim_id"] for c in judgment["claims"]],
                }
            event = self._append(
                mapping[action],
                {
                    "location_code": location_code,
                    "note": note,
                    "operator": operator,
                    "basis": basis,
                },
                self.clock.now(),
            )
            return {"event_id": event.event_id, "road": self._view.road_status[location_code]}

    # ------------------------------------------------------------------ 解除

    def resolve_incident(self, event_key: str, note: str) -> dict:
        with self._lock:
            incident = self._view.incidents.get(event_key)
            if incident is None:
                raise ServiceError(404, f"事件不存在: {event_key}")
            if incident.get("resolved"):
                raise ServiceError(409, f"事件已解除: {event_key}")
            self._append(
                EventType.INCIDENT_RESOLVED, {"event_key": event_key, "note": note}, self.clock.now()
            )
            return {"event_key": event_key, "resolved": True}

    # ------------------------------------------------------------------ 查询

    def get_report(self, report_id: str) -> dict:
        report = self._view.reports.get(report_id)
        if report is None:
            raise ServiceError(404, f"报告不存在: {report_id}")
        claims = [c for c in self._view.claims.values() if c["report_id"] == report_id]
        return {"report": report, "claims": claims}

    def claims_for_location(self, location_code: str) -> list[dict]:
        """该位置的全部说法，含已被取代与已被撤销的原始记录。"""
        with self._lock:
            claims = [c for c in self._view.claims.values() if c["location_code"] == location_code]
            claims.sort(key=lambda c: (c["recorded_at"], c["claim_id"]))
            return claims

    def judgment_for_location(self, location_code: str) -> dict:
        with self._lock:
            aspects = {
                c["aspect"]
                for c in self._view.claims.values()
                if c["location_code"] == location_code
            }
            judgments = {}
            for aspect in sorted(aspects):
                result = judge(self._view, location_code, aspect)
                if result is not None:
                    judgments[aspect] = result
            return {
                "location_code": location_code,
                "judgments": judgments,
                "road_status": self._view.road_status.get(location_code),
            }

    def state(self, as_of: datetime | None = None) -> dict:
        """当前状态，或 as_of 指定的历史时刻状态（按入库时间回放）。"""
        with self._lock:
            now = self.clock.now()
            if as_of is None:
                view, moment = self._view, now
            else:
                _require_tz(as_of, "as_of")
                view, moment = StateView.build(self.store.until(as_of)), as_of
            locations = {}
            for claim in view.claims.values():
                locations.setdefault(claim["location_code"], set()).add(claim["aspect"])
            return {
                "as_of": moment.isoformat(),
                "generated_at": now.isoformat(),
                # 显式指定 as_of 即为历史回放视图，无论该时刻是否就是现在
                "is_historical": as_of is not None,
                "incidents": view.incidents,
                "locations": {
                    code: {
                        "judgments": {
                            aspect: j
                            for aspect in sorted(aspects)
                            if (j := judge(view, code, aspect)) is not None
                        },
                        "road_status": view.road_status.get(code),
                    }
                    for code, aspects in sorted(locations.items())
                },
                "queue": build_queue(view, moment),
                "dispatches": list(view.dispatches.values()),
                "source_reliability": view.reliability,
                "audit": {
                    "duplicate_reports_ignored": view.duplicate_reports,
                    "dispatch_requests_deduplicated": view.deduplicated_dispatch_requests,
                },
            }

    def events(self, event_type: str | None = None, since: datetime | None = None) -> list[dict]:
        with self._lock:
            result = []
            for event in self.store.all():
                if event_type and event.event_type != event_type:
                    continue
                if since and event.recorded_at <= since:
                    continue
                result.append(event.to_dict())
            return result

    def verify_chain(self) -> dict:
        ok, detail = self.store.verify()
        return {"ok": ok, "detail": detail}
