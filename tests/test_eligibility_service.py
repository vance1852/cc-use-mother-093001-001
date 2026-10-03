import unittest
from datetime import datetime, timedelta, timezone

from creative_program_foundation.clock import FixedClock
from creative_program_foundation.eligibility_service import EligibilityService
from creative_program_foundation.errors import (
    ConflictError,
    EligibilityConflictError,
    FrozenWindowError,
    NotFoundError,
    PermissionDenied,
    QuotaExhaustedError,
    ValidationError,
    WindowClosedError,
)
from creative_program_foundation.service import DomainService
from creative_program_foundation.storage import Database


class EligibilityFixture(unittest.TestCase):
    """提供一套完整的组委会、身份、主体、赛道、窗口建档。"""

    def setUp(self):
        self.database = Database()
        self.start = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)
        self.clock = FixedClock(self.start)
        self.foundation = DomainService(self.database, self.clock)
        self.service = EligibilityService(self.database, self.clock)
        self.foundation.register_organization(
            request_id="org", actor_id="bootstrap", organization_id="o1", name="白塔杯组委会")
        self.foundation.register_actor(
            request_id="admin", actor_id="bootstrap", new_actor_id="a1",
            display_name="管理员", role="admin", organization_id="o1")
        self.foundation.register_actor(
            request_id="reviewer", actor_id="a1", new_actor_id="rv1",
            display_name="审核员", role="reviewer", organization_id="o1")
        self.foundation.register_actor(
            request_id="auditor", actor_id="a1", new_actor_id="au1",
            display_name="审计员", role="auditor", organization_id="o1")
        self.service.register_person(
            request_id="p1", actor_id="a1", person_id="lead",
            legal_name="林一", id_doc_hash="id-lead")
        self.service.register_external_org(
            request_id="o1x", actor_id="a1", external_org_id="studio",
            name="纹韵工作室", license_hash="lic")
        self.service.register_subject(
            request_id="s1", actor_id="a1", subject_id="subj1", display_name="一号主体")
        self.service.link_subject_party(
            request_id="l1", actor_id="a1", subject_id="subj1",
            party_type="person", party_id="lead", link_role="self")
        self.service.link_subject_party(
            request_id="l2", actor_id="a1", subject_id="subj1",
            party_type="org", party_id="studio", link_role="studio")
        self.service.register_track(
            request_id="t1", actor_id="a1", track_id="wenyun", name="西城纹韵",
            slots_total=1, exclusive_group="xicheng")
        self.service.register_track(
            request_id="t2", actor_id="a1", track_id="haowu", name="西城好物",
            slots_total=1, exclusive_group="xicheng")
        self.closes = self.start + timedelta(days=7)
        self.service.register_window(
            request_id="w1x", actor_id="a1", window_id="w1", name="窗口一",
            opens_at=self.start.isoformat().replace("+00:00", "Z"),
            closes_at=self.closes.isoformat().replace("+00:00", "Z"),
            track_ids=["wenyun", "haowu"])

    def tearDown(self):
        self.database.close()

    def iso(self, value: datetime) -> str:
        return value.isoformat().replace("+00:00", "Z")

    def submit_lead(self, request_id="sub-1", track="wenyun"):
        return self.service.submit(
            request_id=request_id, actor_id="person:lead", window_id="w1",
            track_id=track, applicant_type="person", applicant_id="lead",
            title="初版", content={"v": 1})


