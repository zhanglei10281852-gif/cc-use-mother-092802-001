"""时间处理：统一以 UTC ISO-8601 存储，支持无时区输入按 UTC 处理。"""

from datetime import datetime, timezone


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def to_utc(dt: datetime) -> datetime:
    """把感知/朴素 datetime 统一为 UTC 感知时间。"""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def iso(dt: datetime) -> str:
    return to_utc(dt).isoformat(timespec="seconds")


def parse_iso(value: str | datetime) -> datetime:
    """解析 ISO-8601；末尾 Z 视为 UTC；朴素时间按 UTC。"""
    if isinstance(value, datetime):
        return to_utc(value)
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    return to_utc(datetime.fromisoformat(text))
