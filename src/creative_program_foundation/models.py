"""定义基础服务在模块边界使用的数据对象。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Actor:
    """表示具有明确角色的后台操作者。"""

    actor_id: str
    display_name: str
    role: str
    organization_id: str
    active: bool


@dataclass(frozen=True)
class Site:
    """表示文化创意赛事组织下的业务场所。"""

    site_id: str
    organization_id: str
    name: str
    timezone_name: str
    version: int


@dataclass(frozen=True)
class DomainRecord:
    """表示已经持久化的领域资料记录。"""

    record_id: str
    site_id: str
    category: str
    external_key: str
    payload: dict[str, Any]
    created_by: str
    created_at: str


@dataclass(frozen=True)
class WriteReceipt:
    """描述一次幂等写入的稳定结果。"""

    request_id: str
    resource_type: str
    resource_id: str
    replayed: bool


@dataclass(frozen=True)
class Person:
    """自然人参赛主体。"""

    person_id: str
    display_name: str
    is_minor: bool
    id_masked: str


@dataclass(frozen=True)
class OrganizationParty:
    """工作室、代理机构等组织主体及其受益控制人。"""

    organization_party_id: str
    legal_name: str
    org_type: str
    beneficial_person_id: str


@dataclass(frozen=True)
class Representation:
    """监护或授权代理关系。"""

    relation_id: str
    subject_person_id: str
    representative_person_id: str
    kind: str
    scope: dict[str, Any]
    valid_from: str
    valid_until: str | None
    status: str


@dataclass(frozen=True)
class Track:
    """赛道及其名额与互斥组。"""

    track_id: str
    site_id: str
    name: str
    quota: int
    mutex_group: str


@dataclass(frozen=True)
class RegistrationWindow:
    """报名窗口。"""

    window_id: str
    site_id: str
    opens_at: str
    deadline_at: str
    status: str
    frozen_at: str | None


@dataclass(frozen=True)
class SubmissionView:
    """作品投稿对外视图。"""

    submission_id: str
    window_id: str
    title: str
    track_id: str | None
    status: str
    beneficiary_person_id: str
    submitter_person_id: str
    team_id: str | None
    organization_party_id: str | None
    relation_id: str | None
    current_version: int
    created_at: str
