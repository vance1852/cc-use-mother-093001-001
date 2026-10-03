"""参赛资格与投稿治理领域服务。

把自然人、组织（工作室）、监护/授权代理关系、团队成员、受益创作主体、
作品版本、赛道名额和报名窗口纳入同一套可追溯规则。

不变式：

- 所有写操作都在 ``BEGIN IMMEDIATE`` 短事务内完成：状态行、只追加事件、
  幂等回执与全局哈希审计链同生共死，服务崩溃不会留下半截报名；
- ``eg_submission_events`` 只追加，提交/补正/撤回/换赛道/成员变更/评审/
  冻结/申诉均有按发生时间排序的序号，任何修改都表现为新事件；
- 同一受益创作主体（含已归并集群）在同一窗口、同一赛道互斥组内至多保留
  一份未撤回投稿，工作室、代理人、个人等关联身份不能重复占位；
- 窗口冻结原子地锁定每份投稿的生效版本；冻结后迟到内容只允许写入申诉，
  原申请与原生效版本永不被改写。
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from .audit import append_event, canonical_json, digest
from .clock import Clock, SystemClock
from .eligibility_storage import SCHEMA
from .errors import (
    ConflictError,
    EligibilityConflictError,
    FrozenWindowError,
    NotFoundError,
    PermissionDenied,
    QuotaExhaustedError,
    ValidationError,
    WindowClosedError,
)
from .storage import Database
from .models import WriteReceipt

IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{1,63}$")
STAFF_ROLES = frozenset({"admin", "operator", "reviewer", "auditor"})
DECISION_STATUS = {"accepted": "accepted", "rejected": "rejected",
                   "correction_requested": "awaiting_correction"}


def parse_ts(value: str, field: str) -> datetime:
    """把 ISO 8601 文本解析为带 UTC 时区的时间。"""

    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValidationError(f"{field} 不是合法的 ISO 8601 时间") from exc
    if parsed.tzinfo is None:
        raise ValidationError(f"{field} 必须携带时区")
    return parsed.astimezone(timezone.utc)


def fmt_ts(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class EligibilityService:
    """实现资格建档、投稿生命周期、评审冻结与追溯查询。"""

    def __init__(self, database: Database, clock: Clock | None = None) -> None:
        self.database = database
        self.clock = clock or SystemClock()
        database.connection.executescript(SCHEMA)

    # ------------------------------------------------------------------
    # 通用工具
    # ------------------------------------------------------------------
    def _now(self) -> datetime:
        return self.clock.now().astimezone(timezone.utc)

    def _now_str(self) -> str:
        return fmt_ts(self._now())

    def _id(self, value: str, field: str) -> str:
        value = str(value).strip()
        if not IDENTIFIER.fullmatch(value):
            raise ValidationError(f"{field} 格式无效")
        return value

    def _text(self, value: Any, field: str, limit: int = 500) -> str:
        value = str(value).strip()
        if not value or len(value) > limit:
            raise ValidationError(f"{field} 不能为空且不能超过 {limit} 个字符")
        return value

    def _json_hash(self, value: Any) -> str:
        return digest(value)

    def _replay_if_seen(self, connection, *, request_id: str, action: str,
                        payload: dict[str, Any]):
        """在任何业务状态校验之前回放已完成请求，保证重试永不产生副作用。"""

        request_id = self._id(request_id, "request_id")
        row = connection.execute(
            "SELECT resource_type, resource_id, payload_hash, action FROM eg_request_receipts"
            " WHERE request_id=?", (request_id,),
        ).fetchone()
        if row is None:
            return None
        if row["action"] != action or row["payload_hash"] != digest(payload):
            raise ConflictError("request_id 已被不同内容使用")
        return WriteReceipt(request_id, row["resource_type"], row["resource_id"], True)

    def _idempotent(self, connection, *, request_id: str, action: str,
                    payload: dict[str, Any],
                    create: Callable[[], tuple[str, str, dict[str, Any]]]):
        request_id = self._id(request_id, "request_id")
        payload_hash = digest(payload)
        row = connection.execute(
            "SELECT * FROM eg_request_receipts WHERE request_id=?", (request_id,)
        ).fetchone()
        if row:
            if row["action"] != action or row["payload_hash"] != payload_hash:
                raise ConflictError("request_id 已被不同内容使用")
            return WriteReceipt(request_id, row["resource_type"], row["resource_id"], True)
        resource_type, resource_id, response = create()
        connection.execute(
            "INSERT INTO eg_request_receipts(request_id,action,payload_hash,resource_type,"
            "resource_id,response_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (request_id, action, payload_hash, resource_type, resource_id,
             canonical_json(response), self._now_str()),
        )
        return WriteReceipt(request_id, resource_type, resource_id, False)

    def _resolve_caller(self, connection, actor_token: str):
        """返回 (token, staff_row|None)。内部账号优先，其次参与者令牌。"""

        token = str(actor_token or "").strip()
        if not token:
            raise PermissionDenied("缺少 X-Actor-Id 调用方标识")
        if len(token) > 120:
            raise ValidationError("X-Actor-Id 不能超过 120 个字符")
        row = connection.execute("SELECT * FROM actors WHERE actor_id=?", (token,)).fetchone()
        if row is not None:
            if not row["active"]:
                raise PermissionDenied("操作者已停用")
            return token, row
        if token.startswith("person:") or token.startswith("org:"):
            return token, None
        raise PermissionDenied("调用方既不是在岗工作人员，也不是 person:/org: 参与者")

    def _staff(self, staff, *roles: str) -> None:
        if staff is None:
            raise PermissionDenied("该动作仅工作人员可执行")
        if staff["role"] not in roles:
            raise PermissionDenied("当前角色不能执行该动作")

    def _participant_action_allowed(self, staff) -> None:
        """参赛者动作：参与者本人或 admin/operator 代办；reviewer/auditor 禁止。"""

        if staff is not None and staff["role"] not in ("admin", "operator"):
            raise PermissionDenied("审核与审计角色不能代替参赛者操作投稿")

    def _audit(self, connection, *, actor: str, action: str, resource_type: str,
               resource_id: str, detail: dict[str, Any]) -> None:
        append_event(connection, actor_id=actor, action=action, resource_type=resource_type,
                     resource_id=resource_id, detail=detail, occurred_at=self._now_str())

    def _append_event(self, connection, *, submission_id: str, event_type: str, actor: str,
                      detail: dict[str, Any], track_id: str | None = None,
                      version: int | None = None, occurred_at: datetime | None = None):
        occurred = occurred_at or self._now()
        seq_row = connection.execute(
            "SELECT COALESCE(MAX(seq),0)+1 AS seq FROM eg_submission_events WHERE submission_id=?",
            (submission_id,),
        ).fetchone()
        seq = seq_row["seq"]
        event_id = uuid.uuid4().hex
        connection.execute(
            "INSERT INTO eg_submission_events(event_id,submission_id,seq,event_type,occurred_at,"
            "actor,track_id,version,detail_json,detail_hash) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (event_id, submission_id, seq, event_type, fmt_ts(occurred), actor, track_id, version,
             canonical_json(detail), digest(detail)),
        )
        return {"event_id": event_id, "seq": seq, "occurred_at": fmt_ts(occurred)}

    # ------------------------------------------------------------------
    # 建档：自然人 / 工作室 / 关系 / 受益主体 / 团队
    # ------------------------------------------------------------------
    def register_person(self, *, request_id: str, actor_id: str, person_id: str,
                        legal_name: str, id_doc_hash: str, birth_date: str | None = None):
        payload = {"person_id": person_id, "legal_name": legal_name,
                   "id_doc_hash": id_doc_hash, "birth_date": birth_date}
        with self.database.transaction(immediate=True) as conn:
            _seen = self._replay_if_seen(conn, request_id=request_id,
                                               action="eg.register_person", payload=payload)
            if _seen is not None:
                return _seen
            token, staff = self._resolve_caller(conn, actor_id)
            if staff is None and token != f"person:{person_id}":
                raise PermissionDenied("只能为本人建档自然人身份")
            person_id = self._id(person_id, "person_id")
            legal_name = self._text(legal_name, "legal_name")
            id_doc_hash = self._text(id_doc_hash, "id_doc_hash", 128)
            if birth_date is not None:
                birth_date = self._text(birth_date, "birth_date", 10)
                try:
                    datetime.strptime(birth_date, "%Y-%m-%d")
                except ValueError as exc:
                    raise ValidationError("birth_date 需为 YYYY-MM-DD") from exc

            def create():
                try:
                    conn.execute(
                        "INSERT INTO eg_persons(person_id,legal_name,birth_date,id_doc_hash,created_at)"
                        " VALUES(?,?,?,?,?)",
                        (person_id, legal_name, birth_date, id_doc_hash, self._now_str()),
                    )
                except Exception as exc:
                    raise ConflictError("自然人编号或证件哈希已存在") from exc
                self._audit(conn, actor=token, action="eg.person.registered",
                            resource_type="person", resource_id=person_id,
                            detail={"legal_name": legal_name})
                return "person", person_id, {"person_id": person_id}

            return self._idempotent(conn, request_id=request_id, action="eg.register_person",
                                    payload=payload, create=create)

    def register_external_org(self, *, request_id: str, actor_id: str, external_org_id: str,
                              name: str, license_hash: str):
        payload = {"external_org_id": external_org_id, "name": name, "license_hash": license_hash}
        with self.database.transaction(immediate=True) as conn:
            _seen = self._replay_if_seen(conn, request_id=request_id,
                                               action="eg.register_org", payload=payload)
            if _seen is not None:
                return _seen
            token, staff = self._resolve_caller(conn, actor_id)
            if staff is None and token != f"org:{external_org_id}":
                raise PermissionDenied("只能以本工作室令牌建档")
            external_org_id = self._id(external_org_id, "external_org_id")
            name = self._text(name, "name")
            license_hash = self._text(license_hash, "license_hash", 128)

            def create():
                try:
                    conn.execute(
                        "INSERT INTO eg_external_orgs(external_org_id,name,license_hash,created_at)"
                        " VALUES(?,?,?,?)",
                        (external_org_id, name, license_hash, self._now_str()),
                    )
                except Exception as exc:
                    raise ConflictError("机构编号或证照哈希已存在") from exc
                self._audit(conn, actor=token, action="eg.org.registered",
                            resource_type="external_org", resource_id=external_org_id,
                            detail={"name": name})
                return "external_org", external_org_id, {"external_org_id": external_org_id}

            return self._idempotent(conn, request_id=request_id, action="eg.register_org",
                                    payload=payload, create=create)

    def add_relationship(self, *, request_id: str, actor_id: str, relationship_id: str,
                         kind: str, principal_type: str, principal_id: str,
                         agent_type: str, agent_id: str, document_hash: str,
                         valid_from: str, valid_to: str | None = None):
        """登记监护关系或授权代理关系。"""

        payload = {"relationship_id": relationship_id, "kind": kind,
                   "principal_type": principal_type, "principal_id": principal_id,
                   "agent_type": agent_type, "agent_id": agent_id,
                   "document_hash": document_hash, "valid_from": valid_from,
                   "valid_to": valid_to}
        with self.database.transaction(immediate=True) as conn:
            _seen = self._replay_if_seen(conn, request_id=request_id,
                                               action="eg.add_relationship", payload=payload)
            if _seen is not None:
                return _seen
            token, staff = self._resolve_caller(conn, actor_id)
            if staff is not None:
                self._staff(staff, "admin", "operator")
            elif token != f"{agent_type}:{agent_id}":
                raise PermissionDenied("非工作人员只能以代理方本人身份登记关系")
            relationship_id = self._id(relationship_id, "relationship_id")
            if kind not in ("guardian", "authorized_agent"):
                raise ValidationError("kind 只能是 guardian 或 authorized_agent")
            if principal_type not in ("person", "org") or agent_type not in ("person", "org"):
                raise ValidationError("主体/代理类型只能是 person 或 org")
            self._require_party(conn, principal_type, principal_id)
            self._require_party(conn, agent_type, agent_id)
            document_hash = self._text(document_hash, "document_hash", 128)
            start = parse_ts(valid_from, "valid_from")
            end = parse_ts(valid_to, "valid_to") if valid_to else None
            if end is not None and end <= start:
                raise ValidationError("valid_to 必须晚于 valid_from")

            def create():
                try:
                    conn.execute(
                        "INSERT INTO eg_relationships(relationship_id,kind,principal_type,"
                        "principal_person_id,principal_org_id,agent_type,agent_person_id,"
                        "agent_org_id,document_hash,valid_from,valid_to,created_at)"
                        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                        (relationship_id, kind, principal_type,
                         principal_id if principal_type == "person" else None,
                         principal_id if principal_type == "org" else None,
                         agent_type,
                         agent_id if agent_type == "person" else None,
                         agent_id if agent_type == "org" else None,
                         document_hash, fmt_ts(start),
                         fmt_ts(end) if end else None, self._now_str()),
                    )
                except Exception as exc:
                    raise ConflictError("关系编号已存在") from exc
                self._audit(conn, actor=token, action="eg.relationship.added",
                            resource_type="relationship", resource_id=relationship_id,
                            detail={"kind": kind, "principal": f"{principal_type}:{principal_id}",
                                    "agent": f"{agent_type}:{agent_id}"})
                return "relationship", relationship_id, {"relationship_id": relationship_id}

            return self._idempotent(conn, request_id=request_id, action="eg.add_relationship",
                                    payload=payload, create=create)

    def _require_party(self, conn, party_type: str, party_id: str) -> None:
        party_id = self._id(party_id, "party_id")
        table = "eg_persons" if party_type == "person" else "eg_external_orgs"
        column = "person_id" if party_type == "person" else "external_org_id"
        if conn.execute(f"SELECT 1 FROM {table} WHERE {column}=?", (party_id,)).fetchone() is None:
            raise NotFoundError(f"{party_type}:{party_id} 尚未建档")

    def _relationship_valid(self, conn, *, kind: str, principal_type: str, principal_id: str,
                            agent_type: str, agent_id: str, at: datetime) -> bool:
        row = conn.execute(
            "SELECT 1 FROM eg_relationships WHERE kind=? AND principal_type=? AND agent_type=? "
            "AND COALESCE(principal_person_id,'')=COALESCE(?, '') "
            "AND COALESCE(principal_org_id,'')=COALESCE(?, '') "
            "AND COALESCE(agent_person_id,'')=COALESCE(?, '') "
            "AND COALESCE(agent_org_id,'')=COALESCE(?, '') "
            "AND valid_from<=? AND (valid_to IS NULL OR valid_to>=?) AND revoked_at IS NULL",
            (kind, principal_type, agent_type,
             principal_id if principal_type == "person" else None,
             principal_id if principal_type == "org" else None,
             agent_id if agent_type == "person" else None,
             agent_id if agent_type == "org" else None,
             fmt_ts(at), fmt_ts(at)),
        ).fetchone()
        return row is not None

    def register_subject(self, *, request_id: str, actor_id: str, subject_id: str,
                         display_name: str):
        """登记受益创作主体集群（自然人、工作室、成员身份汇聚于此）。"""

        payload = {"subject_id": subject_id, "display_name": display_name}
        with self.database.transaction(immediate=True) as conn:
            _seen = self._replay_if_seen(conn, request_id=request_id,
                                               action="eg.register_subject", payload=payload)
            if _seen is not None:
                return _seen
            token, staff = self._resolve_caller(conn, actor_id)
            if staff is not None:
                self._staff(staff, "admin", "operator")
            subject_id = self._id(subject_id, "subject_id")
            display_name = self._text(display_name, "display_name")

            def create():
                try:
                    conn.execute(
                        "INSERT INTO eg_subjects(subject_id,display_name,created_at) VALUES(?,?,?)",
                        (subject_id, display_name, self._now_str()),
                    )
                except Exception as exc:
                    raise ConflictError("受益主体编号已存在") from exc
                self._audit(conn, actor=token, action="eg.subject.registered",
                            resource_type="subject", resource_id=subject_id,
                            detail={"display_name": display_name})
                return "subject", subject_id, {"subject_id": subject_id}

            return self._idempotent(conn, request_id=request_id, action="eg.register_subject",
                                    payload=payload, create=create)

    def link_subject_party(self, *, request_id: str, actor_id: str, subject_id: str,
                           party_type: str, party_id: str, link_role: str):
        """把自然人/工作室身份挂到受益主体上（self/studio/member）。"""

        payload = {"subject_id": subject_id, "party_type": party_type,
                   "party_id": party_id, "link_role": link_role}
        with self.database.transaction(immediate=True) as conn:
            _seen = self._replay_if_seen(conn, request_id=request_id,
                                               action="eg.link_subject_party", payload=payload)
            if _seen is not None:
                return _seen
            token, staff = self._resolve_caller(conn, actor_id)
            if staff is not None:
                self._staff(staff, "admin", "operator")
            subject_id = self._id(subject_id, "subject_id")
            if party_type not in ("person", "org"):
                raise ValidationError("party_type 只能是 person 或 org")
            if link_role not in ("self", "studio", "member"):
                raise ValidationError("link_role 只能是 self/studio/member")
            self._require_party(conn, party_type, party_id)
            if conn.execute("SELECT 1 FROM eg_subjects WHERE subject_id=?",
                            (subject_id,)).fetchone() is None:
                raise NotFoundError("受益主体不存在")

            def create():
                link_id = uuid.uuid4().hex
                try:
                    conn.execute(
                        "INSERT INTO eg_subject_links(link_id,subject_id,party_type,person_id,"
                        "external_org_id,link_role,active,created_at) VALUES(?,?,?,?,?,?,1,?)",
                        (link_id, subject_id, party_type,
                         party_id if party_type == "person" else None,
                         party_id if party_type == "org" else None,
                         link_role, self._now_str()),
                    )
                except Exception as exc:
                    raise ConflictError("该身份已活跃归属另一受益主体；如需并案请先归并") from exc
                self._audit(conn, actor=token, action="eg.subject.linked",
                            resource_type="subject", resource_id=subject_id,
                            detail={"party": f"{party_type}:{party_id}", "link_role": link_role})
                return "subject_link", link_id, {"link_id": link_id, "subject_id": subject_id}

            return self._idempotent(conn, request_id=request_id, action="eg.link_subject_party",
                                    payload=payload, create=create)

    def _subject_root(self, conn, subject_id: str) -> str:
        seen = set()
        current = subject_id
        while True:
            row = conn.execute("SELECT merged_into FROM eg_subjects WHERE subject_id=?",
                               (current,)).fetchone()
            if row is None:
                raise NotFoundError(f"受益主体 {current} 不存在")
            if row["merged_into"] is None:
                return current
            if current in seen:
                raise ConflictError("受益主体归并存在环")
            seen.add(current)
            current = row["merged_into"]

    def merge_subjects(self, *, request_id: str, actor_id: str, source_subject_id: str,
                       target_subject_id: str):
        """把两个受益主体集群并案（事后发现同一团队多重身份）。"""

        payload = {"source_subject_id": source_subject_id, "target_subject_id": target_subject_id}
        with self.database.transaction(immediate=True) as conn:
            _seen = self._replay_if_seen(conn, request_id=request_id,
                                               action="eg.merge_subjects", payload=payload)
            if _seen is not None:
                return _seen
            token, staff = self._resolve_caller(conn, actor_id)
            self._staff(staff, "admin", "operator")
            source = self._id(source_subject_id, "source_subject_id")
            target = self._id(target_subject_id, "target_subject_id")
            if source == target:
                raise ValidationError("不能归并到自身")
            source_root = self._subject_root(conn, source)
            target_root = self._subject_root(conn, target)
            if source_root == target_root:
                raise ConflictError("两个主体已在同一集群")

            def create():
                conn.execute("UPDATE eg_subjects SET merged_into=? WHERE subject_id=?",
                             (target_root, source_root))
                conn.execute(
                    "UPDATE eg_subject_links SET active=0 WHERE subject_id=?", (source_root,))
                conn.execute(
                    "INSERT INTO eg_subject_links(link_id,subject_id,party_type,person_id,"
                    "external_org_id,link_role,active,created_at) "
                    "SELECT lower(hex(randomblob(16))), ?, party_type, person_id, external_org_id,"
                    "'member', 1, ? FROM eg_subject_links WHERE subject_id=? AND active=0",
                    (target_root, self._now_str(), source_root),
                )
                self._audit(conn, actor=token, action="eg.subject.merged",
                            resource_type="subject", resource_id=target_root,
                            detail={"source": source_root, "target": target_root})
                return "subject", target_root, {"root_subject_id": target_root}

            return self._idempotent(conn, request_id=request_id, action="eg.merge_subjects",
                                    payload=payload, create=create)

    def register_team(self, *, request_id: str, actor_id: str, team_id: str, subject_id: str,
                      name: str, members: list[dict[str, str]]):
        """登记创作团队并批量记录加入历史。"""

        payload = {"team_id": team_id, "subject_id": subject_id, "name": name, "members": members}
        with self.database.transaction(immediate=True) as conn:
            _seen = self._replay_if_seen(conn, request_id=request_id,
                                               action="eg.register_team", payload=payload)
            if _seen is not None:
                return _seen
            token, staff = self._resolve_caller(conn, actor_id)
            if staff is not None:
                self._staff(staff, "admin", "operator")
            team_id = self._id(team_id, "team_id")
            subject_id = self._id(subject_id, "subject_id")
            name = self._text(name, "name")
            if not isinstance(members, list) or not members:
                raise ValidationError("members 必须是非空数组")
            normalized = []
            for item in members:
                pid = self._id(item.get("person_id", ""), "members[].person_id")
                role = self._text(item.get("role", ""), "members[].role", 80)
                self._require_party(conn, "person", pid)
                normalized.append((pid, role))
            root = self._subject_root(conn, subject_id)
            if staff is None:
                if not token.startswith("person:") or token.split(":", 1)[1] not in {p for p, _ in normalized}:
                    raise PermissionDenied("非工作人员建团队时本人必须在成员名单内")
            now = self._now_str()

            def create():
                try:
                    conn.execute(
                        "INSERT INTO eg_teams(team_id,subject_id,name,created_at) VALUES(?,?,?,?)",
                        (team_id, root, name, now),
                    )
                except Exception as exc:
                    raise ConflictError("团队编号已存在") from exc
                for pid, role in normalized:
                    conn.execute(
                        "INSERT INTO eg_team_members(team_id,person_id,role,active,joined_at)"
                        " VALUES(?,?,?,1,?)",
                        (team_id, pid, role, now),
                    )
                    conn.execute(
                        "INSERT INTO eg_team_member_history(history_id,team_id,person_id,"
                        "change_type,occurred_at,detail_json) VALUES(?,?,?,?,?,?)",
                        (uuid.uuid4().hex, team_id, pid, "joined", now,
                         canonical_json({"role": role})),
                    )
                self._audit(conn, actor=token, action="eg.team.registered",
                            resource_type="team", resource_id=team_id,
                            detail={"subject_id": root, "members": len(normalized)})
                return "team", team_id, {"team_id": team_id, "members": len(normalized)}

            return self._idempotent(conn, request_id=request_id, action="eg.register_team",
                                    payload=payload, create=create)

    def change_team_members(self, *, request_id: str, actor_id: str, team_id: str,
                            joins: list[dict[str, str]] | None = None,
                            leaves: list[str] | None = None):
        """成员加入/退出：写不可覆盖历史，并同步在窗口内的未冻结投稿事件。"""

        joins = joins or []
        leaves = leaves or []
        payload = {"team_id": team_id, "joins": joins, "leaves": leaves}
        with self.database.transaction(immediate=True) as conn:
            _seen = self._replay_if_seen(conn, request_id=request_id,
                                               action="eg.change_team_members", payload=payload)
            if _seen is not None:
                return _seen
            token, staff = self._resolve_caller(conn, actor_id)
            if staff is not None:
                self._staff(staff, "admin", "operator")
            team_id = self._id(team_id, "team_id")
            team = conn.execute("SELECT * FROM eg_teams WHERE team_id=?", (team_id,)).fetchone()
            if team is None:
                raise NotFoundError("团队不存在")
            if staff is None:
                if token.startswith("person:"):
                    caller = token.split(":", 1)[1]
                    membership = conn.execute(
                        "SELECT 1 FROM eg_team_members WHERE team_id=? AND person_id=? AND active=1",
                        (team_id, caller),
                    ).fetchone()
                    if membership is None:
                        raise PermissionDenied("只有团队现有成员可以变更名单")
                else:
                    raise PermissionDenied("工作室令牌不能变更团队名单")
            now = self._now()
            now_text = fmt_ts(now)
            join_norm, leave_norm = [], []
            for item in joins:
                pid = self._id(item.get("person_id", ""), "joins[].person_id")
                role = self._text(item.get("role", "成员"), "joins[].role", 80)
                self._require_party(conn, "person", pid)
                join_norm.append((pid, role))
            for pid in leaves:
                leave_norm.append(self._id(pid, "leaves[]"))

            open_submissions = conn.execute(
                "SELECT s.submission_id, w.closes_at, w.frozen FROM eg_submissions s "
                "JOIN eg_windows w ON w.window_id=s.window_id "
                "WHERE s.team_id=? AND s.status!='withdrawn'",
                (team_id,),
            ).fetchall()
            for sub in open_submissions:
                if sub["frozen"]:
                    raise FrozenWindowError("窗口已冻结，成员变更不能影响已锁定投稿")
                if now > parse_ts(sub["closes_at"], "closes_at"):
                    raise WindowClosedError("报名窗口已关闭，成员变更只能另案申诉")

            def create():
                changed = False
                for pid, role in join_norm:
                    existing = conn.execute(
                        "SELECT active FROM eg_team_members WHERE team_id=? AND person_id=?",
                        (team_id, pid),
                    ).fetchone()
                    if existing is None:
                        conn.execute(
                            "INSERT INTO eg_team_members(team_id,person_id,role,active,joined_at)"
                            " VALUES(?,?,?,1,?)",
                            (team_id, pid, role, now_text),
                        )
                    elif not existing["active"]:
                        conn.execute(
                            "UPDATE eg_team_members SET active=1, role=?, joined_at=?, left_at=NULL"
                            " WHERE team_id=? AND person_id=?",
                            (role, now_text, team_id, pid),
                        )
                    else:
                        continue
                    conn.execute(
                        "INSERT INTO eg_team_member_history(history_id,team_id,person_id,"
                        "change_type,occurred_at,detail_json) VALUES(?,?,?,?,?,?)",
                        (uuid.uuid4().hex, team_id, pid, "joined", now_text,
                         canonical_json({"role": role})),
                    )
                    changed = True
                for pid in leave_norm:
                    existing = conn.execute(
                        "SELECT active FROM eg_team_members WHERE team_id=? AND person_id=?",
                        (team_id, pid),
                    ).fetchone()
                    if existing is None or not existing["active"]:
                        raise NotFoundError(f"成员 {pid} 当前不在团队中")
                    conn.execute(
                        "UPDATE eg_team_members SET active=0, left_at=? WHERE team_id=? AND person_id=?",
                        (now_text, team_id, pid),
                    )
                    conn.execute(
                        "INSERT INTO eg_team_member_history(history_id,team_id,person_id,"
                        "change_type,occurred_at,detail_json) VALUES(?,?,?,?,?,?)",
                        (uuid.uuid4().hex, team_id, pid, "left", now_text,
                         canonical_json({})),
                    )
                    changed = True
                if not changed:
                    raise ValidationError("名单没有任何实际变化")
                active_members = conn.execute(
                    "SELECT person_id, role FROM eg_team_members WHERE team_id=? AND active=1"
                    " ORDER BY person_id", (team_id,),
                ).fetchall()
                snapshot = [{"person_id": r["person_id"], "role": r["role"]} for r in active_members]
                for sub in open_submissions:
                    self._append_event(conn, submission_id=sub["submission_id"],
                                       event_type="members_changed", actor=token,
                                       detail={"snapshot": snapshot}, occurred_at=now)
                self._audit(conn, actor=token, action="eg.team.members_changed",
                            resource_type="team", resource_id=team_id,
                            detail={"joins": [p for p, _ in join_norm],
                                    "leaves": leave_norm, "snapshot": snapshot})
                return "team", team_id, {"team_id": team_id, "active_members": snapshot}

            return self._idempotent(conn, request_id=request_id, action="eg.change_team_members",
                                    payload=payload, create=create)

    # ------------------------------------------------------------------
    # 赛道与窗口
    # ------------------------------------------------------------------
    def register_track(self, *, request_id: str, actor_id: str, track_id: str, name: str,
                       slots_total: int, exclusive_group: str | None = None):
        payload = {"track_id": track_id, "name": name, "slots_total": slots_total,
                   "exclusive_group": exclusive_group}
        with self.database.transaction(immediate=True) as conn:
            _seen = self._replay_if_seen(conn, request_id=request_id,
                                               action="eg.register_track", payload=payload)
            if _seen is not None:
                return _seen
            token, staff = self._resolve_caller(conn, actor_id)
            self._staff(staff, "admin", "operator")
            track_id = self._id(track_id, "track_id")
            name = self._text(name, "name")
            if not isinstance(slots_total, int) or slots_total < 1:
                raise ValidationError("slots_total 必须是正整数")
            if exclusive_group is not None:
                exclusive_group = self._id(exclusive_group, "exclusive_group")

            def create():
                try:
                    conn.execute(
                        "INSERT INTO eg_tracks(track_id,name,exclusive_group,slots_total,created_at)"
                        " VALUES(?,?,?,?,?)",
                        (track_id, name, exclusive_group, slots_total, self._now_str()),
                    )
                except Exception as exc:
                    raise ConflictError("赛道编号已存在") from exc
                self._audit(conn, actor=token, action="eg.track.registered",
                            resource_type="track", resource_id=track_id,
                            detail={"name": name, "slots_total": slots_total,
                                    "exclusive_group": exclusive_group})
                return "track", track_id, {"track_id": track_id, "slots_total": slots_total}

            return self._idempotent(conn, request_id=request_id, action="eg.register_track",
                                    payload=payload, create=create)

    def register_window(self, *, request_id: str, actor_id: str, window_id: str, name: str,
                        opens_at: str, closes_at: str, track_ids: list[str]):
        payload = {"window_id": window_id, "name": name, "opens_at": opens_at,
                   "closes_at": closes_at, "track_ids": track_ids}
        with self.database.transaction(immediate=True) as conn:
            _seen = self._replay_if_seen(conn, request_id=request_id,
                                               action="eg.register_window", payload=payload)
            if _seen is not None:
                return _seen
            token, staff = self._resolve_caller(conn, actor_id)
            self._staff(staff, "admin", "operator")
            window_id = self._id(window_id, "window_id")
            name = self._text(name, "name")
            opens = parse_ts(opens_at, "opens_at")
            closes = parse_ts(closes_at, "closes_at")
            if closes <= opens:
                raise ValidationError("closes_at 必须晚于 opens_at")
            if not isinstance(track_ids, list) or not track_ids:
                raise ValidationError("track_ids 必须是非空数组")
            track_ids = [self._id(t, "track_ids[]") for t in track_ids]
            for tid in track_ids:
                if conn.execute("SELECT 1 FROM eg_tracks WHERE track_id=?", (tid,)).fetchone() is None:
                    raise NotFoundError(f"赛道 {tid} 不存在")

            def create():
                try:
                    conn.execute(
                        "INSERT INTO eg_windows(window_id,name,opens_at,closes_at,frozen,created_at)"
                        " VALUES(?,?,?,?,0,?)",
                        (window_id, name, fmt_ts(opens), fmt_ts(closes), self._now_str()),
                    )
                except Exception as exc:
                    raise ConflictError("窗口编号已存在") from exc
                for tid in track_ids:
                    conn.execute(
                        "INSERT INTO eg_window_tracks(window_id,track_id) VALUES(?,?)",
                        (window_id, tid),
                    )
                self._audit(conn, actor=token, action="eg.window.registered",
                            resource_type="window", resource_id=window_id,
                            detail={"name": name, "opens_at": fmt_ts(opens),
                                    "closes_at": fmt_ts(closes), "tracks": track_ids})
                return "window", window_id, {"window_id": window_id, "track_ids": track_ids}

            return self._idempotent(conn, request_id=request_id, action="eg.register_window",
                                    payload=payload, create=create)

    def _load_window(self, conn, window_id: str):
        row = conn.execute("SELECT * FROM eg_windows WHERE window_id=?",
                           (window_id,)).fetchone()
        if row is None:
            raise NotFoundError("报名窗口不存在")
        return row

    def _require_open(self, window, at: datetime) -> None:
        if window["frozen"]:
            raise FrozenWindowError("窗口已冻结，原申请不得再改写")
        opens = parse_ts(window["opens_at"], "opens_at")
        closes = parse_ts(window["closes_at"], "closes_at")
        if at < opens:
            raise WindowClosedError("报名尚未开放")
        if at >= closes:
            raise WindowClosedError("报名已截止，新材料只能通过申诉提交")

    # ------------------------------------------------------------------
    # 投稿生命周期
    # ------------------------------------------------------------------
    def _is_minor(self, conn, person_id: str, at: datetime) -> bool:
        row = conn.execute("SELECT birth_date FROM eg_persons WHERE person_id=?",
                           (person_id,)).fetchone()
        if row is None or not row["birth_date"]:
            return False
        birth = datetime.strptime(row["birth_date"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        age = at.year - birth.year - ((at.month, at.day) < (birth.month, birth.day))
        return age < 18

    def _resolve_subject_for_party(self, conn, party_type: str, party_id: str) -> str | None:
        column = "person_id" if party_type == "person" else "external_org_id"
        row = conn.execute(
            "SELECT subject_id FROM eg_subject_links WHERE party_type=? AND "
            + column + "=? AND active=1",
            (party_type, party_id),
        ).fetchone()
        if row is None:
            return None
        return self._subject_root(conn, row["subject_id"])

    def _mutex_conflicts(self, conn, *, window_id: str, track_group: str | None,
                         subject_id: str, exclude_submission_id: str | None):
        if not track_group:
            return []
        sql = (
            "SELECT s.submission_id, s.subject_id, s.track_id, s.applicant_type, s.applicant_id,"
            " s.status FROM eg_submissions s JOIN eg_tracks t ON t.track_id=s.track_id "
            "WHERE s.window_id=? AND t.exclusive_group=? AND s.status!='withdrawn'"
        )
        params: list[Any] = [window_id, track_group]
        if exclude_submission_id:
            sql += " AND s.submission_id!=?"
            params.append(exclude_submission_id)
        conflicts = []
        for row in conn.execute(sql, params):
            root = self._subject_root(conn, row["subject_id"])
            if root == subject_id:
                conflicts.append({"submission_id": row["submission_id"],
                                  "track_id": row["track_id"],
                                  "applicant": f"{row['applicant_type']}:{row['applicant_id']}",
                                  "status": row["status"]})
        return conflicts

    def _track_occupancy(self, conn, window_id: str, track_id: str,
                         exclude_submission_id: str | None = None) -> int:
        sql = ("SELECT COUNT(*) AS c FROM eg_submissions WHERE window_id=? AND track_id=? "
               "AND status!='withdrawn'")
        params: list[Any] = [window_id, track_id]
        if exclude_submission_id:
            sql += " AND submission_id!=?"
            params.append(exclude_submission_id)
        return conn.execute(sql, params).fetchone()["c"]

    def submit(self, *, request_id: str, actor_id: str, window_id: str, track_id: str,
               applicant_type: str, applicant_id: str, title: str,
               content: dict[str, Any], represented_type: str | None = None,
               represented_id: str | None = None, guardian_person_id: str | None = None,
               team_id: str | None = None):
        """提交作品；同一 request_id 重放永远不产生第二份报名。"""

        payload = {"window_id": window_id, "track_id": track_id,
                   "applicant_type": applicant_type, "applicant_id": applicant_id,
                   "represented_type": represented_type, "represented_id": represented_id,
                   "guardian_person_id": guardian_person_id, "team_id": team_id,
                   "title": title, "content": content}
        with self.database.transaction(immediate=True) as conn:
            _seen = self._replay_if_seen(conn, request_id=request_id,
                                               action="eg.submit", payload=payload)
            if _seen is not None:
                return _seen
            token, staff = self._resolve_caller(conn, actor_id)
            self._participant_action_allowed(staff)
            now = self._now()
            window = self._load_window(conn, window_id)
            self._require_open(window, now)
            track_id = self._id(track_id, "track_id")
            track = conn.execute("SELECT * FROM eg_tracks WHERE track_id=?",
                                 (track_id,)).fetchone()
            if track is None:
                raise NotFoundError("赛道不存在")
            if conn.execute("SELECT 1 FROM eg_window_tracks WHERE window_id=? AND track_id=?",
                            (window_id, track_id)).fetchone() is None:
                raise ValidationError("该赛道不在本窗口开放")
            if applicant_type not in ("person", "org"):
                raise ValidationError("applicant_type 只能是 person 或 org")
            applicant_id = self._id(applicant_id, "applicant_id")
            self._require_party(conn, applicant_type, applicant_id)
            if not isinstance(content, dict) or not content:
                raise ValidationError("content 必须是非空对象")
            title = self._text(title, "title", 200)

            # 受益创作主体：被代表人（真实作者）优先，否则申请人本人。
            beneficiary_type, beneficiary_id = applicant_type, applicant_id
            if represented_type is not None:
                if represented_type not in ("person", "org"):
                    raise ValidationError("represented_type 只能是 person 或 org")
                represented_id = self._id(represented_id, "represented_id")
                self._require_party(conn, represented_type, represented_id)
                beneficiary_type, beneficiary_id = represented_type, represented_id

            subject_id: str | None = None
            if team_id is not None:
                team_id = self._id(team_id, "team_id")
                team = conn.execute("SELECT * FROM eg_teams WHERE team_id=?",
                                    (team_id,)).fetchone()
                if team is None:
                    raise NotFoundError("团队不存在")
                subject_id = self._subject_root(conn, team["subject_id"])
                if applicant_type != "person":
                    raise ValidationError("团队投稿须由一名自然人成员具名投递")
                if conn.execute(
                    "SELECT 1 FROM eg_team_members WHERE team_id=? AND person_id=? AND active=1",
                    (team_id, applicant_id),
                ).fetchone() is None:
                    raise PermissionDenied("具名投递人不是团队在册成员")
            else:
                subject_id = self._resolve_subject_for_party(conn, beneficiary_type,
                                                             beneficiary_id)
                if subject_id is None:
                    raise ValidationError(
                        "受益创作主体未建档关联：请先登记 subject 并挂接身份，"
                        "以便识别个人/工作室/代理人是否为同一主体")

            # 调用令牌与投递身份的授权关系。
            if staff is None:
                self._authorize_applicant(conn, token=token, applicant_type=applicant_type,
                                          applicant_id=applicant_id,
                                          represented_type=represented_type,
                                          represented_id=represented_id, at=now)

            # 未成年人的监护要求：申请人或受益作者为未成年自然人时须监护人具名附署。
            minor_person_id: str | None = None
            if represented_type == "person":
                minor_person_id = represented_id
            elif applicant_type == "person":
                minor_person_id = applicant_id
            if minor_person_id and self._is_minor(conn, minor_person_id, now):
                if not guardian_person_id:
                    raise ValidationError("未成年作者必须由监护人具名附署")
                guardian_person_id = self._id(guardian_person_id, "guardian_person_id")
                self._require_party(conn, "person", guardian_person_id)
                if not self._relationship_valid(
                        conn, kind="guardian", principal_type="person",
                        principal_id=minor_person_id, agent_type="person",
                        agent_id=guardian_person_id, at=now):
                    raise PermissionDenied("监护关系未登记或已失效")

            # 授权代理关系（工作室/代理人代真实作者投递）；监护代投可豁免。
            if represented_type is not None:
                guardian_ok = (
                    applicant_type == "person" and represented_type == "person"
                    and guardian_person_id is not None
                    and self._relationship_valid(
                        conn, kind="guardian", principal_type="person",
                        principal_id=represented_id, agent_type="person",
                        agent_id=applicant_id, at=now))
                agent_ok = self._relationship_valid(
                    conn, kind="authorized_agent", principal_type=represented_type,
                    principal_id=represented_id, agent_type=applicant_type,
                    agent_id=applicant_id, at=now)
                if not guardian_ok and not agent_ok:
                    raise PermissionDenied("缺少有效期内的授权代理关系或监护关系")

            # 互斥资格：同一受益主体不得借关联身份在互斥赛道组重复占位。
            conflicts = self._mutex_conflicts(
                conn, window_id=window_id, track_group=track["exclusive_group"],
                subject_id=subject_id, exclude_submission_id=None)
            if conflicts:
                raise EligibilityConflictError(
                    "同一受益创作主体已在该互斥赛道组持有有效投稿，不得用关联身份重复占位",
                    conflicts=conflicts)

            # 名额。
            if self._track_occupancy(conn, window_id, track_id) >= track["slots_total"]:
                raise QuotaExhaustedError("该赛道名额已满")

            def create():
                submission_id = uuid.uuid4().hex
                now_text = fmt_ts(now)
                content_hash = digest(content)
                conn.execute(
                    "INSERT INTO eg_submissions(submission_id,window_id,track_id,applicant_type,"
                    "applicant_id,represented_type,represented_id,guardian_person_id,team_id,"
                    "subject_id,status,current_version,created_at,updated_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,1,?,?)",
                    (submission_id, window_id, track_id, applicant_type, applicant_id,
                     represented_type, represented_id, guardian_person_id, team_id, subject_id,
                     "in_review", now_text, now_text),
                )
                parties = [(applicant_type, applicant_id, "applicant")]
                if represented_id:
                    parties.append((represented_type, represented_id, "represented"))
                if guardian_person_id:
                    parties.append(("person", guardian_person_id, "guardian"))
                if team_id:
                    for row in conn.execute(
                            "SELECT person_id FROM eg_team_members WHERE team_id=? AND active=1",
                            (team_id,)):
                        parties.append(("person", row["person_id"], "team_member"))
                for ptype, pid, prole in parties:
                    conn.execute(
                        "INSERT OR IGNORE INTO eg_submission_parties(submission_id,party_type,"
                        "party_id,party_role) VALUES(?,?,?,?)",
                        (submission_id, ptype, pid, prole),
                    )
                conn.execute(
                    "INSERT INTO eg_work_versions(submission_id,version,title,content_json,"
                    "content_hash,introduced_by_event,created_at) VALUES(?,?,?,?,?,?,?)",
                    (submission_id, 1, title, canonical_json(content), content_hash,
                     "submitted", now_text),
                )
                conn.execute(
                    "INSERT INTO eg_review_tasks(task_id,submission_id,status,reviewer_id,"
                    "opened_at,updated_at) VALUES(?,?, 'open',NULL,?,?)",
                    (uuid.uuid4().hex, submission_id, now_text, now_text),
                )
                event = self._append_event(
                    conn, submission_id=submission_id, event_type="submitted", actor=token,
                    track_id=track_id, version=1, occurred_at=now,
                    detail={"title": title, "content_hash": content_hash,
                            "window_id": window_id,
                            "applicant": f"{applicant_type}:{applicant_id}",
                            "represented": (f"{represented_type}:{represented_id}"
                                            if represented_id else None),
                            "guardian_person_id": guardian_person_id,
                            "team_id": team_id, "subject_id": subject_id})
                conn.execute(
                    "UPDATE eg_work_versions SET introduced_by_event=? WHERE submission_id=? "
                    "AND version=1", (event["event_id"], submission_id))
                self._audit(conn, actor=token, action="eg.submission.submitted",
                            resource_type="submission", resource_id=submission_id,
                            detail={"window_id": window_id, "track_id": track_id,
                                    "subject_id": subject_id, "event_seq": event["seq"]})
                return "submission", submission_id, {"submission_id": submission_id, "version": 1}

            return self._idempotent(conn, request_id=request_id, action="eg.submit",
                                    payload=payload, create=create)

    def _authorize_applicant(self, conn, *, token: str, applicant_type: str, applicant_id: str,
                             represented_type: str | None, represented_id: str | None,
                             at: datetime) -> None:
        if token == f"{applicant_type}:{applicant_id}":
            return
        if token.startswith("person:"):
            caller = token.split(":", 1)[1]
            # 监护人代未成年作者。
            if applicant_type == "person" and self._relationship_valid(
                    conn, kind="guardian", principal_type="person",
                    principal_id=applicant_id, agent_type="person", agent_id=caller, at=at):
                return
            # 自然人代理人代申请人。
            if self._relationship_valid(
                    conn, kind="authorized_agent", principal_type=applicant_type,
                    principal_id=applicant_id, agent_type="person", agent_id=caller, at=at):
                return
        if token.startswith("org:"):
            caller = token.split(":", 1)[1]
            if applicant_type == "org" and applicant_id == caller:
                return
        raise PermissionDenied("调用令牌与投递身份不匹配，且无监护/授权代理关系")

    def _load_owned_submission(self, conn, submission_id: str, token: str | None,
                               staff, *, allow_party_roles=("applicant", "represented",
                                                            "guardian")):
        submission_id = self._id(submission_id, "submission_id")
        sub = conn.execute("SELECT * FROM eg_submissions WHERE submission_id=?",
                           (submission_id,)).fetchone()
        if sub is None:
            raise NotFoundError("投稿不存在")
        self._participant_action_allowed(staff)
        if staff is None:
            if token.startswith("person:"):
                pid = token.split(":", 1)[1]
                row = conn.execute(
                    "SELECT 1 FROM eg_submission_parties WHERE submission_id=? AND party_type='person'"
                    " AND party_id=? AND party_role IN (%s)" % ",".join("?" * len(allow_party_roles)),
                    (submission_id, pid, *allow_party_roles),
                ).fetchone()
                if row is None and sub["team_id"]:
                    row = conn.execute(
                        "SELECT 1 FROM eg_team_members WHERE team_id=? AND person_id=? AND active=1",
                        (sub["team_id"], pid),
                    ).fetchone()
                if row is None:
                    raise PermissionDenied("只能操作与本人身份相关的投稿")
            else:
                oid = token.split(":", 1)[1]
                row = conn.execute(
                    "SELECT 1 FROM eg_submission_parties WHERE submission_id=? AND party_type='org'"
                    " AND party_id=? AND party_role IN (%s)" % ",".join("?" * len(allow_party_roles)),
                    (submission_id, oid, *allow_party_roles),
                ).fetchone()
                if row is None:
                    raise PermissionDenied("只能操作与本工作室相关的投稿")
        return sub

    def correct(self, *, request_id: str, actor_id: str, submission_id: str,
                title: str, content: dict[str, Any]):
        """在窗口（及补正期限）内提交新版本，版本只增不改。"""

        payload = {"submission_id": submission_id, "title": title, "content": content}
        with self.database.transaction(immediate=True) as conn:
            _seen = self._replay_if_seen(conn, request_id=request_id,
                                               action="eg.correct", payload=payload)
            if _seen is not None:
                return _seen
            token, staff = self._resolve_caller(conn, actor_id)
            sub = self._load_owned_submission(conn, submission_id, token, staff)
            now = self._now()
            window = self._load_window(conn, sub["window_id"])
            self._require_open(window, now)
            if sub["status"] not in ("in_review", "awaiting_correction"):
                raise ConflictError(f"当前状态 {sub['status']} 不允许补正")
            if sub["status"] == "awaiting_correction" and sub["correction_due_at"]:
                if now > parse_ts(sub["correction_due_at"], "correction_due_at"):
                    raise WindowClosedError("补正期限已过，迟到材料请通过申诉提交")
            title = self._text(title, "title", 200)
            if not isinstance(content, dict) or not content:
                raise ValidationError("content 必须是非空对象")
            new_version = sub["current_version"] + 1

            def create():
                now_text = fmt_ts(now)
                content_hash = digest(content)
                conn.execute(
                    "INSERT INTO eg_work_versions(submission_id,version,title,content_json,"
                    "content_hash,introduced_by_event,created_at) VALUES(?,?,?,?,?,?,?)",
                    (sub["submission_id"], new_version, title, canonical_json(content),
                     content_hash, "correction_submitted", now_text),
                )
                conn.execute(
                    "UPDATE eg_submissions SET current_version=?, status='in_review', "
                    "correction_due_at=NULL, updated_at=? WHERE submission_id=?",
                    (new_version, now_text, sub["submission_id"]),
                )
                event = self._append_event(
                    conn, submission_id=sub["submission_id"], event_type="correction_submitted",
                    actor=token, version=new_version, occurred_at=now,
                    detail={"title": title, "content_hash": content_hash})
                conn.execute(
                    "UPDATE eg_work_versions SET introduced_by_event=? WHERE submission_id=? "
                    "AND version=?", (event["event_id"], sub["submission_id"], new_version))
                conn.execute(
                    "UPDATE eg_review_tasks SET status='open', updated_at=? WHERE submission_id=?",
                    (now_text, sub["submission_id"]),
                )
                self._audit(conn, actor=token, action="eg.submission.corrected",
                            resource_type="submission", resource_id=sub["submission_id"],
                            detail={"version": new_version, "event_seq": event["seq"]})
                return "submission", sub["submission_id"], {
                    "submission_id": sub["submission_id"], "version": new_version}

            return self._idempotent(conn, request_id=request_id, action="eg.correct",
                                    payload=payload, create=create)

    def withdraw(self, *, request_id: str, actor_id: str, submission_id: str,
                 reason: str = ""):
        payload = {"submission_id": submission_id, "reason": reason}
        with self.database.transaction(immediate=True) as conn:
            _seen = self._replay_if_seen(conn, request_id=request_id,
                                               action="eg.withdraw", payload=payload)
            if _seen is not None:
                return _seen
            token, staff = self._resolve_caller(conn, actor_id)
            sub = self._load_owned_submission(conn, submission_id, token, staff)
            window = self._load_window(conn, sub["window_id"])
            now = self._now()
            self._require_open(window, now)
            if sub["status"] == "withdrawn":
                raise ConflictError("投稿已经撤回")

            def create():
                now_text = fmt_ts(now)
                conn.execute(
                    "UPDATE eg_submissions SET status='withdrawn', updated_at=? WHERE submission_id=?",
                    (now_text, sub["submission_id"]),
                )
                event = self._append_event(
                    conn, submission_id=sub["submission_id"], event_type="withdrawn",
                    actor=token, occurred_at=now, detail={"reason": reason})
                conn.execute(
                    "UPDATE eg_review_tasks SET status='closed_withdrawn', updated_at=? "
                    "WHERE submission_id=?", (now_text, sub["submission_id"]),
                )
                self._audit(conn, actor=token, action="eg.submission.withdrawn",
                            resource_type="submission", resource_id=sub["submission_id"],
                            detail={"event_seq": event["seq"]})
                return "submission", sub["submission_id"], {"submission_id": sub["submission_id"]}

            return self._idempotent(conn, request_id=request_id, action="eg.withdraw",
                                    payload=payload, create=create)

    def transfer(self, *, request_id: str, actor_id: str, submission_id: str,
                 target_track_id: str):
        """窗口开放期间换赛道；原赛道名额原子释放、新赛道原子占用。"""

        payload = {"submission_id": submission_id, "target_track_id": target_track_id}
        with self.database.transaction(immediate=True) as conn:
            _seen = self._replay_if_seen(conn, request_id=request_id,
                                               action="eg.transfer", payload=payload)
            if _seen is not None:
                return _seen
            token, staff = self._resolve_caller(conn, actor_id)
            sub = self._load_owned_submission(conn, submission_id, token, staff)
            window = self._load_window(conn, sub["window_id"])
            now = self._now()
            self._require_open(window, now)
            if sub["status"] == "withdrawn":
                raise ConflictError("已撤回投稿不能换赛道")
            target_track_id = self._id(target_track_id, "target_track_id")
            if target_track_id == sub["track_id"]:
                raise ValidationError("目标赛道与当前赛道相同")
            target = conn.execute("SELECT * FROM eg_tracks WHERE track_id=?",
                                  (target_track_id,)).fetchone()
            if target is None:
                raise NotFoundError("目标赛道不存在")
            if conn.execute("SELECT 1 FROM eg_window_tracks WHERE window_id=? AND track_id=?",
                            (sub["window_id"], target_track_id)).fetchone() is None:
                raise ValidationError("目标赛道不在本窗口开放")
            subject_id = self._subject_root(conn, sub["subject_id"])
            conflicts = self._mutex_conflicts(
                conn, window_id=sub["window_id"], track_group=target["exclusive_group"],
                subject_id=subject_id, exclude_submission_id=sub["submission_id"])
            if conflicts:
                raise EligibilityConflictError(
                    "换赛道后同一受益主体仍会在互斥赛道组重复占位", conflicts=conflicts)
            if self._track_occupancy(conn, sub["window_id"], target_track_id,
                                     sub["submission_id"]) >= target["slots_total"]:
                raise QuotaExhaustedError("目标赛道名额已满")

            def create():
                now_text = fmt_ts(now)
                conn.execute(
                    "UPDATE eg_submissions SET track_id=?, updated_at=? WHERE submission_id=?",
                    (target_track_id, now_text, sub["submission_id"]),
                )
                event = self._append_event(
                    conn, submission_id=sub["submission_id"], event_type="transferred",
                    actor=token, track_id=target_track_id, occurred_at=now,
                    detail={"from_track_id": sub["track_id"], "to_track_id": target_track_id})
                self._audit(conn, actor=token, action="eg.submission.transferred",
                            resource_type="submission", resource_id=sub["submission_id"],
                            detail={"from": sub["track_id"], "to": target_track_id,
                                    "event_seq": event["seq"]})
                return "submission", sub["submission_id"], {
                    "submission_id": sub["submission_id"], "track_id": target_track_id}

            return self._idempotent(conn, request_id=request_id, action="eg.transfer",
                                    payload=payload, create=create)

    # ------------------------------------------------------------------
    # 评审
    # ------------------------------------------------------------------
    def request_correction(self, *, request_id: str, actor_id: str, submission_id: str,
                           note: str, correction_due_at: str):
        payload = {"submission_id": submission_id, "note": note,
                   "correction_due_at": correction_due_at}
        with self.database.transaction(immediate=True) as conn:
            _seen = self._replay_if_seen(conn, request_id=request_id,
                                               action="eg.request_correction", payload=payload)
            if _seen is not None:
                return _seen
            token, staff = self._resolve_caller(conn, actor_id)
            self._staff(staff, "reviewer", "admin")
            submission_id = self._id(submission_id, "submission_id")
            sub = conn.execute("SELECT * FROM eg_submissions WHERE submission_id=?",
                               (submission_id,)).fetchone()
            if sub is None:
                raise NotFoundError("投稿不存在")
            if sub["status"] not in ("in_review", "awaiting_correction"):
                raise ConflictError(f"当前状态 {sub['status']} 不能要求补正")
            window = self._load_window(conn, sub["window_id"])
            due = parse_ts(correction_due_at, "correction_due_at")
            closes = parse_ts(window["closes_at"], "closes_at")
            if due <= self._now():
                raise ValidationError("补正期限必须晚于当前时间")
            if due > closes:
                raise ValidationError("补正期限不能晚于窗口截止时间")
            note = self._text(note, "note", 1000)
            now = self._now()

            def create():
                now_text = fmt_ts(now)
                conn.execute(
                    "UPDATE eg_submissions SET status='awaiting_correction', "
                    "correction_due_at=?, updated_at=? WHERE submission_id=?",
                    (fmt_ts(due), now_text, submission_id),
                )
                event = self._append_event(
                    conn, submission_id=submission_id, event_type="review_decided",
                    actor=token, occurred_at=now,
                    detail={"decision": "correction_requested", "note": note,
                            "correction_due_at": fmt_ts(due)})
                conn.execute(
                    "UPDATE eg_review_tasks SET status='waiting_correction', reviewer_id=?, "
                    "updated_at=? WHERE submission_id=?",
                    (staff["actor_id"], now_text, submission_id),
                )
                self._audit(conn, actor=token, action="eg.review.correction_requested",
                            resource_type="submission", resource_id=submission_id,
                            detail={"due": fmt_ts(due), "event_seq": event["seq"]})
                return "submission", submission_id, {"submission_id": submission_id,
                                                      "status": "awaiting_correction"}

            return self._idempotent(conn, request_id=request_id,
                                    action="eg.request_correction",
                                    payload=payload, create=create)

    def decide(self, *, request_id: str, actor_id: str, submission_id: str,
               decision: str, reason: str):
        """资格审核结论：通过 / 拒绝。评审在冻结前后均可基于锁定版本继续。"""

        payload = {"submission_id": submission_id, "decision": decision, "reason": reason}
        with self.database.transaction(immediate=True) as conn:
            _seen = self._replay_if_seen(conn, request_id=request_id,
                                               action="eg.decide", payload=payload)
            if _seen is not None:
                return _seen
            token, staff = self._resolve_caller(conn, actor_id)
            self._staff(staff, "reviewer", "admin")
            submission_id = self._id(submission_id, "submission_id")
            sub = conn.execute("SELECT * FROM eg_submissions WHERE submission_id=?",
                               (submission_id,)).fetchone()
            if sub is None:
                raise NotFoundError("投稿不存在")
            if decision not in ("accepted", "rejected"):
                raise ValidationError("decision 只能是 accepted 或 rejected")
            if sub["status"] == "withdrawn":
                raise ConflictError("已撤回投稿不能作出评审结论")
            reason = self._text(reason, "reason", 2000)
            now = self._now()

            def create():
                now_text = fmt_ts(now)
                conn.execute(
                    "UPDATE eg_submissions SET status=?, updated_at=? WHERE submission_id=?",
                    (decision, now_text, submission_id),
                )
                event = self._append_event(
                    conn, submission_id=submission_id, event_type="review_decided",
                    actor=token, occurred_at=now,
                    detail={"decision": decision, "reason": reason,
                            "effective_version_at_review": sub["effective_version"],
                            "current_version": sub["current_version"]})
                conn.execute(
                    "UPDATE eg_review_tasks SET status='decided', reviewer_id=?, updated_at=? "
                    "WHERE submission_id=?", (staff["actor_id"], now_text, submission_id),
                )
                self._audit(conn, actor=token, action="eg.review.decided",
                            resource_type="submission", resource_id=submission_id,
                            detail={"decision": decision, "event_seq": event["seq"]})
                return "submission", submission_id, {"submission_id": submission_id,
                                                      "status": decision}

            return self._idempotent(conn, request_id=request_id, action="eg.decide",
                                    payload=payload, create=create)

    def review_tasks(self, actor_id: str, status: str | None = None) -> list[dict[str, Any]]:
        """审核员的待办队列；状态持久化，重启后未完成任务仍在。"""

        with self.database.transaction() as conn:
            _token, staff = self._resolve_caller(conn, actor_id)
            self._staff(staff, "reviewer", "admin", "operator", "auditor")
            sql = ("SELECT t.task_id, t.submission_id, t.status, t.reviewer_id, t.opened_at,"
                   " t.updated_at, s.window_id, s.track_id, s.current_version,"
                   " s.effective_version, s.status AS submission_status"
                   " FROM eg_review_tasks t JOIN eg_submissions s ON s.submission_id=t.submission_id")
            params: list[Any] = []
            if status:
                sql += " WHERE t.status=?"
                params.append(status)
            sql += " ORDER BY t.opened_at, t.task_id"
            return [dict(row) for row in conn.execute(sql, params)]

    # ------------------------------------------------------------------
    # 截止冻结
    # ------------------------------------------------------------------
    def freeze_window(self, *, request_id: str, actor_id: str, window_id: str):
        """截止时原子冻结：每个未撤回投稿锁定生效版本，事件逐件留痕。"""

        payload = {"window_id": window_id}
        with self.database.transaction(immediate=True) as conn:
            _seen = self._replay_if_seen(conn, request_id=request_id,
                                               action="eg.freeze_window", payload=payload)
            if _seen is not None:
                return _seen
            token, staff = self._resolve_caller(conn, actor_id)
            self._staff(staff, "admin", "operator")
            window_id = self._id(window_id, "window_id")
            window = self._load_window(conn, window_id)
            if window["frozen"]:
                raise FrozenWindowError("窗口已经冻结，冻结不可覆盖")
            now = self._now()
            if now < parse_ts(window["closes_at"], "closes_at"):
                raise WindowClosedError("尚未到达截止时间，不能提前冻结")

            def create():
                now_text = fmt_ts(now)
                conn.execute(
                    "UPDATE eg_windows SET frozen=1, frozen_at=? WHERE window_id=?",
                    (now_text, window_id),
                )
                rows = conn.execute(
                    "SELECT * FROM eg_submissions WHERE window_id=? AND status!='withdrawn'",
                    (window_id,),
                ).fetchall()
                frozen = []
                for sub in rows:
                    effective = sub["current_version"]
                    conn.execute(
                        "UPDATE eg_submissions SET effective_version=?, frozen_at=?, updated_at=?"
                        " WHERE submission_id=?",
                        (effective, now_text, now_text, sub["submission_id"]),
                    )
                    event = self._append_event(
                        conn, submission_id=sub["submission_id"], event_type="frozen",
                        actor=token, occurred_at=now,
                        detail={"window_id": window_id, "effective_version": effective,
                                "prior_status": sub["status"]})
                    frozen.append({"submission_id": sub["submission_id"],
                                   "effective_version": effective, "event_seq": event["seq"]})
                self._audit(conn, actor=token, action="eg.window.frozen",
                            resource_type="window", resource_id=window_id,
                            detail={"frozen_submissions": len(frozen)})
                return "window", window_id, {"window_id": window_id, "frozen": frozen}

            return self._idempotent(conn, request_id=request_id, action="eg.freeze_window",
                                    payload=payload, create=create)

    # ------------------------------------------------------------------
    # 申诉（迟到材料的唯一合法入口）
    # ------------------------------------------------------------------
    def file_appeal(self, *, request_id: str, actor_id: str, submission_id: str,
                    reason: str, evidence: dict[str, Any] | None = None,
                    late_title: str | None = None,
                    late_content: dict[str, Any] | None = None):
        """窗口截止/冻结后提交的材料只进入申诉，绝不回写原申请。"""

        payload = {"submission_id": submission_id, "reason": reason, "evidence": evidence,
                   "late_title": late_title, "late_content": late_content}
        with self.database.transaction(immediate=True) as conn:
            _seen = self._replay_if_seen(conn, request_id=request_id,
                                               action="eg.file_appeal", payload=payload)
            if _seen is not None:
                return _seen
            token, staff = self._resolve_caller(conn, actor_id)
            submission_id = self._id(submission_id, "submission_id")
            sub = self._load_owned_submission(conn, submission_id, token, staff)
            window = self._load_window(conn, sub["window_id"])
            now = self._now()
            if now < parse_ts(window["closes_at"], "closes_at"):
                raise ConflictError("窗口尚未截止，应使用正常补正通道")
            reason = self._text(reason, "reason", 2000)
            evidence = evidence or {}
            if not isinstance(evidence, dict):
                raise ValidationError("evidence 必须是对象")
            late_snapshot = None
            late_version = None
            if late_content is not None:
                if not isinstance(late_content, dict) or not late_content:
                    raise ValidationError("late_content 必须是非空对象")
                late_title = self._text(late_title or "", "late_title", 200)
                late_version = sub["current_version"] + 1
                late_snapshot = {"title": late_title, "content": late_content}

            def create():
                now_text = fmt_ts(now)
                appeal_id = uuid.uuid4().hex
                evidence_blob = {"evidence": evidence, "late_work": late_snapshot}
                conn.execute(
                    "INSERT INTO eg_appeals(appeal_id,submission_id,window_id,reason,"
                    "evidence_json,evidence_hash,late_version,status,filed_by,filed_at)"
                    " VALUES(?,?,?,?,?,?,?, 'pending',?,?)",
                    (appeal_id, submission_id, sub["window_id"], reason,
                     canonical_json(evidence_blob), digest(evidence_blob), late_version,
                     token, now_text),
                )
                event = self._append_event(
                    conn, submission_id=submission_id, event_type="appeal_filed",
                    actor=token, occurred_at=now,
                    detail={"appeal_id": appeal_id, "reason": reason,
                            "late_version": late_version,
                            "late_work_hash": digest(late_snapshot) if late_snapshot else None,
                            "original_effective_version": sub["effective_version"]})
                self._audit(conn, actor=token, action="eg.appeal.filed",
                            resource_type="appeal", resource_id=appeal_id,
                            detail={"submission_id": submission_id,
                                    "late_version": late_version, "event_seq": event["seq"]})
                return "appeal", appeal_id, {"appeal_id": appeal_id,
                                              "late_version": late_version}

            return self._idempotent(conn, request_id=request_id, action="eg.file_appeal",
                                    payload=payload, create=create)

    def decide_appeal(self, *, request_id: str, actor_id: str, appeal_id: str,
                      decision: str, decision_note: str):
        """采纳申诉只设置独立的申诉生效版本号，原冻结生效版本保持不动。"""

        payload = {"appeal_id": appeal_id, "decision": decision,
                   "decision_note": decision_note}
        with self.database.transaction(immediate=True) as conn:
            _seen = self._replay_if_seen(conn, request_id=request_id,
                                               action="eg.decide_appeal", payload=payload)
            if _seen is not None:
                return _seen
            token, staff = self._resolve_caller(conn, actor_id)
            self._staff(staff, "reviewer", "admin")
            appeal_id = self._id(appeal_id, "appeal_id")
            appeal = conn.execute("SELECT * FROM eg_appeals WHERE appeal_id=?",
                                  (appeal_id,)).fetchone()
            if appeal is None:
                raise NotFoundError("申诉不存在")
            if appeal["status"] != "pending":
                raise ConflictError("申诉已经作出结论")
            if decision not in ("accepted", "rejected"):
                raise ValidationError("decision 只能是 accepted 或 rejected")
            decision_note = self._text(decision_note, "decision_note", 2000)
            now = self._now()

            def create():
                now_text = fmt_ts(now)
                conn.execute(
                    "UPDATE eg_appeals SET status=?, decided_by=?, decided_at=?, decision_note=?"
                    " WHERE appeal_id=?",
                    (decision, staff["actor_id"], now_text, decision_note, appeal_id),
                )
                sub_id = appeal["submission_id"]
                sub = conn.execute("SELECT * FROM eg_submissions WHERE submission_id=?",
                                   (sub_id,)).fetchone()
                detail = {"appeal_id": appeal_id, "decision": decision,
                          "decision_note": decision_note,
                          "frozen_effective_version": sub["effective_version"],
                          "late_version": appeal["late_version"]}
                if decision == "accepted" and appeal["late_version"]:
                    conn.execute(
                        "UPDATE eg_submissions SET appeal_effective_version=?, updated_at=? "
                        "WHERE submission_id=?",
                        (appeal["late_version"], now_text, sub_id),
                    )
                event = self._append_event(
                    conn, submission_id=sub_id, event_type="appeal_decided", actor=token,
                    occurred_at=now, detail=detail)
                self._audit(conn, actor=token, action="eg.appeal.decided",
                            resource_type="appeal", resource_id=appeal_id,
                            detail={"submission_id": sub_id, "decision": decision,
                                    "event_seq": event["seq"]})
                return "appeal", appeal_id, {"appeal_id": appeal_id, "decision": decision}

            return self._idempotent(conn, request_id=request_id, action="eg.decide_appeal",
                                    payload=payload, create=create)

    def list_appeals(self, actor_id: str, status: str | None = None) -> list[dict[str, Any]]:
        with self.database.transaction() as conn:
            _token, staff = self._resolve_caller(conn, actor_id)
            self._staff(staff, "reviewer", "admin", "auditor")
            sql = ("SELECT appeal_id,submission_id,window_id,reason,late_version,status,"
                   "filed_by,filed_at,decided_by,decided_at,decision_note,evidence_hash"
                   " FROM eg_appeals")
            params: list[Any] = []
            if status:
                sql += " WHERE status=?"
                params.append(status)
            sql += " ORDER BY filed_at, appeal_id"
            return [dict(row) for row in conn.execute(sql, params)]

    # ------------------------------------------------------------------
    # 查询：材料、冲突依据、历史时点解释
    # ------------------------------------------------------------------
    def _caller_party_submission_ids(self, conn, token: str) -> set[str]:
        ids: set[str] = set()
        if token.startswith("person:"):
            pid = token.split(":", 1)[1]
            for row in conn.execute(
                    "SELECT DISTINCT submission_id FROM eg_submission_parties WHERE party_type='person'"
                    " AND party_id=?", (pid,)):
                ids.add(row["submission_id"])
            for row in conn.execute(
                    "SELECT s.submission_id FROM eg_submissions s JOIN eg_team_members m"
                    " ON m.team_id=s.team_id WHERE m.person_id=? AND m.active=1", (pid,)):
                ids.add(row["submission_id"])
        elif token.startswith("org:"):
            oid = token.split(":", 1)[1]
            for row in conn.execute(
                    "SELECT DISTINCT submission_id FROM eg_submission_parties WHERE party_type='org'"
                    " AND party_id=?", (oid,)):
                ids.add(row["submission_id"])
        return ids

    def list_submissions(self, actor_id: str, *, window_id: str | None = None,
                         status_filter: str | None = None) -> dict[str, Any]:
        """按角色返回可见投稿；参与者只能看到与自己身份相关的件。"""

        with self.database.transaction() as conn:
            token, staff = self._resolve_caller(conn, actor_id)
            sql = "SELECT * FROM eg_submissions"
            clauses, params = [], []
            if window_id:
                clauses.append("window_id=?")
                params.append(window_id)
            if status_filter:
                clauses.append("status=?")
                params.append(status_filter)
            if staff is None:
                visible = self._caller_party_submission_ids(conn, token)
                if not visible:
                    return {"items": []}
                clauses.append("submission_id IN (%s)" % ",".join("?" * len(visible)))
                params.extend(visible)
            elif staff["role"] == "auditor":
                pass
            elif staff["role"] in ("reviewer", "admin", "operator"):
                pass
            if clauses:
                sql += " WHERE " + " AND ".join(clauses)
            sql += " ORDER BY created_at, submission_id"
            items = []
            for row in conn.execute(sql, params):
                item = dict(row)
                items.append(item)
            return {"items": items}

    def _timeline(self, conn, submission_id: str) -> list[dict[str, Any]]:
        rows = conn.execute(
            "SELECT * FROM eg_submission_events WHERE submission_id=? ORDER BY seq",
            (submission_id,),
        ).fetchall()
        return [{"event_id": r["event_id"], "seq": r["seq"], "event_type": r["event_type"],
                 "occurred_at": r["occurred_at"], "actor": r["actor"],
                 "track_id": r["track_id"], "version": r["version"],
                 "detail": json.loads(r["detail_json"])} for r in rows]

    def get_submission(self, actor_id: str, submission_id: str,
                       include_versions: bool = True) -> dict[str, Any]:
        """返回当前完整材料、版本、事件时间线、冲突依据与评审进度。"""

        with self.database.transaction() as conn:
            token, staff = self._resolve_caller(conn, actor_id)
            submission_id = self._id(submission_id, "submission_id")
            sub = conn.execute("SELECT * FROM eg_submissions WHERE submission_id=?",
                               (submission_id,)).fetchone()
            if sub is None:
                raise NotFoundError("投稿不存在")
            if staff is None:
                if submission_id not in self._caller_party_submission_ids(conn, token):
                    raise PermissionDenied("无权查看该投稿")
            is_auditor = staff is not None and staff["role"] == "auditor"

            parties = [dict(r) for r in conn.execute(
                "SELECT party_type,party_id,party_role FROM eg_submission_parties"
                " WHERE submission_id=? ORDER BY party_role,party_type,party_id",
                (submission_id,))]
            versions = []
            if include_versions:
                for r in conn.execute(
                        "SELECT version,title,content_json,content_hash,late,created_at,"
                        "introduced_by_event FROM eg_work_versions WHERE submission_id=?"
                        " ORDER BY version", (submission_id,)):
                    versions.append({"version": r["version"], "title": r["title"],
                                     "content": json.loads(r["content_json"]),
                                     "content_hash": r["content_hash"], "late": bool(r["late"]),
                                     "created_at": r["created_at"],
                                     "introduced_by_event": r["introduced_by_event"]})
            task = conn.execute("SELECT * FROM eg_review_tasks WHERE submission_id=?",
                                (submission_id,)).fetchone()
            window = self._load_window(conn, sub["window_id"])
            track = conn.execute("SELECT * FROM eg_tracks WHERE track_id=?",
                                 (sub["track_id"],)).fetchone()
            # 互斥依据：同组内同主体的其他有效投稿。
            siblings = self._mutex_conflicts(
                conn, window_id=sub["window_id"], track_group=track["exclusive_group"],
                subject_id=self._subject_root(conn, sub["subject_id"]),
                exclude_submission_id=submission_id)
            occupancy = self._track_occupancy(conn, sub["window_id"], sub["track_id"])
            appeals = [dict(r) for r in conn.execute(
                "SELECT appeal_id,reason,late_version,status,filed_by,filed_at,decided_by,"
                "decided_at,decision_note,evidence_hash FROM eg_appeals WHERE submission_id=?"
                " ORDER BY filed_at", (submission_id,))]
            result = {
                "submission": dict(sub),
                "window": {"window_id": window["window_id"], "name": window["name"],
                           "opens_at": window["opens_at"], "closes_at": window["closes_at"],
                           "frozen": bool(window["frozen"]), "frozen_at": window["frozen_at"]},
                "track": {"track_id": track["track_id"], "name": track["name"],
                          "exclusive_group": track["exclusive_group"],
                          "slots_total": track["slots_total"], "occupied": occupancy},
                "parties": parties,
                "versions": versions,
                "effective_version": sub["effective_version"],
                "appeal_effective_version": sub["appeal_effective_version"],
                "timeline": self._timeline(conn, submission_id),
                "review_task": dict(task) if task else None,
                "mutex_basis": {"exclusive_group": track["exclusive_group"],
                                "root_subject_id": self._subject_root(conn, sub["subject_id"]),
                                "sibling_submissions": siblings},
                "appeals": appeals,
            }
            if is_auditor:
                result["audit_note"] = "审计视角：含全部版本、事件与申诉依据"
            return result

    def list_conflicts(self, actor_id: str, window_id: str) -> dict[str, Any]:
        """工作人员视角：列窗口内的互斥占位与名额超额依据。"""

        with self.database.transaction() as conn:
            _token, staff = self._resolve_caller(conn, actor_id)
            self._staff(staff, "admin", "operator", "reviewer", "auditor")
            self._load_window(conn, window_id)
            groups: dict[str, dict[str, list]] = {}
            rows = conn.execute(
                "SELECT s.submission_id, s.subject_id, s.track_id, t.exclusive_group,"
                " s.applicant_type, s.applicant_id, s.status FROM eg_submissions s"
                " JOIN eg_tracks t ON t.track_id=s.track_id WHERE s.window_id=?"
                " AND s.status!='withdrawn'", (window_id,)).fetchall()
            for row in rows:
                if not row["exclusive_group"]:
                    continue
                root = self._subject_root(conn, row["subject_id"])
                bucket = groups.setdefault(row["exclusive_group"], {}).setdefault(root, [])
                bucket.append({"submission_id": row["submission_id"],
                               "track_id": row["track_id"],
                               "applicant": f"{row['applicant_type']}:{row['applicant_id']}",
                               "status": row["status"]})
            duplicates = []
            for group, subjects in groups.items():
                for root, items in subjects.items():
                    if len(items) > 1:
                        duplicates.append({"exclusive_group": group, "root_subject_id": root,
                                           "submissions": items})
            quotas = []
            for t in conn.execute(
                    "SELECT t.track_id, t.name, t.slots_total, COUNT(s.submission_id) AS occupied"
                    " FROM eg_window_tracks wt JOIN eg_tracks t ON t.track_id=wt.track_id"
                    " LEFT JOIN eg_submissions s ON s.track_id=t.track_id"
                    " AND s.window_id=wt.window_id AND s.status!='withdrawn'"
                    " WHERE wt.window_id=? GROUP BY t.track_id", (window_id,)):
                if t["occupied"] > t["slots_total"]:
                    quotas.append(dict(t))
            return {"window_id": window_id, "duplicate_occupancy": duplicates,
                    "quota_overages": quotas}

    # ------------------------------------------------------------------
    # 历史时点回放
    # ------------------------------------------------------------------
    def explain(self, actor_id: str, submission_id: str, at: str) -> dict[str, Any]:
        """按任意历史时点回放事件，解释作品为何有效/被拒/等待补正。"""

        with self.database.transaction() as conn:
            token, staff = self._resolve_caller(conn, actor_id)
            submission_id = self._id(submission_id, "submission_id")
            sub = conn.execute("SELECT * FROM eg_submissions WHERE submission_id=?",
                               (submission_id,)).fetchone()
            if sub is None:
                raise NotFoundError("投稿不存在")
            if staff is None and submission_id not in self._caller_party_submission_ids(conn, token):
                raise PermissionDenied("无权查看该投稿")
            point = parse_ts(at, "at")
            point_text = fmt_ts(point)

            state = {"status": "not_exists", "track_id": None, "current_version": 0,
                     "effective_version": None, "appeal_effective_version": None,
                     "frozen_at": None, "correction_due_at": None}
            applied_events = []
            for event in self._timeline(conn, submission_id):
                if parse_ts(event["occurred_at"], "occurred_at") > point:
                    break
                d = event["detail"]
                et = event["event_type"]
                if et == "submitted":
                    state.update(status="in_review", track_id=event["track_id"],
                                 current_version=1)
                elif et == "correction_submitted":
                    state["current_version"] = event["version"]
                    state["status"] = "in_review"
                    state["correction_due_at"] = None
                elif et == "withdrawn":
                    state["status"] = "withdrawn"
                elif et == "transferred":
                    state["track_id"] = event["track_id"]
                elif et == "members_changed":
                    pass
                elif et == "review_decided":
                    decision = d.get("decision")
                    state["status"] = DECISION_STATUS.get(decision, state["status"])
                    if decision == "correction_requested":
                        state["correction_due_at"] = d.get("correction_due_at")
                elif et == "frozen":
                    state["effective_version"] = d["effective_version"]
                    state["frozen_at"] = event["occurred_at"]
                elif et == "appeal_filed":
                    state["appeal_status"] = "pending"
                elif et == "appeal_decided":
                    state["appeal_status"] = d["decision"]
                    if d["decision"] == "accepted":
                        state["appeal_effective_version"] = d.get("late_version")
                applied_events.append({"seq": event["seq"], "event_type": et,
                                       "occurred_at": event["occurred_at"],
                                       "actor": event["actor"], "detail": d})

            # 结论与人类可读理由。
            reasons: list[str] = []
            if state["status"] == "not_exists":
                verdict = "not_exists"
                reasons.append("该时点之前尚未提交报名。")
            elif state["status"] == "withdrawn":
                verdict = "withdrawn"
                reasons.append("申请人已在该时点之前撤回投稿。")
            elif state["status"] == "rejected":
                last = next((e for e in reversed(applied_events)
                             if e["event_type"] == "review_decided"
                             and e["detail"].get("decision") == "rejected"), None)
                verdict = "rejected"
                if last:
                    reasons.append(f"审核员 {last['actor']} 于 {last['occurred_at']} 拒绝："
                                   f"{last['detail'].get('reason', '')}")
            elif state["status"] == "accepted":
                last = next((e for e in reversed(applied_events)
                             if e["event_type"] == "review_decided"
                             and e["detail"].get("decision") == "accepted"), None)
                verdict = "accepted"
                if last:
                    reasons.append(f"审核员 {last['actor']} 于 {last['occurred_at']} 判定通过。")
                if state["effective_version"]:
                    reasons.append(f"窗口冻结锁定的生效版本为第 {state['effective_version']} 版。")
                if state.get("appeal_status") == "accepted" and state["appeal_effective_version"]:
                    reasons.append("申诉已采纳，迟到第 "
                                   f"{state['appeal_effective_version']} 版仅作为申诉生效版，"
                                   f"原冻结第 {state['effective_version']} 版未被改写。")
            elif state["status"] == "awaiting_correction":
                verdict = "awaiting_correction"
                req = next((e for e in reversed(applied_events)
                            if e["event_type"] == "review_decided"
                            and e["detail"].get("decision") == "correction_requested"), None)
                if req:
                    reasons.append(f"审核员 {req['actor']} 于 {req['occurred_at']} 要求补正："
                                   f"{req['detail'].get('note', '')}")
                    due = state.get("correction_due_at")
                    if due:
                        reasons.append(f"补正期限：{due}（{'已过期' if point_text >= due else '未过期'}）。")
            else:
                verdict = "in_review"
                if state["effective_version"]:
                    reasons.append("窗口已冻结，评审基于第 "
                                   f"{state['effective_version']} 版继续进行，结论尚未作出。")
                else:
                    reasons.append("材料在审，尚未出现评审结论。")

            # 该时点的互斥占位依据：回放同窗口同组兄弟投稿的存活状态。
            sibling_basis = self._sibling_basis_at(conn, sub=sub, point=point)
            return {
                "submission_id": submission_id,
                "at": point_text,
                "verdict": verdict,
                "state_at": state,
                "window_frozen_at_point": state["frozen_at"] is not None,
                "reasons": reasons,
                "evidence_events": applied_events,
                "mutex_basis_at_point": sibling_basis,
            }

    def _sibling_basis_at(self, conn, *, sub, point: datetime) -> list[dict[str, Any]]:
        track = conn.execute("SELECT exclusive_group FROM eg_tracks WHERE track_id=?",
                             (sub["track_id"],)).fetchone()
        if not track or not track["exclusive_group"]:
            return []
        root = self._subject_root(conn, sub["subject_id"])
        rows = conn.execute(
            "SELECT s.submission_id, s.track_id, s.applicant_type, s.applicant_id,"
            " s.subject_id FROM eg_submissions s JOIN eg_tracks t ON t.track_id=s.track_id"
            " WHERE s.window_id=? AND t.exclusive_group=? AND s.submission_id!=?",
            (sub["window_id"], track["exclusive_group"], sub["submission_id"])).fetchall()
        basis = []
        for row in rows:
            if self._subject_root(conn, row["subject_id"]) != root:
                continue
            withdrawn_at = conn.execute(
                "SELECT MIN(occurred_at) AS at FROM eg_submission_events"
                " WHERE submission_id=? AND event_type='withdrawn' AND occurred_at<=?",
                (row["submission_id"], fmt_ts(point))).fetchone()["at"]
            submitted_at = conn.execute(
                "SELECT MIN(occurred_at) AS at FROM eg_submission_events"
                " WHERE submission_id=? AND event_type='submitted' AND occurred_at<=?",
                (row["submission_id"], fmt_ts(point))).fetchone()["at"]
            if submitted_at and not withdrawn_at:
                basis.append({"submission_id": row["submission_id"],
                              "track_id_at_point": self._track_at(conn, row["submission_id"], point),
                              "applicant": f"{row['applicant_type']}:{row['applicant_id']}",
                              "submitted_at": submitted_at})
        return basis

    def _track_at(self, conn, submission_id: str, point: datetime) -> str | None:
        track_id = None
        for row in conn.execute(
                "SELECT event_type,track_id FROM eg_submission_events WHERE submission_id=?"
                " AND occurred_at<=? ORDER BY seq", (submission_id, fmt_ts(point))):
            if row["event_type"] in ("submitted", "transferred"):
                track_id = row["track_id"]
        return track_id
