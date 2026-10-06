"""接报：幂等去重、更正与撤销。

- 每条到达的报文（含转发）都进入 raw_messages，永不丢弃。
- identity_key 相同（来源+外部编号，或显式 dedupe_key/原文哈希）的报文视为重复：
  不产生新的 canonical report，因而不会触发第二次派遣。
- corrects / retracts 只改变旧说法的状态，绝不删除旧行；旧行始终可追溯。
- 归并临界区在 Store 锁内原子完成，避免并发接报产生重复 canonical 报告。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from .storage import Store
from .timeutil import iso, now_utc, parse_iso

VALID_SEVERITIES = {"info", "minor", "major", "critical"}
VALID_ROAD_STATES = {"closed", "open"}
VALID_FACILITY_STATES = {"outage", "restored"}


class IngestError(ValueError):
    pass


@dataclass
class IngestResult:
    canonical_report_id: int
    duplicated: bool = False
    duplicate_raw_id: int | None = None
    retracted_report_id: int | None = None
    superseded_report_id: int | None = None
    target_missing: dict[str, Any] | None = None  # 更正/撤销指向的旧文未找到
    event_reopened: bool = False
    raw: dict[str, Any] = field(default_factory=dict)


def _identity_key(payload: dict[str, Any]) -> str:
    if payload.get("dedupe_key"):
        return f"dk:{payload['dedupe_key']}"
    if payload.get("agency") and payload.get("external_id"):
        return f"src:{payload['agency']}|{payload['external_id']}"
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return "h:" + hashlib.sha256(body.encode("utf-8")).hexdigest()[:32]


def _validate(payload: dict[str, Any]) -> dict[str, Any]:
    required = ["event_key", "kind", "location_code", "agency", "observed_at", "summary"]
    missing = [k for k in required if not payload.get(k)]
    if missing:
        raise IngestError(f"缺少必填字段: {', '.join(missing)}")
    kind = payload["kind"]
    data = dict(payload)
    try:
        data["observed_dt"] = parse_iso(data["observed_at"])
    except (ValueError, TypeError) as exc:
        raise IngestError(f"observed_at 无法解析: {data['observed_at']}") from exc
    sev = data.get("severity") or "info"
    if sev not in VALID_SEVERITIES:
        raise IngestError(f"severity 非法: {sev}（可选 {sorted(VALID_SEVERITIES)}）")
    data["severity"] = sev
    if data.get("affected_people") is None:
        data["affected_people"] = 0
    try:
        data["affected_people"] = int(data["affected_people"])
    except (TypeError, ValueError) as exc:
        raise IngestError("affected_people 必须为非负整数") from exc
    if data["affected_people"] < 0:
        raise IngestError("affected_people 必须为非负整数")
    if kind == "road":
        if not data.get("road_code"):
            raise IngestError("road 类报告必须提供 road_code")
        if data.get("road_state") not in VALID_ROAD_STATES:
            raise IngestError("road 类报告必须提供 road_state: closed/open")
    if kind == "water":
        if not data.get("facility"):
            raise IngestError("water 类报告必须提供 facility")
        if data.get("facility_state") not in VALID_FACILITY_STATES:
            raise IngestError("water 类报告必须提供 facility_state: outage/restored")
    data.setdefault("external_id", None)
    if not data["external_id"]:
        # 无外部编号时用原文哈希，保证同一转发再次到达仍可幂等
        data["external_id"] = "auto-" + hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest()[:16]
    data["retracts"] = data.get("retracts")
    data["corrects"] = data.get("corrects")
    return data


def ingest_report(store: Store, payload: dict[str, Any]) -> IngestResult:
    """校验为纯计算；归并段在 Store 锁内原子完成。"""
    data = _validate(payload)
    identity = _identity_key(payload)
    with store._lock:
        event = store.ensure_event(data["event_key"])
        reopened = False
        if event["status"] == "resolved":
            # 解除后又有新消息：重新开启事件，但解除记录保留
            at = iso(now_utc())
            store.reopen_event(data["event_key"], at)
            store.add_decision(
                data["event_key"], "event", data["event_key"], "event.reopen", "system",
                basis={"trigger": "new_report", "identity_key": identity,
                       "agency": data["agency"],
                       "observed_at": iso(data["observed_dt"])},
                reason="解除后收到新报告，事件重新开启；此前解除决策保留可查",
                created_at=at,
            )
            reopened = True
        return _merge_report(store, data, payload, identity, reopened)


def _merge_report(store: Store, data: dict[str, Any], payload: dict[str, Any],
                  identity: str, reopened: bool) -> IngestResult:
    event_key = data["event_key"]
    existing = store.find_report_by_identity(event_key, identity)
    if existing is not None:
        raw_id = store.record_duplicate_raw(
            event_key, identity, existing["id"], payload
        )
        return IngestResult(
            canonical_report_id=existing["id"],
            duplicated=True,
            duplicate_raw_id=raw_id,
            event_reopened=reopened,
            raw={"identity_key": identity, "status": existing["status"]},
        )

    fields = {
        "event_key": event_key,
        "received_at": iso(now_utc()),
        "kind": data["kind"],
        "location_code": data["location_code"],
        "agency": data["agency"],
        "external_id": data["external_id"],
        "observed_at": iso(data["observed_dt"]),
        "summary": data["summary"],
        "affected_people": data["affected_people"],
        "severity": data["severity"],
        "road_code": data.get("road_code"),
        "road_state": data.get("road_state"),
        "facility": data.get("facility"),
        "facility_state": data.get("facility_state"),
        "corrects": data["corrects"],
        "retracts": data["retracts"],
        "identity_key": identity,
    }
    row = store.insert_report(fields, identity, payload)
    result = IngestResult(canonical_report_id=row["id"], event_reopened=reopened,
                          raw={"identity_key": identity})

    target_ref = data["retracts"] or data["corrects"]
    if target_ref:
        target = store.find_report_by_external(event_key, data["agency"], target_ref)
        if target is None:
            # 迟到的更正/撤销：挂起记录在 note，旧说法暂维持；原报文不丢
            result.target_missing = {"agency": data["agency"], "external_id": target_ref}
            store._conn.execute(
                "UPDATE reports SET note=COALESCE(note,'')||? WHERE id=?",
                (f"pending-{ 'retract' if data['retracts'] else 'correct' }:{target_ref};",
                 row["id"]),
            )
        elif data["retracts"]:
            store.mark_retracted(
                target["id"], row["id"],
                f"被 {data['agency']} 的 {data['external_id']} 撤销",
            )
            result.retracted_report_id = target["id"]
        else:
            store.mark_superseded(
                target["id"], row["id"],
                f"被 {data['agency']} 的 {data['external_id']} 更正",
            )
            result.superseded_report_id = target["id"]

    return result
