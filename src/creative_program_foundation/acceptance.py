"""运行基础服务与白塔杯投稿治理的离线端到端验收。"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .clock import FixedClock
from .governance import GovernanceService
from .service import DomainService
from .storage import Database


def _foundation_acceptance(database, clock) -> dict[str, object]:
    service = DomainService(database, clock)
    service.register_organization(request_id="req-org", actor_id="bootstrap",
                                  organization_id="org-001", name="示范项目机构")
    service.register_actor(request_id="req-admin", actor_id="bootstrap", new_actor_id="admin-001",
                           display_name="系统管理员", role="admin", organization_id="org-001")
    service.register_actor(request_id="req-operator", actor_id="admin-001", new_actor_id="operator-001",
                           display_name="项目负责人", role="operator", organization_id="org-001")
    service.register_site(request_id="req-site", actor_id="operator-001", site_id="site-001",
                          organization_id="org-001", name="一号项目节点", timezone_name="Asia/Shanghai")
    first = service.record_domain_data(request_id="req-data", actor_id="operator-001", site_id="site-001",
                                       category="program_profile", external_key="record-001",
                                       data={"name": "基础资料", "enabled": True})
    replay = service.record_domain_data(request_id="req-data", actor_id="operator-001", site_id="site-001",
                                        category="program_profile", external_key="record-001",
                                        data={"name": "基础资料", "enabled": True})
    records = service.list_domain_data("site-001")
    return {"records": len(records), "first_replayed": first.replayed,
            "second_replayed": replay.replayed}


def _governance_acceptance(database, clock) -> dict[str, object]:
    base = DomainService(database, clock)
    gov = GovernanceService(database, clock)
    clock.set_to(datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc))

    base.register_organization(request_id="g-org", actor_id="admin-001",
                               organization_id="org-btb", name="白塔杯组委会")
    base.register_actor(request_id="g-admin", actor_id="admin-001", new_actor_id="g-ad",
                        display_name="管理员", role="admin", organization_id="org-btb")
    base.register_actor(request_id="g-rev", actor_id="g-ad", new_actor_id="g-rv",
                        display_name="资格审核员", role="reviewer", organization_id="org-btb")
    base.register_actor(request_id="g-aud", actor_id="g-ad", new_actor_id="g-au",
                        display_name="审计员", role="auditor", organization_id="org-btb")
    base.register_site(request_id="g-site", actor_id="g-ad", site_id="site-btb",
                       organization_id="org-btb", name="白塔杯", timezone_name="Asia/Shanghai")

    gov.create_track(request_id="g-t1", actor_id="g-ad", track_id="xicheng-wenyun",
                     site_id="site-btb", name="西城纹韵", quota=50, mutex_group="baitabei")
    gov.create_track(request_id="g-t2", actor_id="g-ad", track_id="xicheng-haowu",
                     site_id="site-btb", name="西城好物", quota=50, mutex_group="baitabei")
    gov.create_window(request_id="g-win", actor_id="g-ad", window_id="win-2026",
                      site_id="site-btb", opens_at="2026-10-01T00:00:00Z",
                      deadline_at="2026-10-20T00:00:00Z")

    # 未成年作者与监护人
    gov.register_person(request_id="g-minor", actor_id="g-ad", person_id="minor-li",
                        display_name="李某", is_minor=True, id_number="11010120140101001X")
    gov.register_person(request_id="g-guard", actor_id="g-ad", person_id="guard-li",
                        display_name="李某监护人", is_minor=False, id_number="11010119800101002X")
    gov.register_representation(request_id="g-rel", actor_id="g-ad", relation_id="guard-rel",
                                subject_person_id="minor-li", representative_person_id="guard-li",
                                kind="guardianship",
                                scope=["submit", "correct", "withdraw", "switch_track", "appeal"],
                                valid_from="2026-09-01T00:00:00Z",
                                valid_until="2026-12-31T00:00:00Z", evidence_ref="guardianship.pdf")
    # 同一未成年作者名下的工作室
    gov.register_organization(request_id="g-studio", actor_id="g-ad",
                              organization_party_id="li-studio", legal_name="李某工作室",
                              org_type="studio", registration_number="STUDIO-LI",
                              beneficial_person_id="minor-li")
    token = gov.mint_participant_token(request_id="g-token", actor_id="g-ad",
                                       person_id="guard-li", label="监护人令牌")["token"]

    # 监护人代未成年人投纹韵
    sub = gov.submit(request_id="g-submit", caller_person_id="guard-li",
                     submitter_person_id="minor-li", relation_id="guard-rel",
                     window_id="win-2026", track_id="xicheng-wenyun", title="纹韵初作",
                     material={"work": "draft.zip"})
    sid = sub.resource_id

    # 工作室关联身份再投好物 -> 互斥拒绝并留痕（监护人仍持有效监护关系）
    duplicate_blocked = False
    duplicate_error = ""
    try:
        gov.submit(request_id="g-submit-dup", caller_person_id="guard-li",
                   submitter_person_id="minor-li", organization_party_id="li-studio",
                   relation_id="guard-rel",
                   window_id="win-2026", track_id="xicheng-haowu", title="好物再投",
                   material={"work": "other.zip"})
    except Exception as exc:
        duplicate_blocked = True
        duplicate_error = type(exc).__name__
    conflicts = gov.list_conflicts("g-rv", "win-2026")

    # 审核要求补正 -> 监护人补正
    clock.set_to(datetime(2026, 10, 5, tzinfo=timezone.utc))
    gov.decide_review(request_id="g-rev1", actor_id="g-rv", submission_id=sid,
                      decision="correction_requested", reason="需补授权说明")
    clock.set_to(datetime(2026, 10, 6, tzinfo=timezone.utc))
    gov.correct(request_id="g-corr", caller_person_id="guard-li", submission_id=sid,
                material={"work": "draft-v2.zip", "authorization": "included"})

    # 正式换赛道到好物
    clock.set_to(datetime(2026, 10, 7, tzinfo=timezone.utc))
    gov.switch_track(request_id="g-switch", caller_person_id="guard-li", submission_id=sid,
                     target_track_id="xicheng-haowu", note="临近截止改投西城好物")

    # 审核通过
    clock.set_to(datetime(2026, 10, 8, tzinfo=timezone.utc))
    gov.decide_review(request_id="g-rev2", actor_id="g-rv", submission_id=sid,
                      decision="approved", reason="材料齐全")

    # 历史时点解释：10-05 12:00 应处于待补正
    past = gov.explain(submission_id=sid, at="2026-10-05T12:00:00Z", actor_id="g-rv")

    # 截止原子冻结
    clock.set_to(datetime(2026, 10, 20, 0, 0, tzinfo=timezone.utc))
    frozen = gov.freeze_window(request_id="g-freeze", actor_id="g-ad", window_id="win-2026")

    # 迟到材料只能进申诉
    appeal = gov.file_appeal(request_id="g-appeal", caller_person_id="guard-li",
                             submission_id=sid, material={"supplement": "late.zip"},
                             reason="物流延误，补充说明")

    final = gov.explain(submission_id=sid, actor_id="g-au")
    return {
        "duplicate_blocked": duplicate_blocked,
        "conflicts": len(conflicts),
        "versions": len(gov.timeline(submission_id=sid, actor_id="g-au")["versions"]),
        "past_status": past["status"],
        "frozen_submissions": frozen.resource_id and 1,
        "appeal_id": appeal.resource_id,
        "final_status": final["status"],
        "final_track": final["track_id"],
        "frozen_version": final["frozen_version"],
        "appeal_preserved_original": final["effective_version"] == final["frozen_version"],
    }


def run() -> dict[str, object]:
    """执行基础登记链与白塔杯治理全流程并返回结果。"""

    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "acceptance.sqlite3")
        clock = FixedClock(datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc))
        foundation = _foundation_acceptance(database, clock)
        governance = _governance_acceptance(database, clock)
        valid, event_count = DomainService(database, clock).verify_audit()
        database.close()
        result = {"status": "ok", "audit_events": event_count, "audit_valid": valid,
                  **foundation, **governance}
        return result


def main() -> int:
    """打印验收结果并设置退出码。"""

    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    ok = (result["status"] == "ok" and result["audit_valid"]
          and result["duplicate_blocked"] and result["conflicts"] >= 1
          and result["past_status"] == "awaiting_correction"
          and result["final_status"] == "approved"
          and result["final_track"] == "xicheng-haowu"
          and result["appeal_preserved_original"])
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