class SubmissionGovernanceTest(EligibilityFixture):
    def test_repeated_request_never_creates_second_submission(self):
        first = self.submit_lead()
        second = self.submit_lead()
        self.assertFalse(first.replayed)
        self.assertTrue(second.replayed)
        self.assertEqual(first.resource_id, second.resource_id)
        items = self.service.list_submissions("rv1")["items"]
        self.assertEqual(1, len(items))

    def test_replay_after_deadline_still_returns_original_receipt(self):
        first = self.submit_lead()
        self.clock._value = self.closes + timedelta(hours=1)
        replayed = self.submit_lead()
        self.assertTrue(replayed.replayed)
        self.assertEqual(first.resource_id, replayed.resource_id)

    def test_studio_identity_cannot_double_occupy_mutex_group(self):
        first = self.submit_lead()
        with self.assertRaises(EligibilityConflictError) as caught:
            self.service.submit(
                request_id="sub-2", actor_id="org:studio", window_id="w1",
                track_id="wenyun", applicant_type="org", applicant_id="studio",
                title="工作室版", content={"v": 2})
        self.assertEqual(first.resource_id, caught.exception.conflicts[0]["submission_id"])
        self.assertEqual(1, len(self.service.list_submissions("rv1")["items"]))

    def test_transfer_tracks_occupancy_and_rejects_when_target_full(self):
        receipt = self.submit_lead(track="wenyun")
        # 第二受益主体占用 haowu 唯一名额。
        self.service.register_person(
            request_id="p2", actor_id="a1", person_id="other",
            legal_name="旁人", id_doc_hash="id-other")
        self.service.register_subject(
            request_id="s2", actor_id="a1", subject_id="subj2", display_name="二号主体")
        self.service.link_subject_party(
            request_id="l3", actor_id="a1", subject_id="subj2",
            party_type="person", party_id="other", link_role="self")
        self.service.submit(
            request_id="sub-other", actor_id="person:other", window_id="w1",
            track_id="haowu", applicant_type="person", applicant_id="other",
            title="他作", content={"v": 9})
        with self.assertRaises(QuotaExhaustedError):
            self.service.transfer(
                request_id="move-1", actor_id="person:lead",
                submission_id=receipt.resource_id, target_track_id="haowu")
        # 另一主体撤出海物后，原赛道名额释放，可以换入。
        other_id = self.service.list_submissions("person:other")["items"][0]["submission_id"]
        self.service.withdraw(
            request_id="wd-1", actor_id="person:other", submission_id=other_id)
        moved = self.service.transfer(
            request_id="move-2", actor_id="person:lead",
            submission_id=receipt.resource_id, target_track_id="haowu")
        self.assertFalse(moved.replayed)

    def test_withdrawn_submission_releases_mutex_slot(self):
        receipt = self.submit_lead()
        self.service.withdraw(
            request_id="wd-1", actor_id="person:lead",
            submission_id=receipt.resource_id, reason="放弃")
        # 同主体撤回后可重新投递。
        again = self.submit_lead(request_id="sub-again", track="wenyun")
        self.assertFalse(again.replayed)
        self.assertNotEqual(receipt.resource_id, again.resource_id)

    def test_correction_appends_version_and_event(self):
        receipt = self.submit_lead()
        corrected = self.service.correct(
            request_id="corr-1", actor_id="person:lead",
            submission_id=receipt.resource_id, title="二版", content={"v": 2})
        self.assertFalse(corrected.replayed)
        self.assertEqual(receipt.resource_id, corrected.resource_id)
        detail = self.service.get_submission("rv1", receipt.resource_id)
        self.assertEqual([1, 2], [v["version"] for v in detail["versions"]])
        kinds = [e["event_type"] for e in detail["timeline"]]
        self.assertEqual(["submitted", "correction_submitted"], kinds)


