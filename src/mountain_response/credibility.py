"""来源可信度评估。

可信度由两部分组成，全部可解释：
- 基础可靠度：值班部门为每个来源机构设定的先验值（默认 0.5，可通过接口调整，
  调整本身也记录为事件）；
- 佐证加成：同一位置同一方面上，有多少个其他独立机构给出了相同说法，
  每个佐证机构加 0.2，总分封顶 1.0。
"""

DEFAULT_RELIABILITY = 0.5
CORROBORATION_BONUS = 0.2
MAX_SCORE = 1.0


class CredibilityEngine:
    """根据来源注册表计算单条说法的可信度。"""

    def __init__(self, reliability: dict[str, float] | None = None):
        self._reliability = dict(reliability or {})

    def base_reliability(self, agency: str) -> float:
        return self._reliability.get(agency, DEFAULT_RELIABILITY)

    def score(self, agency: str, corroborating_agencies: int) -> dict:
        """返回可信度及其构成，供队列解释与判断取舍使用。"""
        base = self.base_reliability(agency)
        bonus = CORROBORATION_BONUS * max(0, corroborating_agencies)
        total = min(MAX_SCORE, round(base + bonus, 4))
        return {
            "score": total,
            "base_reliability": base,
            "corroborating_agencies": corroborating_agencies,
            "corroboration_bonus": round(bonus, 4),
        }
