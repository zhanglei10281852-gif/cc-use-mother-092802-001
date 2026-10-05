"""可解释的处置队列。

队列按四个因子加权打分，每个队列项都附带完整的因子分解，
值班人员可以看到每一分来自哪里：

    score = 100 * (0.45 * 受影响人口
                 + 0.30 * 设施关键度
                 + 0.15 * 信息可信度
                 + 0.10 * 报告时效)

- 受影响人口：当前判断给出的受影响人数，按 100 人封顶归一；
- 设施关键度：需求类型对应的固定权重（救人 > 供水 > 通路）；
- 信息可信度：触发该需求的判断的可信度；若该方面说法存在争议，
  可信度减半并标记 needs_verification，提示先复核再投入力量；
- 报告时效：最新观测时间距现在的小时数，按 72 小时线性衰减。
"""

import hashlib

from .contracts import ClaimAspect
from .state import StateView, judge, parse_ts

WEIGHTS = {
    "affected_people": 0.45,
    "facility_criticality": 0.30,
    "information_confidence": 0.15,
    "report_recency": 0.10,
}

PEOPLE_NORMALIZER = 100
RECENCY_DECAY_HOURS = 72
DISPUTE_PENALTY = 0.5

HAZARD_VALUES = {
    ClaimAspect.ROAD_STATUS: {"blocked", "buried", "damaged", "closed"},
    ClaimAspect.WATER_SUPPLY: {"cut", "contaminated"},
}

NEED_RULES = [
    {"aspect": ClaimAspect.PEOPLE_TRAPPED, "need_kind": "rescue", "criticality": 1.0},
    {"aspect": ClaimAspect.WATER_SUPPLY, "need_kind": "water_restore", "criticality": 0.9},
    {"aspect": ClaimAspect.ROAD_STATUS, "need_kind": "road_clearance", "criticality": 0.8},
]


def make_need_id(event_key: str, location_code: str, need_kind: str) -> str:
    """确定性需求编号：同一事件同一位置同一类需求永远得到同一编号，
    这是派遣幂等的依据——重复报文推不出第二个需求，自然无法重复派遣。"""
    digest = hashlib.sha1(f"{event_key}|{location_code}|{need_kind}".encode("utf-8")).hexdigest()
    return f"need-{digest[:12]}"


def _is_hazard(aspect: str, value: str) -> bool:
    if aspect == ClaimAspect.PEOPLE_TRAPPED:
        try:
            return int(value) > 0
        except ValueError:
            return False
    return value in HAZARD_VALUES.get(aspect, set())


def _people_at(view: StateView, location_code: str) -> tuple[int, list[str]]:
    """取该位置当前判断的受影响人数（受困人数与受影响人数取大者）。"""
    people = 0
    claim_ids: list[str] = []
    for aspect in (ClaimAspect.AFFECTED_PEOPLE, ClaimAspect.PEOPLE_TRAPPED):
        judgment = judge(view, location_code, aspect)
        if judgment is None:
            continue
        claim_ids.append(judgment["winning_claim_id"])
        try:
            people = max(people, int(judgment["value"]))
        except ValueError:
            continue
    return people, claim_ids


def build_queue(view: StateView, now) -> list[dict]:
    """基于物化视图生成处置队列。now 可传入历史时刻以回放历史队列。"""
    items: list[dict] = []
    for event_key, incident in sorted(view.incidents.items()):
        if incident.get("resolved"):
            continue
        for location_code in sorted(view.locations_for_incident(event_key)):
            for rule in NEED_RULES:
                judgment = judge(view, location_code, rule["aspect"])
                if judgment is None or not _is_hazard(rule["aspect"], judgment["value"]):
                    continue
                items.append(_build_item(view, now, event_key, location_code, rule, judgment))
    items.sort(key=lambda item: (-item["score"], item["need_id"]))
    return items


def _build_item(view: StateView, now, event_key: str, location_code: str, rule: dict, judgment: dict) -> dict:
    need_kind = rule["need_kind"]
    need_id = make_need_id(event_key, location_code, need_kind)

    people, people_claim_ids = _people_at(view, location_code)
    people_norm = min(people / PEOPLE_NORMALIZER, 1.0)

    contested = judgment["contested"]
    raw_confidence = judgment["credibility"]["score"]
    confidence = raw_confidence * (DISPUTE_PENALTY if contested else 1.0)

    observed = max(parse_ts(c["claim"]["observed_at"]) for c in judgment["claims"])
    age_hours = max(0.0, (now - observed).total_seconds() / 3600.0)
    recency_norm = max(0.0, 1.0 - age_hours / RECENCY_DECAY_HOURS)

    factors = [
        {
            "name": "affected_people",
            "raw": people,
            "normalized": round(people_norm, 4),
            "weight": WEIGHTS["affected_people"],
            "contribution": round(100 * WEIGHTS["affected_people"] * people_norm, 2),
        },
        {
            "name": "facility_criticality",
            "raw": rule["criticality"],
            "normalized": rule["criticality"],
            "weight": WEIGHTS["facility_criticality"],
            "contribution": round(100 * WEIGHTS["facility_criticality"] * rule["criticality"], 2),
        },
        {
            "name": "information_confidence",
            "raw": raw_confidence,
            "dispute_penalty": DISPUTE_PENALTY if contested else 1.0,
            "normalized": round(confidence, 4),
            "weight": WEIGHTS["information_confidence"],
            "contribution": round(100 * WEIGHTS["information_confidence"] * confidence, 2),
        },
        {
            "name": "report_recency",
            "raw": round(age_hours, 2),
            "normalized": round(recency_norm, 4),
            "weight": WEIGHTS["report_recency"],
            "contribution": round(100 * WEIGHTS["report_recency"] * recency_norm, 2),
        },
    ]
    score = round(sum(factor["contribution"] for factor in factors), 2)

    basis_claim_ids = [judgment["winning_claim_id"], *people_claim_ids]
    dispatch = view.dispatches.get(need_id)
    return {
        "need_id": need_id,
        "event_key": event_key,
        "location_code": location_code,
        "need_kind": need_kind,
        "score": score,
        "needs_verification": contested,
        "dispatch": (
            {"status": "active", "dispatch_event_id": dispatch["dispatch_event_id"]}
            if dispatch
            else {"status": "none"}
        ),
        "explanation": {
            "formula": "score = 100 * Σ(权重 × 归一化因子)",
            "factors": factors,
            "contested": contested,
            "basis_claim_ids": sorted(set(basis_claim_ids)),
        },
    }