class MinorAndAgentTest(EligibilityFixture):
    def setUp(self):
        super().setUp()
        self.service.register_person(
            request_id="pm", actor_id="a1", person_id="minor",
            legal_name="武小", id_doc_hash="id-minor", birth_date="2012-05-01")
        self.service.register_person(
            request_id="pg", actor_id="a1", person_id="guard",
            legal_name="武父", id_doc_hash="id-guard")
        self.service.register_person(
            request_id="pa", actor_id="a1", person_id="agent",
            legal_name="周仲", id_doc_hash="id-agent")
        self.service.add_relationship(
            request_id="r-g", actor_id="a1", relationship_id="guard-rel",
            kind="guardian", principal_type="person", principal_id="minor",
            agent_type="person", agent_id="guard", document_hash="g-cert",
            valid_from="2026-09-01T00:00:00Z")
        self.service.add_relationship(
            request_id="r-a", actor_id="a1", relationship_id="agent-rel",
            kind="authorized_agent", principal_type="person", principal_id="lead",
            agent_type="person", agent_id="agent", document_hash="poa",
            valid_from="2026-09-01T00:00:00Z")
        self.service.register_subject(
            request_id="sm", actor_id="a1", subject_id="subj-minor", display_name="少年主体")
        self.service.link_subject_party(
            request_id="lm", actor_id="a1", subject_id="subj-minor",
            party_type="person", party_id="minor", link_role="self")

    def test_minor_requires_named_guardian_and_relationship(self):
        with self.assertRaises(ValidationError):
            self.service.submit(
                request_id="m-1", actor_id="person:minor", window_id="w1",
                track_id="haowu", applicant_type="person", applicant_id="minor",
                title="少年作", content={"v": 1})
        with self.assertRaises(PermissionDenied):
            self.service.submit(
                request_id="m-2", actor_id="person:guard", window_id="w1",
                track_id="haowu", applicant_type="person", applicant_id="guard",
                represented_type="person", represented_id="minor",
                guardian_person_id="agent", title="少年作", content={"v": 1})
        receipt = self.service.submit(
            request_id="m-3", actor_id="person:guard", window_id="w1",
            track_id="haowu", applicant_type="person", applicant_id="guard",
            represented_type="person", represented_id="minor",
            guardian_person_id="guard", title="少年作", content={"v": 1})
        detail = self.service.get_submission("rv1", receipt.resource_id)
        roles = {p["party_role"] for p in detail["parties"]}
        self.assertIn("guardian", roles)

    def test_authorized_agent_can_submit_for_principal(self):
        receipt = self.submit_lead()
        self.service.withdraw(
            request_id="wd", actor_id="person:lead",
            submission_id=receipt.resource_id)
        agent_receipt = self.service.submit(
            request_id="a-sub", actor_id="person:agent", window_id="w1",
            track_id="wenyun", applicant_type="person", applicant_id="agent",
            represented_type="person", represented_id="lead",
            title="代理投递", content={"v": 2})
        self.assertFalse(agent_receipt.replayed)

    def test_expired_agency_relationship_is_rejected(self):
        self.service.register_person(
            request_id="pr", actor_id="a1", person_id="remote",
            legal_name="远方委托人", id_doc_hash="id-remote")
        self.service.add_relationship(
            request_id="r-old", actor_id="a1", relationship_id="old-agent",
            kind="authorized_agent", principal_type="person", principal_id="remote",
            agent_type="person", agent_id="agent", document_hash="old-poa",
            valid_from="2026-01-01T00:00:00Z", valid_to="2026-05-01T00:00:00Z")
        self.service.register_subject(
            request_id="sag", actor_id="a1", subject_id="subj-remote",
            display_name="远方主体")
        self.service.link_subject_party(
            request_id="lag1", actor_id="a1", subject_id="subj-remote",
            party_type="person", party_id="remote", link_role="self")
        self.service.link_subject_party(
            request_id="lag2", actor_id="a1", subject_id="subj-remote",
            party_type="person", party_id="agent", link_role="member")
        with self.assertRaises(PermissionDenied):
            self.service.submit(
                request_id="exp-1", actor_id="person:agent", window_id="w1",
                track_id="haowu", applicant_type="person", applicant_id="agent",
                represented_type="person", represented_id="remote",
                title="过期代理", content={"v": 3})


