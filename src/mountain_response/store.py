"""只增不改的事件存储。

所有进入系统的事实（接报、更正、撤销、封路、派遣、解除）都追加为事件，
事件之间以 SHA-256 哈希链接，任何事后篡改都会在校验时暴露。
存储可选地持久化到 JSONL 文件，完全离线可用。
"""

import hashlib
import json
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

GENESIS_HASH = "0" * 64


def canonical_json(obj) -> str:
    """确定性 JSON 序列化，用于哈希与落盘。"""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class StoredEvent:
    """一条已落链的事件。"""

    seq: int
    event_id: str
    event_type: str
    recorded_at: datetime
    payload: dict
    prev_hash: str
    hash: str

    def body_for_hash(self) -> dict:
        return {
            "seq": self.seq,
            "event_type": self.event_type,
            "recorded_at": self.recorded_at.isoformat(),
            "payload": self.payload,
            "prev_hash": self.prev_hash,
        }

    def to_dict(self) -> dict:
        return {
            "seq": self.seq,
            "event_id": self.event_id,
            "event_type": self.event_type,
            "recorded_at": self.recorded_at.isoformat(),
            "payload": self.payload,
            "prev_hash": self.prev_hash,
            "hash": self.hash,
        }

    @staticmethod
    def from_dict(data: dict) -> "StoredEvent":
        return StoredEvent(
            seq=data["seq"],
            event_id=data["event_id"],
            event_type=data["event_type"],
            recorded_at=datetime.fromisoformat(data["recorded_at"]),
            payload=data["payload"],
            prev_hash=data["prev_hash"],
            hash=data["hash"],
        )


def _digest(event: StoredEvent) -> str:
    return hashlib.sha256(canonical_json(event.body_for_hash()).encode("utf-8")).hexdigest()


class EventStore:
    """内存事件存储，可选 JSONL 文件持久化。只允许追加。"""

    def __init__(self, path: Optional[str | Path] = None):
        self._events: list[StoredEvent] = []
        self._lock = threading.RLock()
        self._path = Path(path) if path else None
        if self._path and self._path.exists():
            self._load()

    def _load(self) -> None:
        with self._path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    self._events.append(StoredEvent.from_dict(json.loads(line)))
        ok, problem = self.verify()
        if not ok:
            raise ValueError(f"事件链校验失败，拒绝加载: {problem}")

    def next_seq(self) -> int:
        with self._lock:
            return len(self._events) + 1

    def append(self, event_type: str, recorded_at: datetime, payload: dict) -> StoredEvent:
        with self._lock:
            seq = len(self._events) + 1
            prev_hash = self._events[-1].hash if self._events else GENESIS_HASH
            event = StoredEvent(
                seq=seq,
                event_id=f"evt-{seq:06d}",
                event_type=event_type,
                recorded_at=recorded_at,
                payload=payload,
                prev_hash=prev_hash,
                hash="",
            )
            event = StoredEvent(
                seq=event.seq,
                event_id=event.event_id,
                event_type=event.event_type,
                recorded_at=event.recorded_at,
                payload=event.payload,
                prev_hash=event.prev_hash,
                hash=_digest(event),
            )
            self._events.append(event)
            if self._path:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                with self._path.open("a", encoding="utf-8") as fh:
                    fh.write(canonical_json(event.to_dict()) + "\n")
            return event

    def all(self) -> list[StoredEvent]:
        with self._lock:
            return list(self._events)

    def until(self, as_of: datetime) -> list[StoredEvent]:
        """返回入库时间不晚于 as_of 的事件，用于历史回放。"""
        with self._lock:
            return [e for e in self._events if e.recorded_at <= as_of]

    def verify(self) -> tuple[bool, str]:
        """校验哈希链完整性。返回 (是否通过, 说明)。"""
        with self._lock:
            prev_hash = GENESIS_HASH
            for event in self._events:
                if event.prev_hash != prev_hash:
                    return False, f"seq {event.seq} 前向哈希断裂"
                if _digest(event) != event.hash:
                    return False, f"seq {event.seq} 内容哈希不符"
                prev_hash = event.hash
            return True, f"共 {len(self._events)} 条事件，链完整"
