"""SQLite 存储层。

设计原则：
- reports / raw_messages / decisions 只追加、不物理删除；更正与撤销通过状态列体现。
- 重复报文以 identity_key 唯一约束在存储层拦截。
- 决策（道路、调派、事件解除）一经写入不可变，新决策通过 parent_id / deactivated_by 接链。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from .timeutil import iso, now_utc, parse_iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    event_key   TEXT PRIMARY KEY,
    status      TEXT NOT NULL DEFAULT 'active',  -- active / resolved
    created_at  TEXT NOT NULL,
    opened_at   TEXT NOT NULL,
    resolved_at TEXT,
    resolve_decision_id INTEGER
);

CREATE TABLE IF NOT EXISTS reports (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    event_key        TEXT NOT NULL,
    received_at      TEXT NOT NULL,
    kind             TEXT NOT NULL,
    location_code    TEXT NOT NULL,
    agency           TEXT NOT NULL,
    external_id      TEXT NOT NULL,
    observed_at      TEXT NOT NULL,
    summary          TEXT NOT NULL,
    affected_people  INTEGER NOT NULL DEFAULT 0,
    severity         TEXT,
    road_code        TEXT,
    road_state       TEXT,
    facility         TEXT,
    facility_state   TEXT,
    corrects         TEXT,
    retracts         TEXT,
    identity_key     TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'active',
    superseded_by    INTEGER,
    note             TEXT,
    UNIQUE(event_key, agency, external_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_reports_identity
    ON reports(event_key, identity_key);

CREATE TABLE IF NOT EXISTS raw_messages (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    event_key            TEXT NOT NULL,
    received_at          TEXT NOT NULL,
    identity_key         TEXT NOT NULL,
    canonical_report_id  INTEGER,
    duplicate_of_raw     INTEGER,
    payload_json         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS facility_registry (
    event_key     TEXT NOT NULL,
    facility      TEXT NOT NULL,
    location_code TEXT NOT NULL,
    kind          TEXT NOT NULL DEFAULT 'water',
    critical      INTEGER NOT NULL DEFAULT 1,
    updated_at    TEXT NOT NULL,
    PRIMARY KEY (event_key, facility)
);

CREATE TABLE IF NOT EXISTS source_trust (
    agency     TEXT PRIMARY KEY,
    tier       TEXT NOT NULL,            -- official / responder / witness / unknown
    updated_at TEXT NOT NULL,
    note       TEXT
);

CREATE TABLE IF NOT EXISTS decisions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at      TEXT NOT NULL,
    event_key       TEXT NOT NULL,
    subject_type    TEXT NOT NULL,       -- road / dispatch / facility / event
    subject_id      TEXT NOT NULL,
    action          TEXT NOT NULL,
    actor           TEXT NOT NULL,
    reason          TEXT,
    basis_json      TEXT NOT NULL,       -- 决策时刻的依据快照（永不改写）
    parent_id       INTEGER,
    active          INTEGER NOT NULL DEFAULT 1,
    deactivated_by  INTEGER,
    end_time        TEXT,                -- 临时开放的结束时刻
    report_id       INTEGER,
    metadata_json   TEXT
);
CREATE INDEX IF NOT EXISTS ix_decisions_subject ON decisions(subject_type, subject_id, id);
CREATE INDEX IF NOT EXISTS ix_decisions_event ON decisions(event_key, id);

CREATE TABLE IF NOT EXISTS dispatches (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    dispatch_code  TEXT NOT NULL,
    event_key      TEXT NOT NULL,
    decision_id    INTEGER NOT NULL,
    unit           TEXT NOT NULL,
    target         TEXT,
    purpose        TEXT,
    status         TEXT NOT NULL,        -- dispatched / arrived / redeployed / stood_down
    created_at     TEXT NOT NULL,
    UNIQUE(event_key, dispatch_code)
);
"""


class StorageError(Exception):
    pass


class NotFound(StorageError):
    pass


class Conflict(StorageError):
    pass


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {k: row[k] for k in row.keys()}