class FreezeAndAppealTest(EligibilityFixture):
    def test_late_correction_rejected_then_appeal_keeps_frozen_version(self):
        receipt = self.submit_lead()
        self.service.correct(
            request_id="corr-1", actor_id="person:lead",
            submission_id=receipt.resource_id, title="二版", content={"v": 2})
        self.clock._value = self.closes + timedelta(minutes=1)
        with self.assertRaises(WindowClosedError):
            self.service.correct(
                request_id="late", actor_id="person:lead",
                submission_id=receipt.resource_id, title="迟到版", content={"v": 3})
        self.service.freeze_window(
            request_id="freeze", actor_id="a1", window_id="w1")
        with self.assertRaises(FrozenWindowError):
            self.service.withdraw(
                request_id="w-after", actor_id="person:lead",
                submission_id=receipt.resource_id)
        appeal = self.service.file_appeal(
            request_id="appeal-1", actor_id="person:lead",
            submission_id=receipt.resource_id, reason="网络故障",
            evidence={"ticket": "955"}, late_title="申诉版",
            late_content={"v": 3})
        self.service.decide_appeal(
            request_id="ad", actor_id="rv1", appeal_id=appeal.resource_id,
            decision="accepted", decision_note="证据成立")
        detail = self.service.get_submission("au1", receipt.resource_id)
        self.assertEqual(2, detail["submission"]["effective_version"])
        self.assertEqual(3, detail["submission"]["appeal_effective_version"])
        self.assertEqual([1, 2], [v["version"] for v in detail["versions"]])
        self.assertTrue(all("late" in v and not v["late"] for v in detail["versions"]))

    def test_freeze_is_atomic_and_idempotent(self):
        self.submit_lead()
        self.clock._value = self.closes + timedelta(minutes=1)
        first = self.service.freeze_window(
            request_id="freeze", actor_id="a1", window_id="w1")
        second = self.service.freeze_window(
            request_id="freeze", actor_id="a1", window_id="w1")
        self.assertFalse(first.replayed)
        self.assertTrue(second.replayed)
        with self.assertRaises(FrozenWindowError):
            self.service.freeze_window(
                request_id="freeze-2", actor_id="a1", window_id="w1")

    def test_appeal_before_close_is_rejected(self):
        receipt = self.submit_lead()
        with self.assertRaises(ConflictError):
            self.service.file_appeal(
                request_id="early", actor_id="person:lead",
                submission_id=receipt.resource_id, reason="还没截止")

    def test_review_can_continue_after_freeze_after_restart(self):
        receipt = self.submit_lead()
        self.clock._value = self.closes + timedelta(minutes=1)
        self.service.freeze_window(
            request_id="freeze", actor_id="a1", window_id="w1")
        restarted = EligibilityService(self.database, self.clock)
        tasks = restarted.review_tasks("rv1", status="open")
        self.assertEqual(receipt.resource_id, tasks[0]["submission_id"])
        restarted.decide(
            request_id="decide", actor_id="rv1",
            submission_id=receipt.resource_id, decision="accepted", reason="合格")
        self.assertEqual("accepted",
                         restarted.get_submission("rv1", receipt.resource_id)
                         ["submission"]["status"])

    def test_correction_due_window_expiry(self):
        receipt = self.submit_lead()
        self.service.request_correction(
            request_id="rc", actor_id="rv1", submission_id=receipt.resource_id,
            note="补充说明", correction_due_at=self.iso(self.closes - timedelta(days=1)))
        self.clock._value = self.closes - timedelta(hours=12)
        with self.assertRaises(WindowClosedError):
            self.service.correct(
                request_id="too-late", actor_id="person:lead",
                submission_id=receipt.resource_id, title="逾期补正", content={"v": 8})


