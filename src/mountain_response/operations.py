"""处置动作：道路封闭/开放、临时开放、力量调派、事件解除。

每条决策只追加、不可变；basis_json 固化决策时刻看到的态势快照。
- 道路：重复封闭是幂等 no-op；临时开放必须给出结束时刻，过期自动失效。
- 调派：同目标+同任务且已有在途力量时拒绝重复派遣（幂等键亦可防重）。
- 解除：固化最终队列；解除后新报告自动重开事件，但解除决策仍在链上。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .assessment import build_assessment
from .storage import Conflict, NotFound, Store
from .timeutil import iso, now_utc, parse_iso


class OperationError(ValueError):
    pass


def _require_event(store: Store, event_key: str) -> dict[str, Any]:
    event = store.get_event(event_key)
    if event is None:
        raise NotFound(f"事件不存在: {event_key}")
    return event


def _snapshot_at(store: Store, event_key: str, as_of) -> dict[str, Any]:
    snap = build_assessment(store, event_key, as_of)
    # 队列证据较长，basis 中保留摘要与完整队列入口（可通过事件接口回看）
    return {
        "as_of": snap["as_of"],
        "event_status": snap["event_status"],
        "totals": snap["totals"],
        "queue_summary": [
            {"location_code": q["location_code"], "level": q["level"],
             "score": q["score"], "confidence": q["confidence"],
             "headline": q["headline"]}
            for q in snap["queue"]
        ],
    }


def _require_active_event(event: dict[str, Any], action: str) -> None:
    if event["status"] != "active":
        raise OperationError(
            f"事件已解除，不能执行 {action}；新报告会自动重开事件后再处置"
        )


def road_decision(
    store: Store,
    event_key: str,
    road_code: str,
    action: str,
    actor: str,
    reason: str | None = None,
    end_time: str | None = None,
    report_id: int | None = None,
    at: str | None = None,
) -> dict[str, Any]:
    decision_at = parse_iso(at) if at else now_utc()
    if action not in {"road.close", "road.reopen", "road.temporary_open"}:
        raise OperationError(f"非法道路动作: {action}")
    if action == "road.temporary_open":
        if not end_time:
            raise OperationError("临时开放必须提供 end_time")
        end_dt = parse_iso(end_time)
        if end_dt <= decision_at:
            raise OperationError("临时开放结束时刻必须晚于决策生效时刻")
        end_time = iso(end_dt)
    else:
        end_time = None

    with store._lock:
        event = store.ensure_event(event_key)
        _require_active_event(event, action)
        latest = store.latest_decision("road", road_code)
        # 幂等：同一道路同一动作（带相同结束时刻）直接返回既有决策，不产生新链节
        if latest and latest["event_key"] == event_key and latest["action"] == action:
            if action != "road.temporary_open" or latest["end_time"] == end_time:
                return {"decision": latest, "idempotent": True}

        basis = _snapshot_at(store, event_key, decision_at)
        basis["trigger_report_id"] = report_id
        decision = store.add_decision(
            event_key, "road", road_code, action, actor,
            basis=basis, reason=reason, end_time=end_time, report_id=report_id,
            deactivate_previous=True, created_at=iso(decision_at),
        )
    return {"decision": decision, "idempotent": False}


def dispatch_unit(
    store: Store,
    event_key: str,
    unit: str,
    target: str | None = None,
    purpose: str | None = None,
    actor: str = "dispatcher",
    reason: str | None = None,
    report_id: int | None = None,
    dispatch_code: str | None = None,
    at: str | None = None,
) -> dict[str, Any]:
    """调派一支力量。同一目标+任务已有在途/在场力量时拒绝，防止重复派遣。"""
    if not unit or not unit.strip():
        raise OperationError("unit 不能为空")
    decision_dt = parse_iso(at) if at else now_utc()
    decision_ts = iso(decision_dt)

    with store._lock:
        event = _require_event(store, event_key)
        _require_active_event(event, "dispatch.create")
        existing = store.active_dispatch_for_target(event_key, target, purpose)
        if existing is not None:
            raise Conflict(
                f"目标 {target}（任务 {purpose or '综合救援'}）已有在途力量 "
                f"{existing['unit']}（{existing['dispatch_code']}，状态 {existing['status']}）；"
                "如须增援请使用不同 purpose，或先转用/收队"
            )

        if dispatch_code is None:
            seq = store._conn.execute(
                "SELECT COALESCE(COUNT(*),0)+1 FROM dispatches WHERE event_key=?",
                (event_key,)).fetchone()[0]
            dispatch_code = f"D-{event_key}-{seq:03d}"
        basis = _snapshot_at(store, event_key, decision_dt)
        basis.update({"unit": unit, "target": target, "purpose": purpose,
                      "trigger_report_id": report_id})
        decision = store.add_decision(
            event_key, "dispatch", dispatch_code, "dispatch.create", actor,
            basis=basis, reason=reason, report_id=report_id,
            metadata={"unit": unit, "target": target, "purpose": purpose},
            created_at=decision_ts,
        )
        dispatch = store.insert_dispatch(
            event_key, decision["id"], dispatch_code, unit, target, purpose,
            decision["created_at"]
        )
    return {"decision": decision, "dispatch": dispatch, "idempotent": False}


def _get_dispatch(store: Store, event_key: str, code: str) -> dict[str, Any]:
    row = store.get_dispatch_by_code(event_key, code)
    if row is None:
        raise NotFound(f"调派不存在: {code}")
    return row


def _dispatch_lifecycle(
    store: Store, event_key: str, code: str, action: str, actor: str,
    new_status: str, reason: str | None, new_target: str | None = None,
    at: str | None = None,
) -> dict[str, Any]:
    decision_dt = parse_iso(at) if at else now_utc()
    with store._lock:
        event = _require_event(store, event_key)
        dispatch = _get_dispatch(store, event_key, code)
        if dispatch["status"] == "stood_down":
            raise OperationError(f"{code} 已收队，收队决策不可撤销（可重新调派形成新链节）")
        basis = _snapshot_at(store, event_key, decision_dt)
        basis.update({"dispatch_code": code, "from_status": dispatch["status"],
                      "new_target": new_target})
        decision = store.add_decision(
            event_key, "dispatch", code, action, actor,
            basis=basis, reason=reason,
            created_at=iso(decision_dt),
            metadata={"unit": dispatch["unit"], "target": new_target or dispatch["target"],
                      "purpose": dispatch["purpose"]},
        )
        store.update_dispatch_status(dispatch["id"], new_status, new_target)
        updated = store.get_dispatch(dispatch["id"])
    return {"decision": decision, "dispatch": updated}


def mark_arrived(store: Store, event_key: str, code: str, actor: str = "field",
                 reason: str | None = None, at: str | None = None) -> dict[str, Any]:
    return _dispatch_lifecycle(
        store, event_key, code, "dispatch.arrive", actor, "arrived", reason, at=at)


def redeploy(store: Store, event_key: str, code: str, new_target: str,
             actor: str = "dispatcher", reason: str | None = None,
             at: str | None = None) -> dict[str, Any]:
    if not new_target:
        raise OperationError("转用必须提供 new_target")
    return _dispatch_lifecycle(
        store, event_key, code, "dispatch.redeploy", actor, "redeployed",
        reason, new_target=new_target, at=at)


def stand_down(store: Store, event_key: str, code: str, actor: str = "dispatcher",
               reason: str | None = None, at: str | None = None) -> dict[str, Any]:
    return _dispatch_lifecycle(
        store, event_key, code, "dispatch.standdown", actor, "stood_down",
        reason, at=at)


def resolve_event(store: Store, event_key: str, actor: str = "commander",
                  reason: str | None = None, at: str | None = None) -> dict[str, Any]:
    """解除事件。要求队列中已无 P1/P2 未处置项（可被 force 绕过但会留痕）。"""
    event = _require_event(store, event_key)
    if event["status"] == "resolved":
        latest = store.list_decisions(event_key, "event", event_key)
        return {"decision": latest[-1], "idempotent": True, "assessment": None}

    as_of = parse_iso(at) if at else None
    with store._lock:
        snap = build_assessment(store, event_key, as_of)
        urgent = [q for q in snap["queue"] if q["level"] in ("P1", "P2")]
        active_dispatches = [
            d for d in store.list_dispatches(event_key)
            if d["status"] != "stood_down"
        ]
        if urgent:
            raise OperationError(
                "仍有高优先级处置项，不能解除：" +
                "；".join(f"{q['location_code']} {q['level']}({q['headline']})" for q in urgent) +
                "。确认实际已解除时可用 force=true，并在理由中说明。"
            )
        decision_ts = iso(as_of) if as_of else None
        basis = {
            "final_snapshot": _snapshot_at(store, event_key, as_of),
            "standing_dispatches": [
                {"code": d["dispatch_code"], "unit": d["unit"], "status": d["status"]}
                for d in active_dispatches
            ],
        }
        decision = store.add_decision(
            event_key, "event", event_key, "event.resolve", actor,
            basis=basis, reason=reason, created_at=decision_ts,
        )
        store.resolve_event(event_key, decision["id"], decision["created_at"])
    return {"decision": decision, "idempotent": False,
            "assessment": snap}


def force_resolve_event(store: Store, event_key: str, actor: str, reason: str,
                        at: str | None = None) -> dict[str, Any]:
    """强制解除：仍固化快照，并在决策上明确标注绕过原因。"""
    _require_event(store, event_key)
    as_of = parse_iso(at) if at else None
    with store._lock:
        snap = build_assessment(store, event_key, as_of)
        decision_ts = iso(as_of) if as_of else None
        basis = {"final_snapshot": _snapshot_at(store, event_key, as_of), "forced": True,
                 "overridden_reason": reason}
        decision = store.add_decision(
            event_key, "event", event_key, "event.resolve", actor,
            basis=basis, reason=f"[强制解除] {reason}", created_at=decision_ts,
        )
        store.resolve_event(event_key, decision["id"], decision["created_at"])
    return {"decision": decision, "idempotent": False, "assessment": snap}


def event_chain(store: Store, event_key: str) -> dict[str, Any]:
    """完整可追溯事件链：当前状态 + 决策时间线（含每步依据）+ 调派。"""
    event = _require_event(store, event_key)
    decisions = []
    import json
    for d in store.list_decisions(event_key):
        row = dict(d)
        row["basis"] = json.loads(row.pop("basis_json"))
        row["metadata"] = json.loads(row["metadata_json"]) if row.get("metadata_json") else None
        decisions.append(row)
    return {
        "event": event,
        "timeline": decisions,
        "dispatches": store.list_dispatches(event_key),
    }