class Store:
    """所有写操作串行化；离线单文件数据库。"""

    def __init__(self, path: str | Path = ":memory:"):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            self.path, check_same_thread=False, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._lock = threading.RLock()
        self._conn.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---- 基础工具 ----------------------------------------------------------

    def _one(self, sql: str, params: tuple = ()) -> dict[str, Any] | None:
        return _row_to_dict(self._conn.execute(sql, params).fetchone())

    def _all(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        return [_row_to_dict(r) for r in self._conn.execute(sql, params).fetchall()]  # type: ignore[misc]

    # ---- 事件 --------------------------------------------------------------

    def ensure_event(self, event_key: str, now: str | None = None) -> dict[str, Any]:
        with self._lock:
            row = self._one("SELECT * FROM events WHERE event_key=?", (event_key,))
            if row:
                return row
            ts = now or iso(now_utc())
            self._conn.execute(
                "INSERT INTO events(event_key, status, created_at, opened_at) VALUES(?,?,?,?)",
                (event_key, "active", ts, ts),
            )
            return self._one("SELECT * FROM events WHERE event_key=?", (event_key,))  # type: ignore[return-value]

    def get_event(self, event_key: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM events WHERE event_key=?", (event_key,))

    def resolve_event(
        self, event_key: str, decision_id: int, at: str
    ) -> dict[str, Any]:
        with self._lock:
            self._conn.execute(
                "UPDATE events SET status='resolved', resolved_at=?, resolve_decision_id=? "
                "WHERE event_key=?",
                (at, decision_id, event_key),
            )
            return self._one("SELECT * FROM events WHERE event_key=?", (event_key,))  # type: ignore[return-value]

    def reopen_event(self, event_key: str, at: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE events SET status='active', resolved_at=NULL, "
                "resolve_decision_id=NULL WHERE event_key=?",
                (event_key,),
            )

    # ---- 报文 --------------------------------------------------------------

    def insert_report(
        self, fields: dict[str, Any], identity_key: str, raw_payload: dict[str, Any]
    ) -> dict[str, Any]:
        """插入报告。identity_key 冲突时抛 Conflict（调用方转为重复报文）。"""
        cols = (
            "event_key received_at kind location_code agency external_id observed_at "
            "summary affected_people severity road_code road_state facility facility_state "
            "corrects retracts identity_key"
        ).split()
        placeholders = ",".join("?" for _ in cols)
        with self._lock:
            try:
                cur = self._conn.execute(
                    f"INSERT INTO reports({','.join(cols)}) VALUES({placeholders})",
                    tuple(fields[c] for c in cols),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict(f"重复报文 identity={identity_key}") from exc
            report_id = cur.lastrowid
            self._conn.execute(
                "INSERT INTO raw_messages(event_key, received_at, identity_key, "
                "canonical_report_id, duplicate_of_raw, payload_json) VALUES(?,?,?,?,?,?)",
                (
                    fields["event_key"],
                    fields["received_at"],
                    identity_key,
                    report_id,
                    None,
                    json.dumps(raw_payload, ensure_ascii=False, sort_keys=True),
                ),
            )
            return self._one("SELECT * FROM reports WHERE id=?", (report_id,))  # type: ignore[return-value]

    def record_duplicate_raw(
        self, event_key: str, identity_key: str, canonical_report_id: int, payload: dict
    ) -> int:
        with self._lock:
            first_raw = self._one(
                "SELECT id FROM raw_messages WHERE event_key=? AND canonical_report_id=?",
                (event_key, canonical_report_id),
            )
            cur = self._conn.execute(
                "INSERT INTO raw_messages(event_key, received_at, identity_key, "
                "canonical_report_id, duplicate_of_raw, payload_json) VALUES(?,?,?,?,?,?)",
                (
                    event_key,
                    iso(now_utc()),
                    identity_key,
                    canonical_report_id,
                    first_raw["id"] if first_raw else None,
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                ),
            )
            return cur.lastrowid

    def find_report_by_identity(
        self, event_key: str, identity_key: str
    ) -> dict[str, Any] | None:
        return self._one(
            "SELECT * FROM reports WHERE event_key=? AND identity_key=?",
            (event_key, identity_key),
        )

    def find_report_by_external(
        self, event_key: str, agency: str, external_id: str
    ) -> dict[str, Any] | None:
        return self._one(
            "SELECT * FROM reports WHERE event_key=? AND agency=? AND external_id=?",
            (event_key, agency, external_id),
        )

    def mark_superseded(self, report_id: int, by_report_id: int, note: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE reports SET status='superseded', superseded_by=?, note=? WHERE id=?",
                (by_report_id, note, report_id),
            )

    def mark_retracted(self, report_id: int, by_report_id: int, note: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE reports SET status='retracted', superseded_by=?, note=? WHERE id=?",
                (by_report_id, note, report_id),
            )

    def list_reports(
        self, event_key: str, location_code: str | None = None
    ) -> list[dict[str, Any]]:
        if location_code:
            return self._all(
                "SELECT * FROM reports WHERE event_key=? AND location_code=? ORDER BY observed_at, id",
                (event_key, location_code),
            )
        return self._all(
            "SELECT * FROM reports WHERE event_key=? ORDER BY observed_at, id",
            (event_key,),
        )

    def list_raw(self, event_key: str) -> list[dict[str, Any]]:
        return self._all(
            "SELECT * FROM raw_messages WHERE event_key=? ORDER BY id",
            (event_key,),
        )

    def get_report(self, report_id: int) -> dict[str, Any] | None:
        return self._one("SELECT * FROM reports WHERE id=?", (report_id,))

    # ---- 设施 / 来源 -------------------------------------------------------

    def upsert_facility(
        self,
        event_key: str,
        facility: str,
        location_code: str,
        kind: str,
        critical: bool,
    ) -> dict[str, Any]:
        with self._lock:
            self._conn.execute(
                "INSERT INTO facility_registry(event_key, facility, location_code, kind, critical, updated_at) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(event_key, facility) DO UPDATE SET "
                "location_code=excluded.location_code, kind=excluded.kind, "
                "critical=excluded.critical, updated_at=excluded.updated_at",
                (event_key, facility, location_code, kind, 1 if critical else 0, iso(now_utc())),
            )
            return self._one(
                "SELECT * FROM facility_registry WHERE event_key=? AND facility=?",
                (event_key, facility),
            )  # type: ignore[return-value]

    def list_facilities(self, event_key: str) -> list[dict[str, Any]]:
        return self._all(
            "SELECT * FROM facility_registry WHERE event_key=?", (event_key,)
        )

    def set_source_trust(self, agency: str, tier: str, note: str | None = None) -> dict:
        with self._lock:
            self._conn.execute(
                "INSERT INTO source_trust(agency, tier, updated_at, note) VALUES(?,?,?,?) "
                "ON CONFLICT(agency) DO UPDATE SET tier=excluded.tier, "
                "updated_at=excluded.updated_at, note=excluded.note",
                (agency, tier, iso(now_utc()), note),
            )
            return self._one("SELECT * FROM source_trust WHERE agency=?", (agency,))  # type: ignore[return-value]

    def get_source_trust(self, agency: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM source_trust WHERE agency=?", (agency,))

    def list_source_trust(self) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM source_trust ORDER BY agency")

    # ---- 决策链 ------------------------------------------------------------

    def latest_decision(
        self, subject_type: str, subject_id: str
    ) -> dict[str, Any] | None:
        return self._one(
            "SELECT * FROM decisions WHERE subject_type=? AND subject_id=? "
            "ORDER BY id DESC LIMIT 1",
            (subject_type, subject_id),
        )

    def latest_decision_as_of(
        self, subject_type: str, subject_id: str, as_of_iso: str
    ) -> dict[str, Any] | None:
        """截至某时刻该主体的最新决策（用于历史复盘，不含"未来"决策）。"""
        return self._one(
            "SELECT * FROM decisions WHERE subject_type=? AND subject_id=? "
            "AND created_at<=? ORDER BY id DESC LIMIT 1",
            (subject_type, subject_id, as_of_iso),
        )

    def add_decision(
        self,
        event_key: str,
        subject_type: str,
        subject_id: str,
        action: str,
        actor: str,
        basis: dict[str, Any],
        reason: str | None = None,
        end_time: str | None = None,
        report_id: int | None = None,
        metadata: dict[str, Any] | None = None,
        deactivate_previous: bool = False,
        created_at: str | None = None,
    ) -> dict[str, Any]:
        """追加一条决策；如需要，把该主体上一条决策置为失效（原行保留）。"""
        with self._lock:
            prev = self.latest_decision(subject_type, subject_id)
            ts = created_at or iso(now_utc())
            cur = self._conn.execute(
                "INSERT INTO decisions(created_at, event_key, subject_type, subject_id, "
                "action, actor, reason, basis_json, parent_id, active, end_time, report_id, metadata_json) "
                "VALUES(?,?,?,?,?,?,?,?,?,1,?,?,?)",
                (
                    ts,
                    event_key,
                    subject_type,
                    subject_id,
                    action,
                    actor,
                    reason,
                    json.dumps(basis, ensure_ascii=False, sort_keys=True),
                    prev["id"] if prev else None,
                    end_time,
                    report_id,
                    json.dumps(metadata, ensure_ascii=False, sort_keys=True) if metadata else None,
                ),
            )
            new_id = cur.lastrowid
            if deactivate_previous and prev and prev["active"]:
                self._conn.execute(
                    "UPDATE decisions SET active=0, deactivated_by=? WHERE id=?",
                    (new_id, prev["id"]),
                )
            return self._one("SELECT * FROM decisions WHERE id=?", (new_id,))  # type: ignore[return-value]

    def list_decisions(
        self, event_key: str | None = None,
        subject_type: str | None = None,
        subject_id: str | None = None,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM decisions WHERE 1=1"
        params: list[Any] = []
        if event_key:
            sql += " AND event_key=?"
            params.append(event_key)
        if subject_type:
            sql += " AND subject_type=?"
            params.append(subject_type)
        if subject_id:
            sql += " AND subject_id=?"
            params.append(subject_id)
        sql += " ORDER BY id"
        return self._all(sql, tuple(params))

    def get_decision(self, decision_id: int) -> dict[str, Any] | None:
        return self._one("SELECT * FROM decisions WHERE id=?", (decision_id,))

    def active_dispatch_for_target(
        self, event_key: str, target: str | None, purpose: str | None
    ) -> dict[str, Any] | None:
        """同一目标+任务方向上仍未收队的调派（NULL 用 IS 匹配）。"""
        sql = (
            "SELECT d.* FROM dispatches d WHERE d.event_key=? "
            "AND d.status IN ('dispatched','arrived','redeployed')"
        )
        params: list[Any] = [event_key]
        if target is None:
            sql += " AND d.target IS NULL"
        else:
            sql += " AND d.target=?"
            params.append(target)
        if purpose is None:
            sql += " AND d.purpose IS NULL"
        else:
            sql += " AND d.purpose=?"
            params.append(purpose)
        return self._one(sql, tuple(params))

    def get_dispatch_by_code(self, event_key: str, code: str) -> dict[str, Any] | None:
        return self._one(
            "SELECT * FROM dispatches WHERE event_key=? AND dispatch_code=?",
            (event_key, code),
        )

    def get_dispatch(self, dispatch_id: int) -> dict[str, Any] | None:
        return self._one("SELECT * FROM dispatches WHERE id=?", (dispatch_id,))

    def insert_dispatch(
        self,
        event_key: str,
        decision_id: int,
        code: str,
        unit: str,
        target: str | None,
        purpose: str | None,
        created_at: str,
    ) -> dict[str, Any]:
        with self._lock:
            try:
                cur = self._conn.execute(
                    "INSERT INTO dispatches(dispatch_code, event_key, decision_id, unit, "
                    "target, purpose, status, created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (code, event_key, decision_id, unit, target, purpose, "dispatched", created_at),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict(f"调派编号已存在: {code}") from exc
            return self._one("SELECT * FROM dispatches WHERE id=?", (cur.lastrowid,))  # type: ignore[return-value]

    def update_dispatch_status(self, dispatch_id: int, status: str, target: str | None = None) -> None:
        with self._lock:
            if target is not None:
                self._conn.execute(
                    "UPDATE dispatches SET status=?, target=? WHERE id=?",
                    (status, target, dispatch_id),
                )
            else:
                self._conn.execute(
                    "UPDATE dispatches SET status=? WHERE id=?", (status, dispatch_id)
                )

    def list_dispatches(self, event_key: str) -> list[dict[str, Any]]:
        return self._all(
            "SELECT * FROM dispatches WHERE event_key=? ORDER BY id", (event_key,)
        )

    # ---- 读取辅助 ----------------------------------------------------------

    @staticmethod
    def parse_dt(value: str | None):
        return parse_iso(value) if value else None
