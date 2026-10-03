"""白塔杯参赛资格与投稿治理领域服务。

把自然人、组织、监护/授权代理、团队成员、作品版本、赛道名额与报名窗口
纳入同一套可追溯规则。所有状态推进都写入不可覆盖的事件账本与哈希审计链。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from .audit import append_event, canonical_json, digest
from .clock import Clock, SystemClock
from .errors import (
    ConflictError,
    DuplicateQualificationError,
    FrozenError,
    NotFoundError,
    PermissionDenied,
    ValidationError,
    WindowClosedError,
)
from .models import (
    OrganizationParty,
    Person,
    Representation,
    SubmissionView,
    Track,
    RegistrationWindow,
    WriteReceipt,
)


REVIEWABLE_STATUSES = ("pending", "awaiting_correction")
ENTRY_EVENT_TYPES = (
    "submitted",
    "corrected",
    "withdrawn",
    "track_switched",
    "member_changed",
    "reviewed",
    "frozen",
    "appeal_submitted",
    "appeal_decided",
)


def parse_ts(value: str) -> datetime:
    """解析带时区的 ISO8601 时间字符串并归一化为 UTC。"""

    text = str(value).strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        raise ValidationError("时间必须包含时区")
    return parsed.astimezone(timezone.utc)


def ts_text(value: datetime) -> str:
    """统一时间文本格式（UTC，Z 结尾）。"""

    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class GovernanceService:
    """实现资格、投稿、版本、名额、窗口与审核的全部规则。"""

    def __init__(self, database, clock: Clock | None = None) -> None:
        self.database = database
        self.clock = clock or SystemClock()

    # ---------- 基础工具 ----------

    def _now(self) -> datetime:
        return self.clock.now().astimezone()

    def _now_text(self) -> str:
        return ts_text(self._now())

    def _id(self, value: str, field: str) -> str:
        value = str(value or "").strip()
        if not value or len(value) > 64:
            raise ValidationError(f"{field} 不能为空且不能超过 64 个字符")
        return value

    def _text(self, value: str, field: str, limit: int = 300) -> str:
        value = str(value or "").strip()
        if not value or len(value) > limit:
            raise ValidationError(f"{field} 不能为空且不能超过 {limit} 个字符")
        return value

    def _staff(self, connection, actor_id: str, *roles: str):
        row = connection.execute("SELECT * FROM actors WHERE actor_id=?", (actor_id,)).fetchone()
        if row is None:
            raise NotFoundError("操作者不存在")
        if not row["active"]:
            raise PermissionDenied("操作者已停用")
        if roles and row["role"] not in roles:
            raise PermissionDenied("当前角色不能执行该动作")
        return row

    def _person_exists(self, connection, person_id: str) -> bool:
        return connection.execute(
            "SELECT 1 FROM persons WHERE person_id=?", (person_id,)).fetchone() is not None

    def _readonly(self, actor_id: str, *roles: str):
        """员工只读访问：直接使用自动提交连接，避免与写事务在共享连接上冲突。"""

        conn = self.database.connection
        self._staff(conn, actor_id, *roles)
        return conn

    def _staff_or_person(self, connection, caller_id: str, *roles: str) -> str:
        """允许后台员工（按角色）或持有令牌的自然人调用；返回调用者类型。"""

        row = connection.execute(
            "SELECT role,active FROM actors WHERE actor_id=?", (caller_id,)).fetchone()
        if row is not None:
            if not row["active"]:
                raise PermissionDenied("操作者已停用")
            if roles and row["role"] not in roles:
                raise PermissionDenied("当前角色不能执行该动作")
            return "staff"
        if self._person_exists(connection, caller_id):
            return "person"
        raise NotFoundError("操作者不存在")

    def _peek_receipt(self, connection, *, request_id: str, action: str,
                      payload: dict[str, Any]) -> WriteReceipt | None:
        """请求编号已成功处理过时，直接回放原回执（优先于一切业务校验）。"""

        request_id = self._id(request_id, "request_id")
        row = connection.execute(
            "SELECT * FROM request_receipts WHERE request_id=?", (request_id,)
        ).fetchone()
        if row is None:
            return None
        if row["action"] != action or row["payload_hash"] != digest(payload):
            raise ConflictError("request_id 已被不同内容使用")
        return WriteReceipt(request_id, row["resource_type"], row["resource_id"], True)

    def _idempotent(self, connection, *, request_id: str, action: str,
                    payload: dict[str, Any], create: Callable[[], tuple[str, str, dict[str, Any]]]) -> WriteReceipt:
        replay = self._peek_receipt(connection, request_id=request_id, action=action, payload=payload)
        if replay is not None:
            return replay
        request_id = self._id(request_id, "request_id")
        resource_type, resource_id, response = create()
        connection.execute(
            "INSERT INTO request_receipts(request_id,action,payload_hash,resource_type,resource_id,"
            "response_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (request_id, action, digest(payload), resource_type, resource_id,
             canonical_json(response), self._now_text()),
        )
        return WriteReceipt(request_id, resource_type, resource_id, False)

    def _audit(self, connection, *, actor_id: str, action: str, resource_type: str,
               resource_id: str, detail: dict[str, Any]) -> None:
        append_event(connection, actor_id=actor_id, action=action, resource_type=resource_type,
                     resource_id=resource_id, detail=detail, occurred_at=self._now_text())

    # ---------- 赛道 / 窗口 ----------

    def create_track(self, *, request_id: str, actor_id: str, track_id: str, site_id: str,
                     name: str, quota: int, mutex_group: str) -> WriteReceipt:
        payload = {"track_id": track_id, "site_id": site_id, "name": name,
                   "quota": quota, "mutex_group": mutex_group}
        with self.database.transaction(immediate=True) as conn:
            self._staff(conn, actor_id, "admin", "operator")
            track_id = self._id(track_id, "track_id")
            name = self._text(name, "name")
            mutex_group = self._id(mutex_group, "mutex_group")
            if not isinstance(quota, int) or quota <= 0:
                raise ValidationError("quota 必须是正整数")
            if conn.execute("SELECT 1 FROM sites WHERE site_id=?", (site_id,)).fetchone() is None:
                raise NotFoundError("场所不存在")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    conn.execute(
                        "INSERT INTO competition_tracks(track_id,site_id,name,quota,mutex_group,created_at) "
                        "VALUES(?,?,?,?,?,?)",
                        (track_id, site_id, name, quota, mutex_group, self._now_text()),
                    )
                except Exception as exc:
                    raise ConflictError("赛道编号已存在") from exc
                self._audit(conn, actor_id=actor_id, action="track.created",
                            resource_type="track", resource_id=track_id,
                            detail={"name": name, "quota": quota, "mutex_group": mutex_group})
                return "track", track_id, {"track_id": track_id}

            return self._idempotent(conn, request_id=request_id, action="create_track",
                                    payload=payload, create=create)

    def create_window(self, *, request_id: str, actor_id: str, window_id: str, site_id: str,
                      opens_at: str, deadline_at: str) -> WriteReceipt:
        opens_dt = parse_ts(opens_at)
        deadline_dt = parse_ts(deadline_at)
        if deadline_dt <= opens_dt:
            raise ValidationError("截止时间必须晚于开放时间")
        payload = {"window_id": window_id, "site_id": site_id,
                   "opens_at": ts_text(opens_dt), "deadline_at": ts_text(deadline_dt)}
        with self.database.transaction(immediate=True) as conn:
            self._staff(conn, actor_id, "admin", "operator")
            window_id = self._id(window_id, "window_id")
            if conn.execute("SELECT 1 FROM sites WHERE site_id=?", (site_id,)).fetchone() is None:
                raise NotFoundError("场所不存在")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    conn.execute(
                        "INSERT INTO registration_windows(window_id,site_id,opens_at,deadline_at,"
                        "status,created_at) VALUES(?,?,?,?, 'open', ?)",
                        (window_id, site_id, ts_text(opens_dt), ts_text(deadline_dt),
                         self._now_text()),
                    )
                except Exception as exc:
                    raise ConflictError("窗口编号已存在") from exc
                self._audit(conn, actor_id=actor_id, action="window.created",
                            resource_type="window", resource_id=window_id,
                            detail={"opens_at": ts_text(opens_dt), "deadline_at": ts_text(deadline_dt)})
                return "window", window_id, {"window_id": window_id}

            return self._idempotent(conn, request_id=request_id, action="create_window",
                                    payload=payload, create=create)

    def _window(self, conn, window_id: str):
        row = conn.execute("SELECT * FROM registration_windows WHERE window_id=?",
                           (window_id,)).fetchone()
        if row is None:
            raise NotFoundError("报名窗口不存在")
        return row

    def _assert_open(self, window_row) -> None:
        now = self._now()
        if now < parse_ts(window_row["opens_at"]):
            raise WindowClosedError("报名尚未开放")
        if now >= parse_ts(window_row["deadline_at"]):
            raise WindowClosedError("报名窗口已截止，迟到材料请走申诉渠道")
        if window_row["status"] == "frozen":
            raise FrozenError("窗口已冻结，不能再改动作品")

    def freeze_window(self, *, request_id: str, actor_id: str, window_id: str) -> WriteReceipt:
        """截止时在单事务内原子冻结全部生效版本。"""

        payload = {"window_id": window_id}
        with self.database.transaction(immediate=True) as conn:
            replay = self._peek_receipt(conn, request_id=request_id, action="freeze_window", payload=payload)
            if replay is not None:
                return replay
            self._staff(conn, actor_id, "admin", "operator")
            window = self._window(conn, window_id)
            if window["status"] == "frozen":
                raise FrozenError("窗口已经冻结")
            if self._now() < parse_ts(window["deadline_at"]):
                raise ValidationError("尚未到达截止时间，不能提前冻结")
            frozen_at = self._now_text()

            def create() -> tuple[str, str, dict[str, Any]]:
                conn.execute(
                    "UPDATE registration_windows SET status='frozen', frozen_at=? WHERE window_id=?",
                    (frozen_at, window_id),
                )
                submissions = conn.execute(
                    "SELECT * FROM submissions WHERE window_id=? AND status!='withdrawn'",
                    (window_id,),
                ).fetchall()
                count = 0
                for sub in submissions:
                    version = conn.execute(
                        "SELECT version_seq FROM submission_versions WHERE submission_id=? "
                        "ORDER BY version_seq DESC LIMIT 1", (sub["submission_id"],)
                    ).fetchone()
                    roster = conn.execute(
                        "SELECT person_id, change FROM team_member_events WHERE team_id=? "
                        "ORDER BY rowid", (sub["team_id"],)
                    ).fetchall() if sub["team_id"] else []
                    current_members: list[str] = []
                    designated = None
                    for item in roster:
                        if item["change"] == "joined" and item["person_id"] not in current_members:
                            current_members.append(item["person_id"])
                        elif item["change"] == "left" and item["person_id"] in current_members:
                            current_members.remove(item["person_id"])
                        elif item["change"] == "beneficiary_designated":
                            designated = item["person_id"]
                    snapshot_id = uuid.uuid4().hex
                    conn.execute(
                        "INSERT INTO freeze_snapshots(snapshot_id,window_id,submission_id,version_seq,"
                        "track_id,status,roster_json,frozen_at) VALUES(?,?,?,?,?,?,?,?)",
                        (snapshot_id, window_id, sub["submission_id"],
                         version["version_seq"] if version else 0,
                         sub["current_track_id"], sub["status"],
                         canonical_json({"members": current_members, "beneficiary": designated}),
                         frozen_at),
                    )
                    conn.execute(
                        "INSERT INTO entry_events(submission_id,event_type,note,actor_id,occurred_at) "
                        "VALUES(?,?,?,?,?)",
                        (sub["submission_id"], "frozen",
                         f"截止冻结生效版本 {version['version_seq'] if version else 0}", actor_id, frozen_at),
                    )
                    count += 1
                self._audit(conn, actor_id=actor_id, action="window.frozen",
                            resource_type="window", resource_id=window_id,
                            detail={"frozen_at": frozen_at, "submissions": count})
                return "window", window_id, {"window_id": window_id, "frozen_submissions": count}

            return self._idempotent(conn, request_id=request_id, action="freeze_window",
                                    payload=payload, create=create)

    # ---------- 主体登记 ----------

    def register_person(self, *, request_id: str, actor_id: str, person_id: str,
                        display_name: str, is_minor: bool, id_number: str) -> WriteReceipt:
        payload = {"person_id": person_id, "display_name": display_name,
                   "is_minor": bool(is_minor), "id_number": str(id_number)}
        with self.database.transaction(immediate=True) as conn:
            self._staff(conn, actor_id, "admin", "operator", "reviewer")
            person_id = self._id(person_id, "person_id")
            display_name = self._text(display_name, "display_name", 100)
            id_number = self._text(id_number, "id_number", 64)
            id_hash = digest({"id_number": id_number})
            id_masked = "***" + id_number[-4:] if len(id_number) >= 4 else "***"

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    conn.execute(
                        "INSERT INTO persons(person_id,display_name,is_minor,id_hash,id_masked,"
                        "created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                        (person_id, display_name, 1 if is_minor else 0, id_hash, id_masked,
                         actor_id, self._now_text()),
                    )
                except Exception as exc:
                    raise ConflictError("自然人已登记或证件号重复") from exc
                self._audit(conn, actor_id=actor_id, action="person.registered",
                            resource_type="person", resource_id=person_id,
                            detail={"display_name": display_name, "is_minor": bool(is_minor),
                                    "id_masked": id_masked})
                return "person", person_id, {"person_id": person_id, "id_masked": id_masked}

            return self._idempotent(conn, request_id=request_id, action="register_person",
                                    payload=payload, create=create)

    def register_organization(self, *, request_id: str, actor_id: str,
                              organization_party_id: str, legal_name: str, org_type: str,
                              registration_number: str, beneficial_person_id: str) -> WriteReceipt:
        payload = {"organization_party_id": organization_party_id, "legal_name": legal_name,
                   "org_type": org_type, "registration_number": str(registration_number),
                   "beneficial_person_id": beneficial_person_id}
        with self.database.transaction(immediate=True) as conn:
            self._staff(conn, actor_id, "admin", "operator", "reviewer")
            organization_party_id = self._id(organization_party_id, "organization_party_id")
            legal_name = self._text(legal_name, "legal_name")
            if org_type not in ("studio", "agency", "institution"):
                raise ValidationError("org_type 非法")
            if conn.execute("SELECT 1 FROM persons WHERE person_id=?",
                            (beneficial_person_id,)).fetchone() is None:
                raise NotFoundError("受益控制自然人不存在")
            reg_hash = digest({"registration_number": str(registration_number)})

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    conn.execute(
                        "INSERT INTO organizations_ext(organization_party_id,legal_name,org_type,"
                        "registration_hash,beneficial_person_id,created_by,created_at) "
                        "VALUES(?,?,?,?,?,?,?)",
                        (organization_party_id, legal_name, org_type, reg_hash,
                         beneficial_person_id, actor_id, self._now_text()),
                    )
                except Exception as exc:
                    raise ConflictError("组织已登记或证照号重复") from exc
                self._audit(conn, actor_id=actor_id, action="organization_party.registered",
                            resource_type="organization_party", resource_id=organization_party_id,
                            detail={"legal_name": legal_name, "org_type": org_type,
                                    "beneficial_person_id": beneficial_person_id})
                return "organization_party", organization_party_id, {
                    "organization_party_id": organization_party_id}

            return self._idempotent(conn, request_id=request_id, action="register_organization_party",
                                    payload=payload, create=create)

    def register_representation(self, *, request_id: str, actor_id: str, relation_id: str,
                                subject_person_id: str, representative_person_id: str,
                                kind: str, scope: list[str], valid_from: str,
                                valid_until: str | None, evidence_ref: str) -> WriteReceipt:
        if not isinstance(scope, list) or not scope or not all(isinstance(s, str) for s in scope):
            raise ValidationError("scope 必须是非空字符串数组")
        payload = {"relation_id": relation_id, "subject_person_id": subject_person_id,
                   "representative_person_id": representative_person_id, "kind": kind,
                   "scope": sorted(scope), "valid_from": valid_from,
                   "valid_until": valid_until, "evidence_ref": evidence_ref}
        from_dt = parse_ts(valid_from)
        until_dt = parse_ts(valid_until) if valid_until else None
        if until_dt and until_dt <= from_dt:
            raise ValidationError("授权结束时间必须晚于开始时间")
        with self.database.transaction(immediate=True) as conn:
            self._staff(conn, actor_id, "admin", "operator", "reviewer")
            relation_id = self._id(relation_id, "relation_id")
            if kind not in ("guardianship", "authorization"):
                raise ValidationError("kind 非法")
            for label, pid in (("subject_person_id", subject_person_id),
                               ("representative_person_id", representative_person_id)):
                if conn.execute("SELECT 1 FROM persons WHERE person_id=?", (pid,)).fetchone() is None:
                    raise NotFoundError(f"{label} 不存在")
            if subject_person_id == representative_person_id:
                raise ValidationError("被代理人与代理人不能是同一人")
            subject = conn.execute("SELECT is_minor FROM persons WHERE person_id=?",
                                   (subject_person_id,)).fetchone()
            if kind == "guardianship" and not subject["is_minor"]:
                raise ValidationError("监护关系仅适用于未成年自然人")
            evidence_hash = digest({"evidence_ref": str(evidence_ref)})

            def create() -> tuple[str, str, dict[str, Any]]:
                conn.execute(
                    "INSERT INTO representation_relations(relation_id,subject_person_id,"
                    "representative_person_id,kind,scope_json,valid_from,valid_until,evidence_hash,"
                    "status,created_by,created_at) VALUES(?,?,?,?,?,?,?,?, 'active', ?,?)",
                    (relation_id, subject_person_id, representative_person_id, kind,
                     canonical_json(sorted(scope)), ts_text(from_dt),
                     ts_text(until_dt) if until_dt else None, evidence_hash,
                     actor_id, self._now_text()),
                )
                self._audit(conn, actor_id=actor_id, action="representation.registered",
                            resource_type="representation", resource_id=relation_id,
                            detail={"subject_person_id": subject_person_id,
                                    "representative_person_id": representative_person_id,
                                    "kind": kind, "scope": sorted(scope)})
                return "representation", relation_id, {"relation_id": relation_id}

            return self._idempotent(conn, request_id=request_id, action="register_representation",
                                    payload=payload, create=create)

    def revoke_representation(self, *, request_id: str, actor_id: str, relation_id: str) -> WriteReceipt:
        payload = {"relation_id": relation_id}
        with self.database.transaction(immediate=True) as conn:
            self._staff(conn, actor_id, "admin", "operator", "reviewer")
            row = conn.execute("SELECT * FROM representation_relations WHERE relation_id=?",
                               (relation_id,)).fetchone()
            if row is None:
                raise NotFoundError("代理关系不存在")

            def create() -> tuple[str, str, dict[str, Any]]:
                conn.execute(
                    "UPDATE representation_relations SET status='revoked', revoked_at=? "
                    "WHERE relation_id=?", (self._now_text(), relation_id),
                )
                self._audit(conn, actor_id=actor_id, action="representation.revoked",
                            resource_type="representation", resource_id=relation_id, detail={})
                return "representation", relation_id, {"relation_id": relation_id, "status": "revoked"}

            return self._idempotent(conn, request_id=request_id, action="revoke_representation",
                                    payload=payload, create=create)

    # ---------- 团队 ----------

    def create_team(self, *, request_id: str, actor_id: str, team_id: str, name: str,
                    creator_person_id: str) -> WriteReceipt:
        payload = {"team_id": team_id, "name": name, "creator_person_id": creator_person_id}
        with self.database.transaction(immediate=True) as conn:
            replay = self._peek_receipt(conn, request_id=request_id, action="create_team", payload=payload)
            if replay is not None:
                return replay
            self._staff_or_person(conn, actor_id, "admin", "operator")
            team_id = self._id(team_id, "team_id")
            name = self._text(name, "name", 120)
            if not self._person_exists(conn, creator_person_id):
                raise NotFoundError("创建人不存在")
            if actor_id not in (creator_person_id,) and conn.execute(
                    "SELECT role FROM actors WHERE actor_id=?", (actor_id,)).fetchone() is None:
                # 自然人只能以自己身份创建团队
                raise PermissionDenied("自然人只能创建自己为创建人的团队")
            now = self._now_text()

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    conn.execute(
                        "INSERT INTO creative_teams(team_id,name,created_by,created_at) VALUES(?,?,?,?)",
                        (team_id, name, actor_id or creator_person_id, now),
                    )
                except Exception as exc:
                    raise ConflictError("团队编号已存在") from exc
                conn.execute(
                    "INSERT INTO team_member_events(member_event_id,team_id,person_id,change,"
                    "occurred_at,changed_by,note) VALUES(?,?,?, 'joined', ?,?,?)",
                    (uuid.uuid4().hex, team_id, creator_person_id, now,
                     actor_id or creator_person_id, "团队创建人"),
                )
                self._audit(conn, actor_id=actor_id or creator_person_id, action="team.created",
                            resource_type="team", resource_id=team_id,
                            detail={"name": name, "creator_person_id": creator_person_id})
                return "team", team_id, {"team_id": team_id}

            return self._idempotent(conn, request_id=request_id, action="create_team",
                                    payload=payload, create=create)

    def _team_roster(self, conn, team_id: str) -> tuple[list[str], str | None]:
        rows = conn.execute(
            "SELECT person_id, change FROM team_member_events WHERE team_id=? ORDER BY rowid",
            (team_id,),
        ).fetchall()
        members: list[str] = []
        designated = None
        for row in rows:
            if row["change"] == "joined" and row["person_id"] not in members:
                members.append(row["person_id"])
            elif row["change"] == "left" and row["person_id"] in members:
                members.remove(row["person_id"])
            elif row["change"] == "beneficiary_designated":
                designated = row["person_id"]
        return members, designated

    def designate_team_beneficiary(self, *, request_id: str, actor_id: str, team_id: str,
                                   person_id: str) -> WriteReceipt:
        payload = {"team_id": team_id, "person_id": person_id}
        with self.database.transaction(immediate=True) as conn:
            replay = self._peek_receipt(conn, request_id=request_id,
                                        action="designate_team_beneficiary", payload=payload)
            if replay is not None:
                return replay
            if conn.execute("SELECT 1 FROM creative_teams WHERE team_id=?",
                            (team_id,)).fetchone() is None:
                raise NotFoundError("团队不存在")
            members, current = self._team_roster(conn, team_id)
            caller_kind = self._staff_or_person(conn, actor_id, "admin", "operator")
            if caller_kind == "person" and actor_id not in members:
                raise PermissionDenied("只有团队成员可以指定受益创作主体")
            if person_id not in members:
                raise ValidationError("受益创作主体必须是团队当前成员")
            # 团队已持有生效名额时不得更换受益主体，防止借团队身份把同一资格转给他人后再占新名额
            if current is not None and current != person_id:
                active = conn.execute(
                    "SELECT 1 FROM submissions s JOIN qualification_holds h "
                    "ON h.submission_id=s.submission_id AND h.status='held' "
                    "WHERE s.team_id=? LIMIT 1", (team_id,),
                ).fetchone()
                if active is not None:
                    raise ConflictError("团队已有作品持有生效资格，不能更换受益创作主体")
            now = self._now_text()

            def create() -> tuple[str, str, dict[str, Any]]:
                conn.execute(
                    "INSERT INTO team_member_events(member_event_id,team_id,person_id,change,"
                    "occurred_at,changed_by,note) VALUES(?,?,?, 'beneficiary_designated', ?,?,?)",
                    (uuid.uuid4().hex, team_id, person_id, now, actor_id, "指定团队受益创作主体"),
                )
                self._audit(conn, actor_id=actor_id, action="team.beneficiary_designated",
                            resource_type="team", resource_id=team_id,
                            detail={"person_id": person_id})
                return "team", team_id, {"team_id": team_id, "beneficiary_person_id": person_id}

            return self._idempotent(conn, request_id=request_id,
                                    action="designate_team_beneficiary",
                                    payload=payload, create=create)

    # ---------- 参赛令牌 ----------

    def mint_participant_token(self, *, request_id: str, actor_id: str, person_id: str,
                               label: str) -> dict[str, Any]:
        payload = {"person_id": person_id, "label": label}
        with self.database.transaction(immediate=True) as conn:
            self._staff(conn, actor_id, "admin", "operator")
            if conn.execute("SELECT 1 FROM persons WHERE person_id=?",
                            (person_id,)).fetchone() is None:
                raise NotFoundError("自然人不存在")
            label = self._text(label, "label", 100)
            token = "ptk_" + uuid.uuid4().hex
            token_hash = digest({"token": token})

            def create() -> tuple[str, str, dict[str, Any]]:
                conn.execute(
                    "INSERT INTO participant_tokens(token_hash,person_id,label,created_at) "
                    "VALUES(?,?,?,?)",
                    (token_hash, person_id, label, self._now_text()),
                )
                self._audit(conn, actor_id=actor_id, action="participant_token.minted",
                            resource_type="person", resource_id=person_id, detail={"label": label})
                return "participant_token", token_hash, {"person_id": person_id}

            receipt = self._idempotent(conn, request_id=request_id,
                                       action="mint_participant_token", payload=payload,
                                       create=create)
        # 明文令牌仅在创建时返回一次
        return {"request_id": receipt.request_id, "resource_type": receipt.resource_type,
                "resource_id": receipt.resource_id, "replayed": receipt.replayed,
                "person_id": person_id, **({} if receipt.replayed else {"token": token})}

    def resolve_participant_token(self, token: str) -> str:
        token = str(token or "").strip()
        if not token:
            raise PermissionDenied("缺少参赛人令牌")
        row = self.database.connection.execute(
            "SELECT person_id FROM participant_tokens WHERE token_hash=?",
            (digest({"token": token}),),
        ).fetchone()
        if row is None:
            raise PermissionDenied("参赛人令牌无效")
        return row["person_id"]

    # ---------- 资格与授权校验 ----------

    def _resolve_beneficiary(self, conn, *, submitter_person_id, team_id,
                             organization_party_id) -> str:
        if team_id and organization_party_id:
            raise ValidationError("团队投稿与组织投稿只能二选一")
        if team_id:
            if conn.execute("SELECT 1 FROM creative_teams WHERE team_id=?",
                            (team_id,)).fetchone() is None:
                raise NotFoundError("团队不存在")
            _, designated = self._team_roster(conn, team_id)
            if not designated:
                raise ValidationError("团队尚未指定受益创作主体")
            return designated
        if organization_party_id:
            row = conn.execute(
                "SELECT beneficial_person_id FROM organizations_ext WHERE organization_party_id=?",
                (organization_party_id,),
            ).fetchone()
            if row is None:
                raise NotFoundError("组织主体不存在")
            return row["beneficial_person_id"]
        return submitter_person_id

    def _check_relation(self, conn, *, relation_id, beneficiary_person_id,
                        acting_person_id, action: str):
        """校验监护/授权关系；无关系时仅允许本人操作。返回关系行（或 None）。"""

        if not relation_id:
            if acting_person_id != beneficiary_person_id:
                raise PermissionDenied("代理他人投稿必须提供有效监护或授权关系")
            return None
        row = conn.execute("SELECT * FROM representation_relations WHERE relation_id=?",
                           (relation_id,)).fetchone()
        if row is None:
            raise NotFoundError("代理关系不存在")
        if row["status"] != "active":
            raise PermissionDenied("代理关系已撤销")
        if row["subject_person_id"] != beneficiary_person_id:
            raise ValidationError("代理关系的被代理人不是该作品的受益创作主体")
        if row["representative_person_id"] != acting_person_id:
            raise PermissionDenied("当前操作者不是该代理关系的代理人")
        scope = json.loads(row["scope_json"])
        if action not in scope:
            raise PermissionDenied(f"代理授权范围不含 {action}")
        now = self._now()
        if now < parse_ts(row["valid_from"]):
            raise PermissionDenied("代理关系尚未生效")
        if row["valid_until"] and now >= parse_ts(row["valid_until"]):
            raise PermissionDenied("代理关系已过期")
        return row

    def _acquire_hold(self, conn, *, window_id, beneficiary_person_id, submission_id, track_id) -> None:
        """在窗口维度保证同一受益主体只有一个生效资格占位。"""

        track = conn.execute("SELECT * FROM competition_tracks WHERE track_id=?",
                             (track_id,)).fetchone()
        if track is None:
            raise NotFoundError("赛道不存在")
        existing = conn.execute(
            "SELECT * FROM qualification_holds WHERE window_id=? AND mutex_group=? "
            "AND beneficiary_person_id=? AND status='held' AND submission_id!=?",
            (window_id, track["mutex_group"], beneficiary_person_id, submission_id),
        ).fetchone()
        if existing is not None:
            reason = (f"受益创作主体 {beneficiary_person_id} 已通过作品 {existing['submission_id']} "
                      f"在赛道 {existing['track_id']} 持有生效资格，不能再借关联身份占用 {track_id}")
            # 冲突记录必须在主事务回滚后独立留痕，故详情随异常带出
            raise DuplicateQualificationError(reason, {
                "window_id": window_id, "mutex_group": track["mutex_group"],
                "beneficiary_person_id": beneficiary_person_id,
                "existing_submission_id": existing["submission_id"],
                "attempted_submission_id": submission_id,
                "existing_track_id": existing["track_id"],
                "attempted_track_id": track_id, "reason": reason})
        held = conn.execute(
            "SELECT COUNT(*) AS c FROM qualification_holds WHERE window_id=? AND track_id=? "
            "AND status='held'", (window_id, track_id),
        ).fetchone()["c"]
        if held >= track["quota"]:
            raise ConflictError(f"赛道 {track_id} 名额已满")
        conn.execute(
            "INSERT INTO qualification_holds(hold_id,window_id,mutex_group,beneficiary_person_id,"
            "submission_id,track_id,status,created_at) VALUES(?,?,?,?,?,?, 'held', ?)",
            (uuid.uuid4().hex, window_id, track["mutex_group"], beneficiary_person_id,
             submission_id, track_id, self._now_text()),
        )

    def _record_conflict(self, detail: dict[str, Any], *, actor_id: str) -> None:
        """在失败事务回滚之后，用独立事务持久化冲突依据并写审计链。"""

        with self.database.transaction(immediate=True) as conn:
            conn.execute(
                "INSERT INTO conflict_records(conflict_id,window_id,mutex_group,beneficiary_person_id,"
                "existing_submission_id,attempted_submission_id,reason,created_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (uuid.uuid4().hex, detail["window_id"], detail["mutex_group"],
                 detail["beneficiary_person_id"], detail["existing_submission_id"],
                 detail["attempted_submission_id"], detail["reason"], self._now_text()),
            )
            self._audit(conn, actor_id=actor_id, action="qualification.conflict_detected",
                        resource_type="conflict",
                        resource_id=detail["attempted_submission_id"], detail=detail)

    def _release_hold(self, conn, *, window_id, submission_id) -> None:
        conn.execute(
            "UPDATE qualification_holds SET status='released', released_at=? "
            "WHERE submission_id=? AND status='held'",
            (self._now_text(), submission_id),
        )

    # ---------- 投稿生命周期 ----------

    def submit(self, *, request_id: str, caller_person_id: str, window_id: str, track_id: str,
               title: str, material: dict[str, Any], submitter_person_id: str | None = None,
               team_id: str | None = None, organization_party_id: str | None = None,
               relation_id: str | None = None) -> WriteReceipt:
        if not isinstance(material, dict) or not material:
            raise ValidationError("material 必须是非空对象")
        submitter_person_id = submitter_person_id or caller_person_id
        payload = {"window_id": window_id, "track_id": track_id, "title": title,
                   "material": material, "submitter_person_id": submitter_person_id,
                   "team_id": team_id, "organization_party_id": organization_party_id,
                   "relation_id": relation_id}
        try:
            with self.database.transaction(immediate=True) as conn:
                replay = self._peek_receipt(conn, request_id=request_id, action="submit", payload=payload)
                if replay is not None:
                    return replay
                window = self._window(conn, window_id)
                self._assert_open(window)
                if not self._person_exists(conn, submitter_person_id):
                    raise NotFoundError("投稿自然人不存在")
                candidate = self._resolve_beneficiary(
                    conn, submitter_person_id=submitter_person_id, team_id=team_id,
                    organization_party_id=organization_party_id)
                if relation_id:
                    rel_row = conn.execute(
                        "SELECT * FROM representation_relations WHERE relation_id=?",
                        (relation_id,)).fetchone()
                    if rel_row is None:
                        raise NotFoundError("代理关系不存在")
                    if rel_row["subject_person_id"] != candidate:
                        raise ValidationError("代理关系的被代理人与作品受益创作主体不一致")
                    beneficiary = rel_row["subject_person_id"]
                    self._check_relation(conn, relation_id=relation_id,
                                         beneficiary_person_id=beneficiary,
                                         acting_person_id=caller_person_id, action="submit")
                else:
                    beneficiary = candidate
                    if team_id:
                        members, _ = self._team_roster(conn, team_id)
                        if caller_person_id != beneficiary and caller_person_id not in members:
                            raise PermissionDenied("团队作品只能由受益主体或团队成员提交")
                    elif caller_person_id != beneficiary:
                        raise PermissionDenied("代理他人投稿必须提供有效监护或授权关系")
                title = self._text(title, "title", 200)
                track_id = self._id(track_id, "track_id")
                now = self._now_text()
                submission_id = uuid.uuid4().hex

                def create() -> tuple[str, str, dict[str, Any]]:
                    conn.execute(
                        "INSERT INTO submissions(submission_id,window_id,site_id,title,"
                        "submitter_person_id,team_id,organization_party_id,relation_id,"
                        "beneficiary_person_id,current_track_id,status,created_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?, 'pending', ?)",
                        (submission_id, window_id, window["site_id"], title, submitter_person_id,
                         team_id, organization_party_id, relation_id, beneficiary, track_id, now),
                    )
                    material_hash = digest(material)
                    conn.execute(
                        "INSERT INTO submission_versions(submission_id,version_seq,track_id,"
                        "material_json,material_hash,created_by,created_at) VALUES(?,1,?,?,?,?,?)",
                        (submission_id, track_id, canonical_json(material), material_hash,
                         caller_person_id, now),
                    )
                    conn.execute(
                        "INSERT INTO entry_events(submission_id,event_type,to_track_id,material_json,"
                        "material_hash,actor_id,occurred_at) VALUES(?, 'submitted', ?,?,?,?,?)",
                        (submission_id, track_id, canonical_json(material), material_hash,
                         caller_person_id, now),
                    )
                    # 资格占位最后获取：冲突时主事务整体回滚，不会留下半成品
                    self._acquire_hold(conn, window_id=window_id, beneficiary_person_id=beneficiary,
                                       submission_id=submission_id, track_id=track_id)
                    conn.execute(
                        "INSERT INTO review_tasks(task_id,submission_id,state,created_at,updated_at) "
                        "VALUES(?,?, 'queued', ?,?)",
                        (uuid.uuid4().hex, submission_id, now, now),
                    )
                    self._audit(conn, actor_id=caller_person_id, action="submission.submitted",
                                resource_type="submission", resource_id=submission_id,
                                detail={"window_id": window_id, "track_id": track_id,
                                        "beneficiary_person_id": beneficiary,
                                        "material_hash": material_hash})
                    return "submission", submission_id, {"submission_id": submission_id, "version_seq": 1}

                return self._idempotent(conn, request_id=request_id, action="submit",
                                        payload=payload, create=create)
        except DuplicateQualificationError as exc:
            # 主事务已回滚；在独立事务中持久化冲突依据，供审核员/审计员追溯
            self._record_conflict(exc.detail, actor_id=caller_person_id)
            raise

    def _get_submission(self, conn, submission_id: str):
        row = conn.execute("SELECT * FROM submissions WHERE submission_id=?",
                           (submission_id,)).fetchone()
        if row is None:
            raise NotFoundError("作品不存在")
        return row

    def _assert_actor_on_submission(self, conn, sub, person_id: str, action: str) -> None:
        if person_id == sub["submitter_person_id"] or person_id == sub["beneficiary_person_id"]:
            return
        if sub["team_id"]:
            members, _ = self._team_roster(conn, sub["team_id"])
            if person_id in members:
                return
        if sub["relation_id"]:
            self._check_relation(conn, relation_id=sub["relation_id"],
                                 beneficiary_person_id=sub["beneficiary_person_id"],
                                 acting_person_id=person_id, action=action)
            return
        raise PermissionDenied("无权操作该作品")

    def _add_version(self, conn, *, sub, track_id, material, actor_id: str,
                     event_type: str, from_track_id: str | None = None, note: str = "") -> int:
        next_seq = conn.execute(
            "SELECT COALESCE(MAX(version_seq),0)+1 AS seq FROM submission_versions "
            "WHERE submission_id=?", (sub["submission_id"],)
        ).fetchone()["seq"]
        now = self._now_text()
        material_json = canonical_json(material)
        material_hash = digest(material)
        conn.execute(
            "INSERT INTO submission_versions(submission_id,version_seq,track_id,material_json,"
            "material_hash,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
            (sub["submission_id"], next_seq, track_id, material_json, material_hash, actor_id, now),
        )
        conn.execute(
            "INSERT INTO entry_events(submission_id,event_type,from_track_id,to_track_id,"
            "material_json,material_hash,note,actor_id,occurred_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (sub["submission_id"], event_type, from_track_id, track_id, material_json,
             material_hash, note, actor_id, now),
        )
        return next_seq

    def correct(self, *, request_id: str, caller_person_id: str, submission_id: str,
                material: dict[str, Any], note: str = "") -> WriteReceipt:
        if not isinstance(material, dict) or not material:
            raise ValidationError("material 必须是非空对象")
        payload = {"submission_id": submission_id, "material": material, "note": note}
        with self.database.transaction(immediate=True) as conn:
            replay = self._peek_receipt(conn, request_id=request_id, action="correct", payload=payload)
            if replay is not None:
                return replay
            sub = self._get_submission(conn, submission_id)
            window = self._window(conn, sub["window_id"])
            self._assert_open(window)
            self._assert_actor_on_submission(conn, sub, caller_person_id, "correct")
            if sub["status"] not in REVIEWABLE_STATUSES:
                raise ConflictError(f"当前状态 {sub['status']} 不允许补正")

            def create() -> tuple[str, str, dict[str, Any]]:
                seq = self._add_version(conn, sub=sub, track_id=sub["current_track_id"],
                                        material=material, actor_id=caller_person_id,
                                        event_type="corrected", note=note)
                conn.execute(
                    "UPDATE submissions SET status='pending' WHERE submission_id=?",
                    (submission_id,),
                )
                # 补正产生待审新材料：无论上一轮是否终结都重新入队并清除旧决定
                conn.execute(
                    "UPDATE review_tasks SET state='queued', decision=NULL, reason='', "
                    "appeal_id=NULL, updated_at=? WHERE submission_id=?",
                    (self._now_text(), submission_id),
                )
                self._audit(conn, actor_id=caller_person_id, action="submission.corrected",
                            resource_type="submission", resource_id=submission_id,
                            detail={"version_seq": seq})
                return "submission", submission_id, {"submission_id": submission_id, "version_seq": seq}

            return self._idempotent(conn, request_id=request_id, action="correct",
                                    payload=payload, create=create)

    def withdraw(self, *, request_id: str, caller_person_id: str, submission_id: str,
                 reason: str = "") -> WriteReceipt:
        payload = {"submission_id": submission_id, "reason": reason}
        with self.database.transaction(immediate=True) as conn:
            replay = self._peek_receipt(conn, request_id=request_id, action="withdraw", payload=payload)
            if replay is not None:
                return replay
            sub = self._get_submission(conn, submission_id)
            window = self._window(conn, sub["window_id"])
            self._assert_open(window)
            self._assert_actor_on_submission(conn, sub, caller_person_id, "withdraw")
            if sub["status"] == "withdrawn":
                raise ConflictError("作品已撤回")

            def create() -> tuple[str, str, dict[str, Any]]:
                now = self._now_text()
                conn.execute(
                    "UPDATE submissions SET status='withdrawn' WHERE submission_id=?",
                    (submission_id,),
                )
                self._release_hold(conn, window_id=sub["window_id"], submission_id=submission_id)
                conn.execute(
                    "INSERT INTO entry_events(submission_id,event_type,note,actor_id,occurred_at) "
                    "VALUES(?, 'withdrawn', ?,?,?)",
                    (submission_id, reason, caller_person_id, now),
                )
                conn.execute(
                    "UPDATE review_tasks SET state='resolved', updated_at=? WHERE submission_id=?",
                    (now, submission_id),
                )
                self._audit(conn, actor_id=caller_person_id, action="submission.withdrawn",
                            resource_type="submission", resource_id=submission_id,
                            detail={"reason": reason})
                return "submission", submission_id, {"submission_id": submission_id, "status": "withdrawn"}

            return self._idempotent(conn, request_id=request_id, action="withdraw",
                                    payload=payload, create=create)

    def switch_track(self, *, request_id: str, caller_person_id: str, submission_id: str,
                     target_track_id: str, material: dict[str, Any] | None = None,
                     note: str = "") -> WriteReceipt:
        if material is not None and not isinstance(material, dict):
            raise ValidationError("material 必须是对象")
        payload = {"submission_id": submission_id, "target_track_id": target_track_id,
                   "material": material, "note": note}
        try:
            with self.database.transaction(immediate=True) as conn:
                replay = self._peek_receipt(conn, request_id=request_id, action="switch_track", payload=payload)
                if replay is not None:
                    return replay
                sub = self._get_submission(conn, submission_id)
                window = self._window(conn, sub["window_id"])
                self._assert_open(window)
                self._assert_actor_on_submission(conn, sub, caller_person_id, "switch_track")
                if sub["status"] not in REVIEWABLE_STATUSES:
                    raise ConflictError(f"当前状态 {sub['status']} 不允许换赛道")
                old_track = sub["current_track_id"]
                target_track_id = self._id(target_track_id, "target_track_id")
                if target_track_id == old_track:
                    raise ValidationError("目标赛道与当前赛道相同")
                target = conn.execute("SELECT * FROM competition_tracks WHERE track_id=?",
                                      (target_track_id,)).fetchone()
                if target is None:
                    raise NotFoundError("目标赛道不存在")

                def create() -> tuple[str, str, dict[str, Any]]:
                    # 同一事务内先释放旧占位，再获取新占位；任一步失败整体回滚，原占位保留。
                    # 若受益主体已在其它作品持有该互斥资格，_acquire_hold 拒绝并随异常带出冲突详情。
                    self._release_hold(conn, window_id=sub["window_id"], submission_id=submission_id)
                    self._acquire_hold(conn, window_id=sub["window_id"],
                                       beneficiary_person_id=sub["beneficiary_person_id"],
                                       submission_id=submission_id, track_id=target_track_id)
                    if material is None:
                        last = conn.execute(
                            "SELECT material_json FROM submission_versions WHERE submission_id=? "
                            "ORDER BY version_seq DESC LIMIT 1", (submission_id,),
                        ).fetchone()
                        material_value = json.loads(last["material_json"])
                    else:
                        material_value = material
                    seq = self._add_version(conn, sub=sub, track_id=target_track_id,
                                            material=material_value, actor_id=caller_person_id,
                                            event_type="track_switched", from_track_id=old_track,
                                            note=note or f"{old_track} -> {target_track_id}")
                    conn.execute(
                        "UPDATE submissions SET current_track_id=?, status='pending' WHERE submission_id=?",
                        (target_track_id, submission_id),
                    )
                    conn.execute(
                        "UPDATE review_tasks SET state='queued', decision=NULL, reason='', "
                        "appeal_id=NULL, updated_at=? WHERE submission_id=?",
                        (self._now_text(), submission_id),
                    )
                    self._audit(conn, actor_id=caller_person_id, action="submission.track_switched",
                                resource_type="submission", resource_id=submission_id,
                                detail={"from_track_id": old_track, "to_track_id": target_track_id,
                                        "version_seq": seq})
                    return "submission", submission_id, {
                        "submission_id": submission_id, "track_id": target_track_id, "version_seq": seq}

                return self._idempotent(conn, request_id=request_id, action="switch_track",
                                        payload=payload, create=create)
        except DuplicateQualificationError as exc:
            self._record_conflict(exc.detail, actor_id=caller_person_id)
            raise

    def change_team_member(self, *, request_id: str, caller_person_id: str, team_id: str,
                           person_id: str, change: str, note: str = "",
                           submission_id: str | None = None) -> WriteReceipt:
        if change not in ("joined", "left"):
            raise ValidationError("change 只能是 joined 或 left")
        payload = {"team_id": team_id, "person_id": person_id, "change": change,
                   "note": note, "submission_id": submission_id}
        with self.database.transaction(immediate=True) as conn:
            replay = self._peek_receipt(conn, request_id=request_id, action="change_team_member", payload=payload)
            if replay is not None:
                return replay
            if conn.execute("SELECT 1 FROM creative_teams WHERE team_id=?",
                            (team_id,)).fetchone() is None:
                raise NotFoundError("团队不存在")
            if conn.execute("SELECT 1 FROM persons WHERE person_id=?",
                            (person_id,)).fetchone() is None:
                raise NotFoundError("自然人不存在")
            members, designated = self._team_roster(conn, team_id)
            caller_kind = self._staff_or_person(conn, caller_person_id, "admin", "operator")
            if change == "joined" and person_id in members:
                raise ConflictError("该成员已在团队中")
            if change == "left":
                if person_id not in members:
                    raise ConflictError("该成员不在团队中")
                if designated == person_id:
                    raise ConflictError("受益创作主体不能退出，请先重新指定受益人")
            sub = None
            if submission_id:
                sub = self._get_submission(conn, submission_id)
                if sub["team_id"] != team_id:
                    raise ValidationError("作品不属于该团队")
                window_row = self._window(conn, sub["window_id"])
                self._assert_open(window_row)
                self._assert_actor_on_submission(conn, sub, caller_person_id, "member_change")
            elif caller_kind == "person":
                # 团队级变更：成员可添加他人、移除他人；本人可自行加入
                if change == "joined" and person_id != caller_person_id and caller_person_id not in members:
                    raise PermissionDenied("只有团队成员可以邀请他人加入")
                if change == "left" and caller_person_id not in members:
                    raise PermissionDenied("只有团队成员可以移除成员")
            now = self._now_text()

            def create() -> tuple[str, str, dict[str, Any]]:
                conn.execute(
                    "INSERT INTO team_member_events(member_event_id,team_id,person_id,change,"
                    "occurred_at,changed_by,note) VALUES(?,?,?,?,?,?,?)",
                    (uuid.uuid4().hex, team_id, person_id, change, now, caller_person_id, note),
                )
                self._audit(conn, actor_id=caller_person_id, action="team.member_changed",
                            resource_type="team", resource_id=team_id,
                            detail={"person_id": person_id, "change": change,
                                    "submission_id": submission_id})
                if submission_id:
                    conn.execute(
                        "INSERT INTO entry_events(submission_id,event_type,material_json,note,"
                        "actor_id,occurred_at) VALUES(?, 'member_changed', ?,?,?,?)",
                        (submission_id, canonical_json({"person_id": person_id, "change": change}),
                         note, caller_person_id, now),
                    )
                return "team", team_id, {"team_id": team_id, "person_id": person_id,
                                         "change": change}

            return self._idempotent(conn, request_id=request_id, action="change_team_member",
                                    payload=payload, create=create)

    # ---------- 审核（重启后继续） ----------

    def review_queue(self, actor_id: str) -> list[dict[str, Any]]:
        conn = self._readonly(actor_id, "reviewer", "admin", "auditor")
        rows = conn.execute(
            "SELECT t.*, s.title, s.current_track_id, s.status AS submission_status, "
            "s.beneficiary_person_id, s.window_id FROM review_tasks t JOIN submissions s "
            "ON t.submission_id=s.submission_id WHERE t.state!='resolved' "
            "ORDER BY t.created_at, t.task_id"
        ).fetchall()
        return [dict(row) for row in rows]

    def decide_review(self, *, request_id: str, actor_id: str, submission_id: str,
                      decision: str, reason: str = "") -> WriteReceipt:
        if decision not in ("approved", "rejected", "correction_requested"):
            raise ValidationError("decision 非法")
        payload = {"submission_id": submission_id, "decision": decision, "reason": reason}
        with self.database.transaction(immediate=True) as conn:
            self._staff(conn, actor_id, "reviewer", "admin")
            sub = self._get_submission(conn, submission_id)
            if sub["status"] == "withdrawn":
                raise ConflictError("作品已撤回，无需审核")
            task = conn.execute(
                "SELECT * FROM review_tasks WHERE submission_id=?", (submission_id,)
            ).fetchone()
            if task is None:
                raise NotFoundError("审核任务不存在")
            if task["state"] == "resolved":
                raise ConflictError("该审核任务已终结，需经补正或申诉成立重新排队后才能再审")
            # 冻结锁定的是被评审的版本，评审本身在截止后继续（含重启后续审）。
            now = self._now_text()
            new_status = {"approved": "approved", "rejected": "rejected",
                          "correction_requested": "awaiting_correction"}[decision]

            def create() -> tuple[str, str, dict[str, Any]]:
                conn.execute(
                    "UPDATE review_tasks SET state='resolved', assigned_to=?, decision=?, reason=?, "
                    "updated_at=? WHERE submission_id=?",
                    (actor_id, decision, reason, now, submission_id),
                )
                conn.execute(
                    "UPDATE submissions SET status=? WHERE submission_id=?",
                    (new_status, submission_id),
                )
                conn.execute(
                    "INSERT INTO entry_events(submission_id,event_type,material_json,note,"
                    "actor_id,occurred_at) VALUES(?, 'reviewed', ?,?,?,?)",
                    (submission_id, canonical_json({"decision": decision, "reason": reason}),
                     reason, actor_id, now),
                )
                self._audit(conn, actor_id=actor_id, action="submission.reviewed",
                            resource_type="submission", resource_id=submission_id,
                            detail={"decision": decision, "reason": reason})
                return "submission", submission_id, {"submission_id": submission_id,
                                                     "status": new_status}

            return self._idempotent(conn, request_id=request_id, action="decide_review",
                                    payload=payload, create=create)

    # ---------- 申诉（迟到材料的唯一通道） ----------

    def file_appeal(self, *, request_id: str, caller_person_id: str, submission_id: str,
                    material: dict[str, Any], reason: str) -> WriteReceipt:
        if not isinstance(material, dict) or not material:
            raise ValidationError("material 必须是非空对象")
        reason = self._text(reason, "reason", 1000)
        payload = {"submission_id": submission_id, "material": material, "reason": reason}
        with self.database.transaction(immediate=True) as conn:
            replay = self._peek_receipt(conn, request_id=request_id, action="file_appeal", payload=payload)
            if replay is not None:
                return replay
            sub = self._get_submission(conn, submission_id)
            window = self._window(conn, sub["window_id"])
            if sub["status"] == "withdrawn":
                raise ConflictError("作品已撤回，不能申诉")
            # 申诉是截止后的唯一入口：窗口开放时应当走补正
            if self._now() < parse_ts(window["deadline_at"]) and window["status"] != "frozen":
                raise ConflictError("窗口尚未截止，应直接补正而非申诉")
            self._assert_actor_on_submission(conn, sub, caller_person_id, "appeal")
            now = self._now_text()

            def create() -> tuple[str, str, dict[str, Any]]:
                appeal_id = uuid.uuid4().hex
                conn.execute(
                    "INSERT INTO appeals(appeal_id,submission_id,material_json,material_hash,reason,"
                    "state,created_by,created_at) VALUES(?,?,?,?,?, 'submitted', ?,?)",
                    (appeal_id, submission_id, canonical_json(material), digest(material), reason,
                     caller_person_id, now),
                )
                conn.execute(
                    "INSERT INTO entry_events(submission_id,event_type,material_json,note,"
                    "actor_id,occurred_at) VALUES(?, 'appeal_submitted', ?,?,?,?)",
                    (submission_id, canonical_json({"appeal_id": appeal_id}), reason,
                     caller_person_id, now),
                )
                self._audit(conn, actor_id=caller_person_id, action="appeal.filed",
                            resource_type="appeal", resource_id=appeal_id,
                            detail={"submission_id": submission_id})
                return "appeal", appeal_id, {"appeal_id": appeal_id,
                                             "submission_id": submission_id}

            return self._idempotent(conn, request_id=request_id, action="file_appeal",
                                    payload=payload, create=create)

    def decide_appeal(self, *, request_id: str, actor_id: str, appeal_id: str,
                      decision: str, decision_note: str = "") -> WriteReceipt:
        if decision not in ("accepted", "rejected"):
            raise ValidationError("decision 只能是 accepted 或 rejected")
        payload = {"appeal_id": appeal_id, "decision": decision, "decision_note": decision_note}
        with self.database.transaction(immediate=True) as conn:
            self._staff(conn, actor_id, "reviewer", "admin")
            appeal = conn.execute("SELECT * FROM appeals WHERE appeal_id=?",
                                  (appeal_id,)).fetchone()
            if appeal is None:
                raise NotFoundError("申诉不存在")
            if appeal["state"] != "submitted":
                raise ConflictError("申诉已处理")
            now = self._now_text()

            def create() -> tuple[str, str, dict[str, Any]]:
                conn.execute(
                    "UPDATE appeals SET state=?, decision_note=?, decided_at=? WHERE appeal_id=?",
                    (decision, decision_note, now, appeal_id),
                )
                conn.execute(
                    "INSERT INTO entry_events(submission_id,event_type,material_json,note,"
                    "actor_id,occurred_at) VALUES(?, 'appeal_decided', ?,?,?,?)",
                    (appeal["submission_id"],
                     canonical_json({"appeal_id": appeal_id, "decision": decision}),
                     decision_note, actor_id, now),
                )
                # 申诉成立：在不改写冻结版本的前提下重新排队复核，并以 appeal_id 留痕来源
                if decision == "accepted":
                    conn.execute(
                        "UPDATE review_tasks SET state='queued', decision=NULL, reason=?, "
                        "appeal_id=?, updated_at=? WHERE submission_id=?",
                        (f"申诉成立：{decision_note}", appeal_id, now, appeal["submission_id"]),
                    )
                self._audit(conn, actor_id=actor_id, action="appeal.decided",
                            resource_type="appeal", resource_id=appeal_id,
                            detail={"decision": decision})
                return "appeal", appeal_id, {"appeal_id": appeal_id, "state": decision}

            return self._idempotent(conn, request_id=request_id, action="decide_appeal",
                                    payload=payload, create=create)

    # ---------- 查询 / 时间旅行 ----------

    def _submission_view(self, conn, row) -> SubmissionView:
        version = conn.execute(
            "SELECT MAX(version_seq) AS seq FROM submission_versions WHERE submission_id=?",
            (row["submission_id"],),
        ).fetchone()["seq"]
        return SubmissionView(
            submission_id=row["submission_id"], window_id=row["window_id"], title=row["title"],
            track_id=row["current_track_id"], status=row["status"],
            beneficiary_person_id=row["beneficiary_person_id"],
            submitter_person_id=row["submitter_person_id"], team_id=row["team_id"],
            organization_party_id=row["organization_party_id"], relation_id=row["relation_id"],
            current_version=version or 0, created_at=row["created_at"])

    def my_submissions(self, person_id: str) -> list[dict[str, Any]]:
        conn = self.database.connection
        rows = conn.execute("SELECT * FROM submissions ORDER BY created_at").fetchall()
        result = []
        for row in rows:
            related = (row["submitter_person_id"] == person_id
                       or row["beneficiary_person_id"] == person_id)
            if not related and row["team_id"]:
                members, _ = self._team_roster(conn, row["team_id"])
                related = person_id in members
            if not related and row["relation_id"]:
                rel = conn.execute(
                    "SELECT representative_person_id FROM representation_relations WHERE relation_id=?",
                    (row["relation_id"],),
                ).fetchone()
                related = bool(rel and rel["representative_person_id"] == person_id)
            if related:
                view = self._submission_view(conn, row)
                task = conn.execute(
                    "SELECT state,decision,reason FROM review_tasks WHERE submission_id=?",
                    (row["submission_id"],),
                ).fetchone()
                appeals = conn.execute(
                    "SELECT appeal_id,reason,state,decision_note,created_at,decided_at "
                    "FROM appeals WHERE submission_id=? ORDER BY created_at",
                    (row["submission_id"],),
                ).fetchall()
                latest = conn.execute(
                    "SELECT material_json FROM submission_versions WHERE submission_id=? "
                    "ORDER BY version_seq DESC LIMIT 1", (row["submission_id"],),
                ).fetchone()
                result.append({
                    **view.__dict__,
                    "review": dict(task) if task else None,
                    "appeals": [dict(a) for a in appeals],
                    "latest_material": json.loads(latest["material_json"]) if latest else None,
                })
        return result

    def list_submissions(self, actor_id: str, window_id: str | None = None) -> list[dict[str, Any]]:
        conn = self._readonly(actor_id, "reviewer", "admin", "auditor", "operator")
        query = "SELECT * FROM submissions"
        params: list[Any] = []
        if window_id:
            query += " WHERE window_id=?"
            params.append(window_id)
        query += " ORDER BY created_at"
        rows = conn.execute(query, params).fetchall()
        items = []
        for row in rows:
            view = self._submission_view(conn, row).__dict__
            hold = conn.execute(
                "SELECT hold_id,track_id,status,created_at,released_at FROM qualification_holds "
                "WHERE submission_id=? ORDER BY created_at", (row["submission_id"],),
            ).fetchall()
            view["holds"] = [dict(h) for h in hold]
            task = conn.execute(
                "SELECT task_id,state,decision,reason,assigned_to FROM review_tasks "
                "WHERE submission_id=?", (row["submission_id"],),
            ).fetchone()
            view["review"] = dict(task) if task else None
            items.append(view)
        return items

    def list_conflicts(self, actor_id: str, window_id: str | None = None) -> list[dict[str, Any]]:
        conn = self._readonly(actor_id, "reviewer", "admin", "auditor")
        query = "SELECT * FROM conflict_records"
        params: list[Any] = []
        if window_id:
            query += " WHERE window_id=?"
            params.append(window_id)
        query += " ORDER BY created_at"
        return [dict(row) for row in conn.execute(query, params).fetchall()]

    def list_appeals(self, actor_id: str) -> list[dict[str, Any]]:
        conn = self.database.connection
        staff = self._staff(conn, actor_id, "reviewer", "admin", "auditor")
        # 审核员/管理员可见材料全文；审计员仅见哈希与处理进度
        if staff["role"] in ("reviewer", "admin"):
            rows = conn.execute(
                "SELECT appeal_id,submission_id,material_json,reason,state,decision_note,"
                "created_by,created_at,decided_at,material_hash FROM appeals ORDER BY created_at"
            ).fetchall()
            items = []
            for row in rows:
                item = dict(row)
                item["material"] = json.loads(item.pop("material_json"))
                items.append(item)
            return items
        rows = conn.execute(
            "SELECT appeal_id,submission_id,reason,state,decision_note,created_by,created_at,"
            "decided_at,material_hash FROM appeals ORDER BY created_at"
        ).fetchall()
        return [dict(row) for row in rows]

    def track_occupancy(self, actor_id: str, window_id: str) -> list[dict[str, Any]]:
        conn = self._readonly(actor_id, "reviewer", "admin", "auditor", "operator")
        rows = conn.execute(
            "SELECT t.track_id,t.name,t.quota,COUNT(h.hold_id) AS held "
            "FROM competition_tracks t LEFT JOIN qualification_holds h "
            "ON h.track_id=t.track_id AND h.window_id=? AND h.status='held' "
            "GROUP BY t.track_id ORDER BY t.track_id", (window_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def timeline(self, *, submission_id: str, actor_id: str | None = None,
                 person_id: str | None = None) -> dict[str, Any]:
        """返回作品不可覆盖事件账本与版本链。"""

        conn = self.database.connection
        sub = self._get_submission(conn, submission_id)
        can_see_material = False
        if actor_id:
            staff = self._staff(conn, actor_id, "reviewer", "admin", "auditor", "operator")
            # 只有负责资格审核的角色可见材料全文；审计员只见哈希用于完整性核验
            can_see_material = staff["role"] in ("reviewer", "admin")
        elif person_id:
            self._assert_actor_on_submission(conn, sub, person_id, "read")
            can_see_material = True
        else:
            raise PermissionDenied("缺少访问身份")

        def decode(row_dict):
            if can_see_material and row_dict.get("material_json"):
                row_dict["material"] = json.loads(row_dict.pop("material_json"))
            else:
                row_dict.pop("material_json", None)
            return row_dict

        events = []
        for row in conn.execute(
            "SELECT event_seq,event_type,from_track_id,to_track_id,material_json,material_hash,note,"
            "actor_id,occurred_at FROM entry_events WHERE submission_id=? ORDER BY event_seq",
            (submission_id,),
        ):
            events.append(decode(dict(row)))
        versions = []
        for row in conn.execute(
            "SELECT version_seq,track_id,material_json,material_hash,created_by,created_at "
            "FROM submission_versions WHERE submission_id=? ORDER BY version_seq",
            (submission_id,),
        ):
            versions.append(decode(dict(row)))
        return {"submission": self._submission_view(conn, sub).__dict__,
                "events": events, "versions": versions,
                "material_visible": can_see_material}

    def explain(self, *, submission_id: str, at: str | None = None,
                actor_id: str | None = None, person_id: str | None = None) -> dict[str, Any]:
        """重放事件账本，解释作品在任意历史时点为何有效/被拒/待补正。"""

        conn = self.database.connection
        sub = self._get_submission(conn, submission_id)
        if actor_id:
            self._staff(conn, actor_id, "reviewer", "admin", "auditor", "operator")
        elif person_id:
            self._assert_actor_on_submission(conn, sub, person_id, "read")
        else:
            raise PermissionDenied("缺少访问身份")

        cutoff = parse_ts(at) if at else self._now()
        status = None
        track_id = None
        effective_version = 0
        reasons: list[dict[str, Any]] = []
        frozen = False
        for row in conn.execute(
            "SELECT * FROM entry_events WHERE submission_id=? ORDER BY event_seq",
            (submission_id,),
        ):
            occurred = parse_ts(row["occurred_at"])
            if occurred > cutoff:
                break
            kind = row["event_type"]
            if kind == "submitted":
                status, track_id, effective_version = "pending", row["to_track_id"], 1
                reasons.append({"at": row["occurred_at"], "type": kind,
                                "message": "作品已提交，进入资格审核"})
            elif kind == "corrected":
                status, effective_version = "pending", effective_version + 1
                reasons.append({"at": row["occurred_at"], "type": kind,
                                "message": f"已提交补正材料（版本 {effective_version}），重新等待审核"})
            elif kind == "withdrawn":
                status = "withdrawn"
                reasons.append({"at": row["occurred_at"], "type": kind,
                                "message": f"投稿人撤回：{row['note']}"})
            elif kind == "track_switched":
                track_id = row["to_track_id"]
                effective_version += 1
                status = "pending"
                reasons.append({"at": row["occurred_at"], "type": kind,
                                "message": f"换赛道至 {track_id}（版本 {effective_version}），重新排队审核"})
            elif kind == "reviewed":
                material = json.loads(row["material_json"])
                decision = material["decision"]
                status = {"approved": "approved", "rejected": "rejected",
                          "correction_requested": "awaiting_correction"}[decision]
                word = {"approved": "审核通过，资格有效",
                        "rejected": "审核拒绝",
                        "correction_requested": "审核员要求补正"}[decision]
                reasons.append({"at": row["occurred_at"], "type": kind,
                                "message": f"{word}：{material.get('reason', '')}"})
            elif kind == "member_changed":
                reasons.append({"at": row["occurred_at"], "type": kind,
                                "message": f"团队成员变更：{row['material_json']}"})
            elif kind == "frozen":
                frozen = True
                reasons.append({"at": row["occurred_at"], "type": kind,
                                "message": "截止时已原子冻结生效版本"})
            elif kind == "appeal_submitted":
                reasons.append({"at": row["occurred_at"], "type": kind,
                                "message": "截止后提交申诉材料（不改动原申请）"})
            elif kind == "appeal_decided":
                material = json.loads(row["material_json"])
                if material["decision"] == "accepted":
                    status = "pending"
                reasons.append({"at": row["occurred_at"], "type": kind,
                                "message": f"申诉结论：{material['decision']}"})

        # 冻结快照（若时点已在冻结之后）
        snapshot = conn.execute(
            "SELECT * FROM freeze_snapshots WHERE submission_id=? AND frozen_at<=?",
            (submission_id, ts_text(cutoff)),
        ).fetchone()
        conflicts = [dict(r) for r in conn.execute(
            "SELECT conflict_id,existing_submission_id,attempted_submission_id,reason,created_at "
            "FROM conflict_records WHERE attempted_submission_id=? OR existing_submission_id=?",
            (submission_id, submission_id),
        ).fetchall()]
        hold_rows = conn.execute(
            "SELECT track_id,status,created_at,released_at FROM qualification_holds "
            "WHERE submission_id=? ORDER BY created_at", (submission_id,),
        ).fetchall()
        return {
            "submission_id": submission_id,
            "at": ts_text(cutoff),
            "status": status or "not_yet_submitted",
            "track_id": track_id,
            "effective_version": effective_version,
            "frozen": frozen,
            "frozen_version": snapshot["version_seq"] if snapshot else None,
            "reasons": reasons,
            "qualification_holds": [dict(h) for h in hold_rows],
            "conflicts": conflicts,
        }

    # ---------- 主体读取 ----------

    def get_person(self, person_id: str) -> Person:
        row = self.database.connection.execute(
            "SELECT person_id,display_name,is_minor,id_masked FROM persons WHERE person_id=?",
            (person_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError("自然人不存在")
        return Person(row["person_id"], row["display_name"], bool(row["is_minor"]),
                      row["id_masked"])

    def get_window(self, window_id: str) -> RegistrationWindow:
        row = self._window(self.database.connection, window_id)
        return RegistrationWindow(row["window_id"], row["site_id"], row["opens_at"],
                                  row["deadline_at"], row["status"], row["frozen_at"])

    def get_track(self, track_id: str) -> Track:
        row = self.database.connection.execute(
            "SELECT * FROM competition_tracks WHERE track_id=?", (track_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError("赛道不存在")
        return Track(row["track_id"], row["site_id"], row["name"], row["quota"],
                     row["mutex_group"])