class TeamAndHistoryTest(EligibilityFixture):
    def test_member_change_is_recorded_and_reflected_on_open_submission(self):
        self.service.register_person(
            request_id="p3", actor_id="a1", person_id="lead2",
            legal_name="陈二", id_doc_hash="id-lead2")
        self.service.register_team(
            request_id="team", actor_id="a1", team_id="team-1",
            subject_id="subj1", name="一号团队",
            members=[{"person_id": "lead", "role": "主创"},
                     {"person_id": "lead2", "role": "协创"}])
        receipt = self.service.submit(
            request_id="team-sub", actor_id="person:lead", window_id="w1",
            track_id="wenyun", applicant_type="person", applicant_id="lead",
            team_id="team-1", title="团队作", content={"v": 1})
        self.service.register_person(
            request_id="p4", actor_id="a1", person_id="newbie",
            legal_name="新人", id_doc_hash="id-newbie")
        self.service.change_team_members(
            request_id="chg", actor_id="person:lead", team_id="team-1",
            joins=[{"person_id": "newbie", "role": "实习"}], leaves=["lead2"])
        detail = self.service.get_submission("rv1", receipt.resource_id)
        member_events = [e for e in detail["timeline"] if e["event_type"] == "members_changed"]
        self.assertEqual(1, len(member_events))
        snapshot = member_events[0]["detail"]["snapshot"]
        self.assertEqual({"lead", "newbie"}, {m["person_id"] for m in snapshot})
        history = self.database.connection.execute(
            "SELECT change_type FROM eg_team_member_history WHERE person_id='lead2'").fetchall()
        self.assertEqual([("joined",), ("left",)], [(r[0],) for r in history])

    def test_member_change_after_close_is_blocked(self):
        self.service.register_team(
            request_id="team", actor_id="a1", team_id="team-9",
            subject_id="subj1", name="团队九",
            members=[{"person_id": "lead", "role": "主创"}])
        self.service.submit(
            request_id="ts", actor_id="person:lead", window_id="w1",
            track_id="wenyun", applicant_type="person", applicant_id="lead",
            team_id="team-9", title="团队作", content={"v": 1})
        self.clock._value = self.closes + timedelta(minutes=1)
        with self.assertRaises(WindowClosedError):
            self.service.change_team_members(
                request_id="chg-late", actor_id="person:lead", team_id="team-9",
                joins=[], leaves=["lead"])


