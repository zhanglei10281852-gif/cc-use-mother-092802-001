"""服务门面：值班端调用的统一入口，聚合接报、评估、处置、追溯。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import operations
from .assessment import build_assessment
from .ingest import ingest_report
from .storage import Store


class Service:
    def __init__(self, store: Store | str | Path = ":memory:"):
        self.store = store if isinstance(store, Store) else Store(store)

    def close(self) -> None:
        self.store.close()

    # ---- 配置 --------------------------------------------------------------

    def register_facility(self, event_key: str, facility: str, location_code: str,
                          critical: bool = True, kind: str = "water") -> dict[str, Any]:
        self.store.ensure_event(event_key)
        return self.store.upsert_facility(
            event_key, facility, location_code, kind, critical)

    def set_source_trust(self, agency: str, tier: str, note: str | None = None) -> dict[str, Any]:
        if tier not in {"official", "responder", "witness", "unknown"}:
            raise ValueError("tier 必须为 official/responder/witness/unknown")
        return self.store.set_source_trust(agency, tier, note)

    # ---- 接报与态势 --------------------------------------------------------

    def ingest(self, payload: dict[str, Any]) -> dict[str, Any]:
        result = ingest_report(self.store, payload)
        return {
            "report_id": result.canonical_report_id,
            "duplicated": result.duplicated,
            "duplicate_raw_id": result.duplicate_raw_id,
            "retracted_report_id": result.retracted_report_id,
            "superseded_report_id": result.superseded_report_id,
            "target_missing": result.target_missing,
            "event_reopened": result.event_reopened,
            "identity_key": result.raw.get("identity_key"),
        }

    def assessment(self, event_key: str, as_of: str | None = None) -> dict[str, Any]:
        from .timeutil import parse_iso
        return build_assessment(
            self.store, event_key,
            parse_iso(as_of) if as_of else None,
        )

    # ---- 处置 --------------------------------------------------------------

    def road_action(self, req: dict[str, Any]) -> dict[str, Any]:
        return operations.road_decision(
            self.store, req["event_key"], req["road_code"], req["action"],
            actor=req.get("actor", "dispatcher"), reason=req.get("reason"),
            end_time=req.get("end_time"), report_id=req.get("report_id"),
            at=req.get("at"),
        )

    def dispatch(self, req: dict[str, Any]) -> dict[str, Any]:
        return operations.dispatch_unit(
            self.store, req["event_key"], req["unit"],
            target=req.get("target"), purpose=req.get("purpose"),
            actor=req.get("actor", "dispatcher"), reason=req.get("reason"),
            report_id=req.get("report_id"), dispatch_code=req.get("dispatch_code"),
            at=req.get("at"),
        )

    def dispatch_update(self, req: dict[str, Any]) -> dict[str, Any]:
        at = req.get("at")
        action = req["action"]
        if action == "dispatch.arrive":
            return operations.mark_arrived(
                self.store, req["event_key"], req["dispatch_code"],
                actor=req.get("actor", "field"), reason=req.get("reason"), at=at)
        if action == "dispatch.redeploy":
            return operations.redeploy(
                self.store, req["event_key"], req["dispatch_code"],
                req["new_target"], actor=req.get("actor", "dispatcher"),
                reason=req.get("reason"), at=at)
        if action == "dispatch.standdown":
            return operations.stand_down(
                self.store, req["event_key"], req["dispatch_code"],
                actor=req.get("actor", "dispatcher"), reason=req.get("reason"), at=at)
        raise ValueError(f"非法调派动作: {action}")

    def resolve(self, event_key: str, actor: str = "commander",
                reason: str | None = None, force: bool = False,
                at: str | None = None) -> dict[str, Any]:
        if force:
            return operations.force_resolve_event(
                self.store, event_key, actor, reason or "", at=at)
        return operations.resolve_event(
            self.store, event_key, actor, reason, at=at)

    # ---- 追溯 --------------------------------------------------------------

    def chain(self, event_key: str) -> dict[str, Any]:
        return operations.event_chain(self.store, event_key)

    def raw_messages(self, event_key: str) -> list[dict[str, Any]]:
        rows = self.store.list_raw(event_key)
        for r in rows:
            r["payload"] = json.loads(r.pop("payload_json"))
        return rows

    def reports(self, event_key: str) -> list[dict[str, Any]]:
        return self.store.list_reports(event_key)

    def event(self, event_key: str) -> dict[str, Any]:
        row = self.store.get_event(event_key)
        if row is None:
            raise KeyError(event_key)
        return row
