"""参赛资格与投稿治理服务的离线端到端验收。

剧情覆盖：

1. 同一创作团队以个人、工作室、代理人三种身份建档并汇聚到同一受益主体；
2. 未成年作者由监护人代签；
3. 个人/工作室重复占位被互斥规则拦截；
4. 临近截止换赛道、补正、撤回形成不可覆盖事件；
5. 截止原子冻结生效版本，迟到材料只能进入申诉且不回写原申请；
6. 幂等重放不产生第二份报名；
7. 服务“重启”后未完成审核仍在队列中继续处理；
8. 任意历史时点解释作品为何在审/等待补正/有效/被拒；
9. 全局哈希审计链保持完整。
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .clock import FixedClock
from .eligibility_service import EligibilityService
from .errors import (
    EligibilityConflictError,
    FrozenWindowError,
    WindowClosedError,
)
from .service import DomainService
from .storage import Database


def _staff(service: DomainService) -> None:
    service.register_organization(request_id="org", actor_id="bootstrap",
                                  organization_id="o1", name="白塔杯组委会")
    service.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="a1",
                           display_name="管理员", role="admin", organization_id="o1")
    service.register_actor(request_id="reviewer", actor_id="a1", new_actor_id="rv1",
                           display_name="资格审核员", role="reviewer", organization_id="o1")
    service.register_actor(request_id="auditor", actor_id="a1", new_actor_id="au1",
                           display_name="审计员", role="auditor", organization_id="o1")


def run() -> dict[str, object]:
    with tempfile.TemporaryDirectory() as directory:
        db_path = Path(directory) / "eligibility_acceptance.sqlite3"
        start = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)
        clock = FixedClock(start)
        database = Database(db_path)
        foundation = DomainService(database, clock)
        service = EligibilityService(database, clock)
        _staff(foundation)

        # --- 建档：个人、工作室、代理人、未成年人与监护人 ---
        service.register_person(request_id="p-lead", actor_id="a1", person_id="lead",
                                legal_name="主创林一", id_doc_hash="hash-lead")
        service.register_person(request_id="p-agent", actor_id="a1", person_id="agent",
                                legal_name="代理人周仲", id_doc_hash="hash-agent")
        service.register_person(request_id="p-minor", actor_id="a1", person_id="minor",
                                legal_name="少年作者武小", id_doc_hash="hash-minor",
                                birth_date="2012-05-01")
        service.register_person(request_id="p-guard", actor_id="a1", person_id="guard",
                                legal_name="监护人武父", id_doc_hash="hash-guard")
        service.register_external_org(request_id="o-studio", actor_id="a1",
                                      external_org_id="studio", name="纹韵工作室",
                                      license_hash="lic-studio")
        service.register_person(request_id="p-lead2", actor_id="a1", person_id="lead2",
                                legal_name="另一主创陈二", id_doc_hash="hash-lead2")

        service.add_relationship(request_id="r-agent", actor_id="a1", relationship_id="ragent",
                                 kind="authorized_agent", principal_type="person",
                                 principal_id="lead", agent_type="person", agent_id="agent",
                                 document_hash="poa-1",
                                 valid_from="2026-09-01T00:00:00Z")
        service.add_relationship(request_id="r-guard", actor_id="a1", relationship_id="rguard",
                                 kind="guardian", principal_type="person",
                                 principal_id="minor", agent_type="person", agent_id="guard",
                                 document_hash="guard-cert-1",
                                 valid_from="2026-09-01T00:00:00Z")

        service.register_subject(request_id="subj", actor_id="a1", subject_id="team-beneficiary",
                                 display_name="白塔杯一号创作团队")
        service.link_subject_party(request_id="link-lead", actor_id="a1",
                                   subject_id="team-beneficiary", party_type="person",
                                   party_id="lead", link_role="self")
        service.link_subject_party(request_id="link-studio", actor_id="a1",
                                   subject_id="team-beneficiary", party_type="org",
                                   party_id="studio", link_role="studio")
        service.link_subject_party(request_id="link-agent", actor_id="a1",
                                   subject_id="team-beneficiary", party_type="person",
                                   party_id="agent", link_role="member")
        service.link_subject_party(request_id="link-lead2", actor_id="a1",
                                   subject_id="team-beneficiary", party_type="person",
                                   party_id="lead2", link_role="member")
        service.register_team(request_id="team", actor_id="a1", team_id="team-1",
                              subject_id="team-beneficiary", name="一号创作团队",
                              members=[{"person_id": "lead", "role": "主创"},
                                       {"person_id": "lead2", "role": "协创"}])

        # 未成年人单独的受益主体。
        service.register_subject(request_id="subj-minor", actor_id="a1",
                                 subject_id="minor-beneficiary", display_name="少年作者主体")
        service.link_subject_party(request_id="link-minor", actor_id="a1",
                                   subject_id="minor-beneficiary", party_type="person",
                                   party_id="minor", link_role="self")

        # --- 赛道与窗口（两赛道同属互斥组，名额各 2）---
        service.register_track(request_id="t-wenyun", actor_id="a1", track_id="wenyun",
                               name="西城纹韵", slots_total=2, exclusive_group="xicheng-2026")
        service.register_track(request_id="t-haowu", actor_id="a1", track_id="haowu",
                               name="西城好物", slots_total=2, exclusive_group="xicheng-2026")
        opens = start
        closes = start + timedelta(days=7)
        service.register_window(request_id="win", actor_id="a1", window_id="w1",
                                name="白塔杯报名窗口",
                                opens_at=opens.isoformat().replace("+00:00", "Z"),
                                closes_at=closes.isoformat().replace("+00:00", "Z"),
                                track_ids=["wenyun", "haowu"])

        # --- 个人身份投稿“西城纹韵”，幂等重放不产生第二份 ---
        first = service.submit(
            request_id="submit-personal", actor_id="person:lead", window_id="w1",
            track_id="wenyun", applicant_type="person", applicant_id="lead",
            title="纹样初版", content={"sketch": "v1"})
        replay = service.submit(
            request_id="submit-personal", actor_id="person:lead", window_id="w1",
            track_id="wenyun", applicant_type="person", applicant_id="lead",
            title="纹样初版", content={"sketch": "v1"})
        assert not first.replayed and replay.replayed
        submission_id = first.resource_id

        # --- 工作室身份再投同互斥组：必须被拦截并给出冲突依据 ---
        blocked = None
        try:
            service.submit(
                request_id="submit-studio-dup", actor_id="org:studio", window_id="w1",
                track_id="wenyun", applicant_type="org", applicant_id="studio",
                title="工作室同名作品", content={"sketch": "dup"})
        except EligibilityConflictError as exc:
            blocked = exc.conflicts
        assert blocked and blocked[0]["submission_id"] == submission_id

        # --- 代理人替另一自然人投稿也会因同主体被拦截 ---
        blocked_agent = False
        try:
            service.submit(
                request_id="submit-agent-dup", actor_id="person:agent", window_id="w1",
                track_id="haowu", applicant_type="person", applicant_id="agent",
                represented_type="person", represented_id="lead",
                title="代理人投递", content={"sketch": "agent-dup"})
        except EligibilityConflictError:
            blocked_agent = True
        assert blocked_agent

        # --- 未成年作者由监护人代签投稿 ---
        minor_receipt = service.submit(
            request_id="submit-minor", actor_id="person:guard", window_id="w1",
            track_id="haowu", applicant_type="person", applicant_id="guard",
            represented_type="person", represented_id="minor",
            guardian_person_id="guard", title="少年作品初版", content={"idea": "m1"})
        minor_id = minor_receipt.resource_id

        # --- 临近截止：补正、换赛道、成员变更全部留下事件 ---
        clock._value = closes - timedelta(hours=2)
        service.correct(request_id="correct-1", actor_id="person:lead",
                        submission_id=submission_id, title="纹样二版",
                        content={"sketch": "v2"})
        service.transfer(request_id="transfer-1", actor_id="person:lead",
                         submission_id=submission_id, target_track_id="haowu")
        service.change_team_members(request_id="members-1", actor_id="person:lead",
                                    team_id="team-1", joins=[{"person_id": "agent", "role": "经纪"}],
                                    leaves=[])

        # 审核员要求补正：进入等待补正状态。
        clock._value = closes - timedelta(minutes=90)
        service.request_correction(
            request_id="rc-1", actor_id="rv1", submission_id=submission_id,
            note="请补充创作说明", correction_due_at=(closes - timedelta(minutes=30))
            .isoformat().replace("+00:00", "Z"))
        waiting = service.explain("rv1", submission_id,
                                 (closes - timedelta(minutes=80)).isoformat().replace("+00:00", "Z"))
        assert waiting["verdict"] == "awaiting_correction"

        # 参赛者在期限内完成补正，状态回到在审。
        clock._value = closes - timedelta(minutes=60)
        service.correct(request_id="correct-2", actor_id="person:lead",
                        submission_id=submission_id, title="纹样三版",
                        content={"sketch": "v3", "note": "创作说明已补"})

        # --- 截止后：常规补正被拒，冻结原子生效 ---
        clock._value = closes + timedelta(minutes=1)
        late_correct_blocked = False
        try:
            service.correct(request_id="late-correct", actor_id="person:lead",
                            submission_id=submission_id, title="迟到版",
                            content={"sketch": "late"})
        except WindowClosedError:
            late_correct_blocked = True
        assert late_correct_blocked

        frozen = service.freeze_window(request_id="freeze", actor_id="a1", window_id="w1")
        assert frozen.resource_id == "w1"
        # 回执响应体需要重新取（WriteReceipt 不含 body），改为直接查询状态。
        detail_after = service.get_submission("rv1", submission_id)
        assert detail_after["submission"]["effective_version"] == 3
        assert detail_after["window"]["frozen"] is True

        # 冻结后任何改写原申请的动作都被拒绝。
        frozen_blocked = False
        try:
            service.withdraw(request_id="withdraw-after-freeze", actor_id="person:lead",
                             submission_id=submission_id, reason="赛后撤回")
        except FrozenWindowError:
            frozen_blocked = True
        assert frozen_blocked

        # --- 迟到材料只能申诉：原冻结版本保持第 3 版 ---
        appeal = service.file_appeal(
            request_id="appeal-1", actor_id="person:lead", submission_id=submission_id,
            reason="截止前网络故障，补正说明延迟到达",
            evidence={"isp_report": "故障单号 955xx"},
            late_title="纹样申诉版", late_content={"sketch": "appeal-v4"})
        appeal_id = appeal.resource_id
        detail_before_decision = service.get_submission("rv1", submission_id)
        assert detail_before_decision["submission"]["effective_version"] == 3
        assert [v["version"] for v in detail_before_decision["versions"]] == [1, 2, 3]
        assert detail_before_decision["appeals"][0]["status"] == "pending"

        # --- 模拟服务重启：新建服务实例，未完成审核仍在 open 队列 ---
        del service
        restarted = EligibilityService(Database(db_path), clock)
        tasks = restarted.review_tasks("rv1")
        open_ids = {t["submission_id"] for t in tasks if t["status"] == "open"}
        assert submission_id in open_ids and minor_id in open_ids

        # 采纳申诉：申诉生效版本独立记录，冻结版本不变。
        restarted.decide_appeal(request_id="appeal-ok", actor_id="rv1", appeal_id=appeal_id,
                                decision="accepted", decision_note="故障证据成立")
        # 对冻结版本作正式评审：接受团队作品，拒绝程序不符的少年作品。
        restarted.decide(request_id="decide-1", actor_id="rv1", submission_id=submission_id,
                         decision="accepted", reason="材料齐全，冻结第 3 版有效")
        restarted.decide(request_id="decide-2", actor_id="rv1", submission_id=minor_id,
                         decision="rejected", reason="授权链条不完整")

        final = restarted.get_submission("au1", submission_id)
        assert final["submission"]["effective_version"] == 3
        assert final["submission"]["appeal_effective_version"] == 4
        assert len(final["versions"]) == 3  # 迟到第 4 版从未写入作品版本表

        # --- 历史时点解释 ---
        before_submit = restarted.explain(
            "rv1", submission_id, (start - timedelta(hours=1)).isoformat().replace("+00:00", "Z"))
        at_review = restarted.explain(
            "rv1", submission_id, (closes - timedelta(hours=1, minutes=30))
            .isoformat().replace("+00:00", "Z"))
        after_all = restarted.explain(
            "rv1", submission_id, (closes + timedelta(days=1)).isoformat().replace("+00:00", "Z"))
        assert before_submit["verdict"] == "not_exists"
        assert at_review["verdict"] == "awaiting_correction"
        assert after_all["verdict"] == "accepted"
        assert after_all["state_at"]["effective_version"] == 3

        # 冲突依据可查；名额占用正常。
        conflicts = restarted.list_conflicts("au1", "w1")
        occupancy = {}
        for item in restarted.list_submissions("au1", window_id="w1")["items"]:
            track = restarted.get_submission("au1", item["submission_id"])["track"]
            occupancy[track["track_id"]] = track["occupied"]

        from .audit import verify_chain
        chain_ok, chain_events = verify_chain(restarted.database.connection)
        result = {
            "status": "ok",
            "audit_valid": chain_ok,
            "audit_events": chain_events,
            "duplicate_blocked": bool(blocked),
            "agent_duplicate_blocked": blocked_agent,
            "late_correction_blocked": late_correct_blocked,
            "post_freeze_write_blocked": frozen_blocked,
            "frozen_effective_version": final["submission"]["effective_version"],
            "appeal_effective_version": final["submission"]["appeal_effective_version"],
            "work_versions": len(final["versions"]),
            "timeline_events": len(final["timeline"]),
            "verdicts": {"before": before_submit["verdict"], "mid": at_review["verdict"],
                         "after": after_all["verdict"]},
            "duplicate_sets": len(conflicts["duplicate_occupancy"]),
            "occupancy": occupancy,
            "open_tasks_after_restart": len(open_ids),
            "minor_rejected": restarted.get_submission("rv1", minor_id)["submission"]["status"]
            == "rejected",
        }
        restarted.database.close()
        database.close()
        return result


def main() -> int:
    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "ok" and result["audit_valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
