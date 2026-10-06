"""态势评估：把相互矛盾的原始说法投影为"当前判断"，并给出可解释处置队列。

重要区分：
- evidence / history 中保留每条原始说法（含已撤销/已更正）；
- 评分只使用 status='active' 的说法，撤销与迟到更正不会删除历史依据；
- 所有阈值集中在本模块，值班人员可核对每一分的来源。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import timedelta
from typing import Any

from .storage import Store
from .timeutil import iso, now_utc, parse_iso

SOURCE_TIER_WEIGHT = {
    "official": 1.0,    # 政府/应急管理等官方核实渠道
    "responder": 0.85,  # 现场救援、巡查力量
    "witness": 0.6,     # 群众/目击者转发
    "unknown": 0.4,
}

SEVERITY_POINTS = {"info": 0, "minor": 10, "major": 20, "critical": 30}


@dataclass
class Component:
    name: str
    points: float
    why: str


@dataclass
class ClaimView:
    report_id: int
    kind: str
    agency: str
    tier: str
    observed_at: str
    summary: str
    affected_people: int
    severity: str
    road_code: str | None
    road_state: str | None
    facility: str | None
    facility_state: str | None
    status: str
    credibility: float


@dataclass
class TriageItem:
    event_key: str
    location_code: str
    level: str
    score: float
    confidence: float
    headline: str
    people_estimate: int
    people_range: list[int]
    components: list[Component] = field(default_factory=list)
    confidence_factors: list[str] = field(default_factory=list)
    contradictions: list[str] = field(default_factory=list)
    action_gaps: list[str] = field(default_factory=list)
    current_view: dict[str, Any] = field(default_factory=dict)
    evidence: list[ClaimView] = field(default_factory=list)
    superseded_history: list[ClaimView] = field(default_factory=list)


def _tier(store: Store, agency: str) -> str:
    row = store.get_source_trust(agency)
    return row["tier"] if row else "unknown"


def _recency_multiplier(observed_at: str, as_of) -> tuple[float, str]:
    age = as_of - parse_iso(observed_at)
    if age <= timedelta(hours=1):
        return 1.0, "观测在1小时内"
    if age <= timedelta(hours=3):
        return 0.9, "观测在3小时内"
    if age <= timedelta(hours=6):
        return 0.8, "观测在6小时内"
    if age <= timedelta(hours=12):
        return 0.7, "观测在12小时内"
    if age <= timedelta(hours=24):
        return 0.55, "观测在24小时内"
    return 0.4, "观测超过24小时（信息陈旧）"


def _people_points(pop: int) -> tuple[float, str]:
    if pop >= 100:
        return 100, f"约{pop}人受影响（≥100，按最高档计）"
    if pop >= 50:
        return 80, f"约{pop}人受影响（≥50）"
    if pop >= 20:
        return 60, f"约{pop}人受影响（≥20）"
    if pop >= 1:
        return 40, f"约{pop}人受影响（1–19）"
    return 0, "无受影响人口报告"


def _claim_view(store: Store, r: dict[str, Any], as_of) -> ClaimView:
    tier = _tier(store, r["agency"])
    rec, _ = _recency_multiplier(r["observed_at"], as_of)
    return ClaimView(
        report_id=r["id"], kind=r["kind"], agency=r["agency"], tier=tier,
        observed_at=r["observed_at"], summary=r["summary"],
        affected_people=r["affected_people"], severity=r["severity"],
        road_code=r["road_code"], road_state=r["road_state"],
        facility=r["facility"], facility_state=r["facility_state"],
        status=r["status"],
        credibility=round(SOURCE_TIER_WEIGHT[tier] * rec, 3),
    )


def _best_claim(claims: list[ClaimView], state_attr: str, state: str) -> ClaimView:
    """同一说法下最可信的报告；可信度相同则观测时间更新者胜出。"""
    candidates = [v for v in claims if getattr(v, state_attr) == state]
    return max(candidates, key=lambda v: (v.credibility, parse_iso(v.observed_at)))


def _road_operational_state(store: Store, event_key: str, road_code: str, as_of) -> dict[str, Any]:
    """道路的当前指挥状态以最新道路决策为准；临时开放过期自动回到封闭。"""
    d = store.latest_decision_as_of("road", road_code, iso(as_of))
    if not d or d["event_key"] != event_key:
        return {"state": "unknown", "decision_id": None}
    action = d["action"]
    state = {"road.close": "closed", "road.reopen": "open",
             "road.temporary_open": "temporary_open"}[action]
    expired = False
    if state == "temporary_open" and d["end_time"]:
        expired = parse_iso(d["end_time"]) <= as_of
        if expired:
            state = "closed"
    return {"state": state, "decision_id": d["id"], "action": action,
            "since": d["created_at"], "end_time": d["end_time"],
            "temporary_window_expired": expired, "active": bool(d["active"])}


def _facility_operational_state(store: Store, facility: str) -> dict[str, Any]:
    d = store.latest_decision("facility", facility)
    if not d:
        return {"state": "unknown", "decision_id": None}
    return {
        "state": "outage" if d["action"] == "facility.outage" else "restored",
        "decision_id": d["id"], "since": d["created_at"],
    }


def _served_targets(store: Store, event_key: str, location_code: str,
                    roads: list[str], facilities: list[str]) -> set[str]:
    served: set[str] = set()
    for d in store.list_dispatches(event_key):
        if d["status"] == "stood_down":
            continue
        t = d.get("target")
        if t == location_code or t in roads or t in facilities:
            served.add(t or "")
    return served


def build_assessment(store: Store, event_key: str, as_of=None) -> dict[str, Any]:
    as_of = as_of or now_utc()
    event = store.get_event(event_key)
    if event is None:
        raise KeyError(f"事件不存在: {event_key}")

    reports = store.list_reports(event_key)
    active = [r for r in reports if r["status"] == "active"]
    locations = sorted({r["location_code"] for r in active})

    items: list[TriageItem] = []
    total_people = 0
    blocked_roads: list[str] = []
    facilities_out: list[str] = []

    for loc in locations:
        loc_reports = [r for r in active if r["location_code"] == loc]
        views = [_claim_view(store, r, as_of) for r in loc_reports]
        history_views = [
            _claim_view(store, r, as_of)
            for r in reports
            if r["location_code"] == loc and r["status"] != "active"
        ]
        factors: list[str] = []
        contradictions: list[str] = []

        # ---- 当前判断：人口 ----
        # 同主题（location 上的人员情况）按可信度最高的说法采信；
        # 所有原始数字保留在 people_range，分歧进入 contradictions。
        people_views = [v for v in views if v.kind == "people"]
        all_numbers = [v.affected_people for v in people_views] or [0]
        positive_numbers = [n for n in all_numbers if n > 0]
        winner_people = max(
            people_views, key=lambda v: (v.credibility, parse_iso(v.observed_at))
        ) if people_views else None
        people_est = winner_people.affected_people if winner_people else 0
        total_people += people_est
        if positive_numbers and (
            max(all_numbers) - min(all_numbers) >= 20 or people_est != max(all_numbers)
        ):
            contradictions.append(
                f"受影响人口说法矛盾：{min(all_numbers)}–{max(all_numbers)}人；"
                f"当前按最高可信度说法采信{people_est}人（{winner_people.agency}"
                f" 报告#{winner_people.report_id}），原始说法均保留"
            )

        # ---- 道路：矛盾检测 + 指挥状态叠加 ----
        road_codes = sorted({v.road_code for v in views if v.road_code})
        current_roads: dict[str, Any] = {}
        closed_roads_here: list[str] = []
        for rc in road_codes:
            claims = [v for v in views if v.road_code == rc]
            states = {v.road_state for v in claims}
            best = {st: _best_claim(claims, "road_state", st) for st in states}
            claimed = max(best, key=lambda s: (best[s].credibility,
                                               parse_iso(best[s].observed_at)))
            op = _road_operational_state(store, event_key, rc, as_of)
            current_roads[rc] = {
                "claimed_state": claimed,
                "claim_disputed": len(states) > 1,
                "claim_credibility_by_state": {s: best[s].credibility for s in states},
                "winning_report_id": best[claimed].report_id,
                "operational": op,
            }
            if len(states) > 1:
                contradictions.append(
                    f"道路{rc}通行说法矛盾：" +
                    "、".join(f"{s}（最高可信度{best[s].credibility:.2f}）" for s in sorted(states)) +
                    f"，当前采信『{claimed}』，两方原始报文均保留"
                )
            eff_blocked = op["state"] == "closed" or (
                op["state"] == "unknown" and claimed == "closed"
            )
            if eff_blocked:
                closed_roads_here.append(rc)
                blocked_roads.append(rc)

        # ---- 供水设施 ----
        facs = sorted({v.facility for v in views if v.facility})
        current_facilities: dict[str, Any] = {}
        critical_out: list[str] = []
        noncritical_out: list[str] = []
        registry = {f["facility"]: f for f in store.list_facilities(event_key)}
        for fc in facs:
            claims = [v for v in views if v.facility == fc]
            states = {v.facility_state for v in claims}
            best = {st: _best_claim(claims, "facility_state", st) for st in states}
            claimed = max(best, key=lambda s: (best[s].credibility,
                                               parse_iso(best[s].observed_at)))
            op = _facility_operational_state(store, fc)
            critical = bool(registry.get(fc, {}).get("critical", 1))
            current_facilities[fc] = {
                "claimed_state": claimed,
                "claim_disputed": len(states) > 1,
                "claim_credibility_by_state": {s: best[s].credibility for s in states},
                "winning_report_id": best[claimed].report_id,
                "operational": op,
                "critical": critical,
            }
            if len(states) > 1:
                contradictions.append(
                    f"设施{fc}状态说法矛盾：" +
                    "、".join(f"{s}（最高可信度{best[s].credibility:.2f}）" for s in sorted(states)) +
                    f"，当前采信『{claimed}』"
                )
            eff_out = op["state"] == "outage" or (
                op["state"] == "unknown" and claimed == "outage"
            )
            if eff_out:
                facilities_out.append(fc)
                (critical_out if critical else noncritical_out).append(fc)

        # ---- 评分组件 ----
        components: list[Component] = []
        pp, why = _people_points(people_est)
        if pp:
            components.append(Component("受影响人口", pp, why))
        if critical_out:
            components.append(Component(
                "关键供水设施中断", 30,
                "关键设施中断：" + "、".join(critical_out)))
        if noncritical_out:
            components.append(Component(
                "一般供水设施中断", 15,
                "一般设施中断：" + "、".join(noncritical_out)))
        if closed_roads_here:
            components.append(Component(
                "道路阻断", 20,
                "当前阻断/封闭：" + "、".join(closed_roads_here)))
        # ---- 严重度：只统计与当前判断一致的"胜出"说法 ----
        current_winner_ids: set[int] = set()
        if winner_people is not None:
            current_winner_ids.add(winner_people.report_id)
        for rc, info in current_roads.items():
            winners = [v for v in views if v.road_code == rc
                       and v.road_state == info["claimed_state"]]
            if winners:
                current_winner_ids.add(max(winners, key=lambda v: v.credibility).report_id)
        for fc, info in current_facilities.items():
            winners = [v for v in views if v.facility == fc
                       and v.facility_state == info["claimed_state"]]
            if winners:
                current_winner_ids.add(max(winners, key=lambda v: v.credibility).report_id)
        max_sev = max(
            (SEVERITY_POINTS[v.severity] for v in views if v.report_id in current_winner_ids),
            default=0,
        )
        if max_sev:
            sev_name = next(s for s, p in SEVERITY_POINTS.items() if p == max_sev)
            components.append(Component(
                "灾情严重度", max_sev,
                f"当前采信说法的最高严重度为 {sev_name}（未被采信的矛盾说法不加分但仍留档）"))

        # ---- 处置缺口 ----
        served = _served_targets(store, event_key, loc, road_codes, facs)
        gaps: list[str] = []
        for rc in closed_roads_here:
            if rc not in served and loc not in served:
                gaps.append(f"道路{rc}阻断但尚无在途/在场力量")
        for fc in critical_out + noncritical_out:
            if fc not in served and loc not in served:
                gaps.append(f"设施{fc}中断但尚无在途/在场力量")
        if people_est >= 20 and loc not in served:
            gaps.append(f"{loc}有群众受影响但尚无在途/在场力量")
        for gap in gaps:
            components.append(Component("处置缺口", 10, gap))

        # ---- 可信度 ----
        creds = [v.credibility for v in views]
        confidence = sum(creds) / len(creds) if creds else 0.3
        agencies = {v.agency for v in views}
        if len(agencies) >= 2 and not contradictions:
            confidence += 0.1
            factors.append(f"{len(agencies)}个独立来源一致，可信度+0.1")
        elif len(agencies) >= 2:
            factors.append(f"{len(agencies)}个独立来源但存在分歧（见矛盾说明）")
        for v in views:
            rec_text = _recency_multiplier(v.observed_at, as_of)[1]
            factors.append(
                f"报告#{v.report_id} {v.agency}（{v.tier}，权重"
                f"{SOURCE_TIER_WEIGHT[v.tier]:.2f}）{rec_text}，综合{v.credibility:.2f}"
            )
        if contradictions:
            confidence -= 0.2
            factors.append("存在未消解的矛盾说法，可信度-0.2")
        confidence = round(min(0.99, max(0.2, confidence)), 2)

        raw_score = sum(c.points for c in components)
        score = round(raw_score * confidence, 1)

        # 硬性保底规则：大规模核实灾情不得因可信度压低到 P3/P4 以下
        guaranteed = None
        if people_est >= 50 and confidence >= 0.7:
            guaranteed = "P2"
        if people_est >= 100 and confidence >= 0.8:
            guaranteed = "P1"
        if score >= 75:
            level = "P1"
        elif score >= 50:
            level = "P2"
        elif score >= 25:
            level = "P3"
        else:
            level = "P4"
        # P1 可信度门槛：单条未证实说法即使分值很高也先降为 P2 核实
        if level == "P1" and confidence < 0.7:
            level = "P2"
            factors.append("综合可信度低于0.7，P1 暂降为 P2，待官方核实")
        order = {"P1": 1, "P2": 2, "P3": 3, "P4": 4}
        if guaranteed and order[guaranteed] < order[level]:
            factors.append(f"触发保底规则：{people_est}人受影响且可信度≥阈值，级别保底为{guaranteed}")
            level = guaranteed

        headline_parts = []
        if people_est:
            headline_parts.append(f"约{people_est}人受影响")
        if closed_roads_here:
            headline_parts.append(f"道路{'/'.join(closed_roads_here)}阻断")
        if critical_out:
            headline_parts.append(f"关键供水{'/'.join(critical_out)}中断")
        headline = "；".join(headline_parts) or f"{loc} 待核实信息"

        items.append(TriageItem(
            event_key=event_key, location_code=loc, level=level, score=score,
            confidence=confidence, headline=headline,
            people_estimate=people_est, people_range=[min(all_numbers), max(all_numbers)],
            components=components, confidence_factors=factors,
            contradictions=contradictions, action_gaps=gaps,
            current_view={"roads": current_roads, "facilities": current_facilities},
            evidence=views, superseded_history=history_views,
        ))

    level_rank = {"P1": 0, "P2": 1, "P3": 2, "P4": 3}
    items.sort(key=lambda it: (level_rank[it.level], -it.score, it.location_code))

    return {
        "event_key": event_key,
        "as_of": as_of.isoformat(timespec="seconds"),
        "event_status": event["status"],
        "opened_at": event["opened_at"],
        "resolved_at": event["resolved_at"],
        "totals": {
            "affected_people_upper_bound": total_people,
            "blocked_roads": sorted(set(blocked_roads)),
            "facilities_out": sorted(set(facilities_out)),
            "locations": len(items),
        },
        "queue": [asdict(it) for it in items],
    }