class ConflictDetectionAndExplainTest(EligibilityFixture):
    def test_merge_subjects_surfaces_duplicate_occupancy(self):
        first = self.submit_lead()
        self.service.register_person(
            request_id="px", actor_id="a1", person_id="sneaky",
            legal_name="分身", id_doc_hash="id-sneaky")
        self.service.register_subject(
            request_id="sx", actor_id="a1", subject_id="subj-x", display_name="分身主体")
        self.service.link_subject_party(
            request_id="lx", actor_id="a1", subject_id="subj-x",
            party_type="person", party_id="sneaky", link_role="self")
        second = self.service.submit(
            request_id="dup-separate", actor_id="a1", window_id="w1",
            track_id="haowu", applicant_type="person", applicant_id="sneaky",
            title="分身作", content={"v": 2})
        # 组委会受理岗事后查实两个主体实为同一团队，并案。
        self.service.merge_subjects(
            request_id="merge", actor_id="a1", source_subject_id="subj-x",
            target_subject_id="subj1")
        conflicts = self.service.list_conflicts("rv1", "w1")
        groups = {(c["root_subject_id"]) for c in conflicts["duplicate_occupancy"]}
        self.assertIn("subj1", groups)
        found = next(c for c in conflicts["duplicate_occupancy"])
        ids = {s["submission_id"] for s in found["submissions"]}
        self.assertEqual({first.resource_id, second.resource_id}, ids)

    def test_explain_replays_status_at_historical_points(self):
        receipt = self.submit_lead()
        sub_id = receipt.resource_id
        self.service.request_correction(
            request_id="rc", actor_id="rv1", submission_id=sub_id,
            note="补材料",
            correction_due_at=self.iso(self.closes - timedelta(days=2)))
        waiting = self.service.explain(
            "rv1", sub_id, self.iso(self.start + timedelta(days=1)))
        self.assertEqual("awaiting_correction", waiting["verdict"])
        self.clock._value = self.closes - timedelta(days=3)
        self.service.correct(
            request_id="corr", actor_id="person:lead", submission_id=sub_id,
            title="补正版", content={"v": 2})
        self.clock._value = self.closes + timedelta(minutes=1)
        self.service.freeze_window(request_id="fz", actor_id="a1", window_id="w1")
        self.service.decide(
            request_id="ok", actor_id="rv1", submission_id=sub_id,
            decision="accepted", reason="通过")
        after = self.service.explain(
            "rv1", sub_id, self.iso(self.closes + timedelta(days=2)))
        self.assertEqual("accepted", after["verdict"])
        self.assertEqual(2, after["state_at"]["effective_version"])
        before = self.service.explain(
            "rv1", sub_id, self.iso(self.start - timedelta(days=1)))
        self.assertEqual("not_exists", before["verdict"])

    def test_explain_mutex_basis_shows_live_sibling_only(self):
        first = self.submit_lead()
        # 同主体第二件投到另一赛道会被拒，因此通过工作人员直接构造并案场景：
        # 先由不同主体投递，再并案；兄弟件在第一时点存活，撤回后消失。
        self.service.register_person(
            request_id="py", actor_id="a1", person_id="pal",
            legal_name="伙伴", id_doc_hash="id-pal")
        self.service.register_subject(
            request_id="sy", actor_id="a1", subject_id="subj-y", display_name="伙伴主体")
        self.service.link_subject_party(
            request_id="ly", actor_id="a1", subject_id="subj-y",
            party_type="person", party_id="pal", link_role="self")
        sibling = self.service.submit(
            request_id="sib", actor_id="a1", window_id="w1",
            track_id="haowu", applicant_type="person", applicant_id="pal",
            title="伙伴作", content={"v": 7})
        self.service.merge_subjects(
            request_id="merge-y", actor_id="a1", source_subject_id="subj-y",
            target_subject_id="subj1")
        at = self.start + timedelta(days=2)
        basis = self.service.explain("rv1", first.resource_id, self.iso(at))
        self.assertEqual(1, len(basis["mutex_basis_at_point"]))
        self.service.withdraw(
            request_id="wd-sib", actor_id="a1", submission_id=sibling.resource_id)
        basis_after = self.service.explain(
            "rv1", first.resource_id, self.iso(at + timedelta(days=2)))
        self.assertEqual(0, len(basis_after["mutex_basis_at_point"]))


class VisibilityTest(EligibilityFixture):
    def test_participant_sees_only_related_materials(self):
        mine = self.submit_lead()
        self.service.register_person(
            request_id="p9", actor_id="a1", person_id="outsider",
            legal_name="外人", id_doc_hash="id-outsider")
        visible = self.service.list_submissions("person:lead")["items"]
        self.assertEqual([mine.resource_id], [s["submission_id"] for s in visible])
        self.assertEqual([], self.service.list_submissions("person:outsider")["items"])
        with self.assertRaises(PermissionDenied):
            self.service.get_submission("person:outsider", mine.resource_id)

    def test_auditor_reads_everything_but_cannot_write(self):
        receipt = self.submit_lead()
        detail = self.service.get_submission("au1", receipt.resource_id)
        self.assertIn("timeline", detail)
        with self.assertRaises(PermissionDenied):
            self.service.decide(
                request_id="dec-x", actor_id="au1", submission_id=receipt.resource_id,
                decision="accepted", reason="审计员无权评审")
        with self.assertRaises(PermissionDenied):
            self.service.register_track(
                request_id="trk-x", actor_id="au1", track_id="zz",
                name="越权赛道", slots_total=1)

    def test_participant_cannot_open_review_queue(self):
        with self.assertRaises(PermissionDenied):
            self.service.review_tasks("person:lead")

    def test_unknown_token_is_rejected(self):
        with self.assertRaises(PermissionDenied):
            self.service.list_submissions("ghost-token")


if __name__ == "__main__":
    unittest.main()
