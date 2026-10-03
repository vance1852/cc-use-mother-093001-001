"""封装 SQLite 连接、建表和事务边界。"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS organizations (
    organization_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS actors (
    actor_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    active INTEGER NOT NULL CHECK(active IN (0, 1)),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sites (
    site_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    name TEXT NOT NULL,
    timezone_name TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS domain_records (
    record_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    category TEXT NOT NULL,
    external_key TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    UNIQUE(site_id, category, external_key)
);
CREATE TABLE IF NOT EXISTS request_receipts (
    request_id TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    occurred_at TEXT NOT NULL
);
-- 白塔杯投稿治理：赛道与名额
CREATE TABLE IF NOT EXISTS competition_tracks (
    track_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    name TEXT NOT NULL,
    quota INTEGER NOT NULL CHECK(quota > 0),
    mutex_group TEXT NOT NULL,
    created_at TEXT NOT NULL
);
-- 报名窗口（每个 site 一条当前窗口，窗口历史以版本行保存）
CREATE TABLE IF NOT EXISTS registration_windows (
    window_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    opens_at TEXT NOT NULL,
    deadline_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('open','closed','frozen')),
    frozen_at TEXT,
    created_at TEXT NOT NULL
);
-- 自然人（证件原文不落库，只存哈希与脱敏标记）
CREATE TABLE IF NOT EXISTS persons (
    person_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    is_minor INTEGER NOT NULL CHECK(is_minor IN (0,1)),
    id_hash TEXT NOT NULL UNIQUE,
    id_masked TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
-- 组织（工作室/机构），绑定受益控制自然人
CREATE TABLE IF NOT EXISTS organizations_ext (
    organization_party_id TEXT PRIMARY KEY,
    legal_name TEXT NOT NULL,
    org_type TEXT NOT NULL CHECK(org_type IN ('studio','agency','institution')),
    registration_hash TEXT NOT NULL UNIQUE,
    beneficial_person_id TEXT NOT NULL REFERENCES persons(person_id),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
-- 监护或授权代理关系（可撤销、带有效期与授权范围）
CREATE TABLE IF NOT EXISTS representation_relations (
    relation_id TEXT PRIMARY KEY,
    subject_person_id TEXT NOT NULL REFERENCES persons(person_id),
    representative_person_id TEXT NOT NULL REFERENCES persons(person_id),
    kind TEXT NOT NULL CHECK(kind IN ('guardianship','authorization')),
    scope_json TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_until TEXT,
    evidence_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active','revoked')),
    revoked_at TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
-- 创作团队
CREATE TABLE IF NOT EXISTS creative_teams (
    team_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
-- 团队成员（append-only 成员事件驱动当前名册）
CREATE TABLE IF NOT EXISTS team_member_events (
    member_event_id TEXT PRIMARY KEY,
    team_id TEXT NOT NULL REFERENCES creative_teams(team_id),
    person_id TEXT NOT NULL REFERENCES persons(person_id),
    change TEXT NOT NULL CHECK(change IN ('joined','left','beneficiary_designated')),
    occurred_at TEXT NOT NULL,
    changed_by TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT ''
);
-- 作品投稿主档
CREATE TABLE IF NOT EXISTS submissions (
    submission_id TEXT PRIMARY KEY,
    window_id TEXT NOT NULL REFERENCES registration_windows(window_id),
    site_id TEXT NOT NULL,
    title TEXT NOT NULL,
    submitter_person_id TEXT NOT NULL REFERENCES persons(person_id),
    team_id TEXT REFERENCES creative_teams(team_id),
    organization_party_id TEXT REFERENCES organizations_ext(organization_party_id),
    relation_id TEXT REFERENCES representation_relations(relation_id),
    beneficiary_person_id TEXT NOT NULL REFERENCES persons(person_id),
    current_track_id TEXT REFERENCES competition_tracks(track_id),
    status TEXT NOT NULL CHECK(status IN ('pending','awaiting_correction','approved','rejected','withdrawn')),
    created_at TEXT NOT NULL
);
-- 作品不可覆盖事件账本（提交/补正/撤回/换赛道/成员变更/审核/冻结）
CREATE TABLE IF NOT EXISTS entry_events (
    event_seq INTEGER PRIMARY KEY AUTOINCREMENT,
    submission_id TEXT NOT NULL REFERENCES submissions(submission_id),
    event_type TEXT NOT NULL,
    from_track_id TEXT,
    to_track_id TEXT,
    material_json TEXT,
    material_hash TEXT,
    note TEXT NOT NULL DEFAULT '',
    actor_id TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    UNIQUE(submission_id, event_seq)
);
-- 作品版本（每次提交/补正/换赛道生成新版本，永不更新删除）
CREATE TABLE IF NOT EXISTS submission_versions (
    submission_id TEXT NOT NULL REFERENCES submissions(submission_id),
    version_seq INTEGER NOT NULL CHECK(version_seq >= 1),
    track_id TEXT NOT NULL,
    material_json TEXT NOT NULL,
    material_hash TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (submission_id, version_seq)
);
-- 互斥资格占用：同一受益主体在同一互斥组内仅可有一个生效占位
CREATE TABLE IF NOT EXISTS qualification_holds (
    hold_id TEXT PRIMARY KEY,
    window_id TEXT NOT NULL REFERENCES registration_windows(window_id),
    mutex_group TEXT NOT NULL,
    beneficiary_person_id TEXT NOT NULL REFERENCES persons(person_id),
    submission_id TEXT NOT NULL REFERENCES submissions(submission_id),
    track_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('held','released')),
    created_at TEXT NOT NULL,
    released_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_qualification_active
    ON qualification_holds(window_id, mutex_group, beneficiary_person_id)
    WHERE status = 'held';
-- 审核任务（服务重启后仍可继续）
CREATE TABLE IF NOT EXISTS review_tasks (
    task_id TEXT PRIMARY KEY,
    submission_id TEXT NOT NULL UNIQUE REFERENCES submissions(submission_id),
    state TEXT NOT NULL CHECK(state IN ('queued','in_review','resolved')),
    assigned_to TEXT,
    decision TEXT CHECK(decision IS NULL OR decision IN ('approved','rejected','correction_requested')),
    reason TEXT NOT NULL DEFAULT '',
    appeal_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
-- 冲突依据（重复占位检测留痕）
CREATE TABLE IF NOT EXISTS conflict_records (
    conflict_id TEXT PRIMARY KEY,
    window_id TEXT NOT NULL,
    mutex_group TEXT NOT NULL,
    beneficiary_person_id TEXT NOT NULL,
    existing_submission_id TEXT NOT NULL,
    attempted_submission_id TEXT,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL
);
-- 申诉（截止后迟到材料只能进入申诉，不能回写原申请）
CREATE TABLE IF NOT EXISTS appeals (
    appeal_id TEXT PRIMARY KEY,
    submission_id TEXT NOT NULL REFERENCES submissions(submission_id),
    material_json TEXT NOT NULL,
    material_hash TEXT NOT NULL,
    reason TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('submitted','accepted','rejected')),
    decision_note TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    decided_at TEXT
);
-- 截止冻结快照（原子记录当时生效版本、赛道、状态与团队名册）
CREATE TABLE IF NOT EXISTS freeze_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    window_id TEXT NOT NULL REFERENCES registration_windows(window_id),
    submission_id TEXT NOT NULL REFERENCES submissions(submission_id),
    version_seq INTEGER NOT NULL,
    track_id TEXT NOT NULL,
    status TEXT NOT NULL,
    roster_json TEXT NOT NULL DEFAULT '[]',
    frozen_at TEXT NOT NULL,
    UNIQUE(window_id, submission_id)
);
-- 参赛人访问令牌（自然人自服务，令牌只展示哈希）
CREATE TABLE IF NOT EXISTS participant_tokens (
    token_hash TEXT PRIMARY KEY,
    person_id TEXT NOT NULL REFERENCES persons(person_id),
    label TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


class Database:
    """管理 SQLite 数据库并为服务提供短事务。"""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        # 单连接配合线程化 HTTP：用可重入锁把事务串行化，避免两个线程在同一连接上交错 BEGIN，
        # 也保证「幂等检查 + 写入」整体原子，并发重复请求不会产生第二份报名。
        self._tx_lock = threading.RLock()
        self.connection = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self.connection.executescript(SCHEMA)

    @contextmanager
    def transaction(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        """在异常时回滚，在成功时提交；同进程内事务串行执行。"""

        with self._tx_lock:
            self.connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            try:
                yield self.connection
            except Exception:
                self.connection.rollback()
                raise
            else:
                self.connection.commit()

    def close(self) -> None:
        """关闭底层连接。"""

        self.connection.close()
