"""参赛资格与投稿治理模块的 SQLite 表结构。

设计要点：

- ``eg_submission_events`` / ``eg_work_versions`` / ``eg_team_member_history``
  只追加、不更新、不删除，提交、补正、撤回、换赛道、成员变更、评审结论、
  冻结与申诉均按发生时间留痕；
- ``eg_submissions`` 是与事件流同步维护的当前状态，任何状态变更都与事件
  追加处于同一个事务；
- 自然人、外部机构（工作室）、监护/授权代理关系、受益主体集群与团队成员
  分开建模，防止关联身份重复占位；
- 赛道带互斥组与名额，报名窗口带开放区间与冻结标志。
"""

from __future__ import annotations

SCHEMA = """
CREATE TABLE IF NOT EXISTS eg_persons (
    person_id TEXT PRIMARY KEY,
    legal_name TEXT NOT NULL,
    birth_date TEXT,
    id_doc_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS eg_external_orgs (
    external_org_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    license_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS eg_relationships (
    relationship_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK(kind IN ('guardian','authorized_agent')),
    principal_type TEXT NOT NULL CHECK(principal_type IN ('person','org')),
    principal_person_id TEXT REFERENCES eg_persons(person_id),
    principal_org_id TEXT REFERENCES eg_external_orgs(external_org_id),
    agent_type TEXT CHECK(agent_type IS NULL OR agent_type IN ('person','org')),
    agent_person_id TEXT REFERENCES eg_persons(person_id),
    agent_org_id TEXT REFERENCES eg_external_orgs(external_org_id),
    document_hash TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    revoked_at TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS eg_subjects (
    subject_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    merged_into TEXT REFERENCES eg_subjects(subject_id),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS eg_subject_links (
    link_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES eg_subjects(subject_id),
    party_type TEXT NOT NULL CHECK(party_type IN ('person','org')),
    person_id TEXT REFERENCES eg_persons(person_id),
    external_org_id TEXT REFERENCES eg_external_orgs(external_org_id),
    link_role TEXT NOT NULL CHECK(link_role IN ('self','studio','member')),
    active INTEGER NOT NULL CHECK(active IN (0,1)),
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_eg_link_person
    ON eg_subject_links(person_id) WHERE person_id IS NOT NULL AND active = 1;
CREATE UNIQUE INDEX IF NOT EXISTS idx_eg_link_org
    ON eg_subject_links(external_org_id) WHERE external_org_id IS NOT NULL AND active = 1;
CREATE TABLE IF NOT EXISTS eg_teams (
    team_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES eg_subjects(subject_id),
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS eg_team_members (
    team_id TEXT NOT NULL REFERENCES eg_teams(team_id),
    person_id TEXT NOT NULL REFERENCES eg_persons(person_id),
    role TEXT NOT NULL,
    active INTEGER NOT NULL CHECK(active IN (0,1)),
    joined_at TEXT NOT NULL,
    left_at TEXT,
    PRIMARY KEY(team_id, person_id)
);
CREATE TABLE IF NOT EXISTS eg_team_member_history (
    history_id TEXT PRIMARY KEY,
    team_id TEXT NOT NULL,
    person_id TEXT NOT NULL,
    change_type TEXT NOT NULL CHECK(change_type IN ('joined','left')),
    occurred_at TEXT NOT NULL,
    detail_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS eg_tracks (
    track_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    exclusive_group TEXT,
    slots_total INTEGER NOT NULL CHECK(slots_total >= 1),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS eg_windows (
    window_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    opens_at TEXT NOT NULL,
    closes_at TEXT NOT NULL,
    frozen INTEGER NOT NULL DEFAULT 0 CHECK(frozen IN (0,1)),
    frozen_at TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS eg_window_tracks (
    window_id TEXT NOT NULL REFERENCES eg_windows(window_id),
    track_id TEXT NOT NULL REFERENCES eg_tracks(track_id),
    PRIMARY KEY(window_id, track_id)
);
CREATE TABLE IF NOT EXISTS eg_submissions (
    submission_id TEXT PRIMARY KEY,
    window_id TEXT NOT NULL REFERENCES eg_windows(window_id),
    track_id TEXT NOT NULL REFERENCES eg_tracks(track_id),
    applicant_type TEXT NOT NULL CHECK(applicant_type IN ('person','org')),
    applicant_id TEXT NOT NULL,
    represented_type TEXT CHECK(represented_type IS NULL OR represented_type IN ('person','org')),
    represented_id TEXT,
    guardian_person_id TEXT REFERENCES eg_persons(person_id),
    team_id TEXT REFERENCES eg_teams(team_id),
    subject_id TEXT NOT NULL REFERENCES eg_subjects(subject_id),
    status TEXT NOT NULL CHECK(status IN (
        'in_review','awaiting_correction','accepted','rejected','withdrawn')),
    current_version INTEGER NOT NULL DEFAULT 0,
    effective_version INTEGER,
    appeal_effective_version INTEGER,
    correction_due_at TEXT,
    frozen_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS eg_submission_parties (
    submission_id TEXT NOT NULL REFERENCES eg_submissions(submission_id),
    party_type TEXT NOT NULL CHECK(party_type IN ('person','org')),
    party_id TEXT NOT NULL,
    party_role TEXT NOT NULL,
    PRIMARY KEY(submission_id, party_type, party_id, party_role)
);
CREATE INDEX IF NOT EXISTS idx_eg_parties_person ON eg_submission_parties(party_type, party_id);
CREATE TABLE IF NOT EXISTS eg_work_versions (
    submission_id TEXT NOT NULL REFERENCES eg_submissions(submission_id),
    version INTEGER NOT NULL CHECK(version >= 1),
    title TEXT NOT NULL,
    content_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    late INTEGER NOT NULL DEFAULT 0 CHECK(late IN (0,1)),
    introduced_by_event TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(submission_id, version)
);
CREATE TABLE IF NOT EXISTS eg_submission_events (
    event_id TEXT PRIMARY KEY,
    submission_id TEXT NOT NULL REFERENCES eg_submissions(submission_id),
    seq INTEGER NOT NULL,
    event_type TEXT NOT NULL CHECK(event_type IN (
        'submitted','correction_submitted','withdrawn','transferred',
        'members_changed','review_decided','frozen',
        'appeal_filed','appeal_decided')),
    occurred_at TEXT NOT NULL,
    actor TEXT NOT NULL,
    track_id TEXT,
    version INTEGER,
    detail_json TEXT NOT NULL,
    detail_hash TEXT NOT NULL,
    UNIQUE(submission_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_eg_events_time ON eg_submission_events(occurred_at);
CREATE TABLE IF NOT EXISTS eg_review_tasks (
    task_id TEXT PRIMARY KEY,
    submission_id TEXT NOT NULL UNIQUE REFERENCES eg_submissions(submission_id),
    status TEXT NOT NULL CHECK(status IN ('open','waiting_correction','decided','closed_withdrawn')),
    reviewer_id TEXT,
    opened_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS eg_appeals (
    appeal_id TEXT PRIMARY KEY,
    submission_id TEXT NOT NULL REFERENCES eg_submissions(submission_id),
    window_id TEXT NOT NULL REFERENCES eg_windows(window_id),
    reason TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    evidence_hash TEXT NOT NULL,
    late_version INTEGER,
    status TEXT NOT NULL CHECK(status IN ('pending','accepted','rejected')),
    filed_by TEXT NOT NULL,
    filed_at TEXT NOT NULL,
    decided_by TEXT,
    decided_at TEXT,
    decision_note TEXT
);
CREATE TABLE IF NOT EXISTS eg_request_receipts (
    request_id TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""
