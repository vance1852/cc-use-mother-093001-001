import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from creative_program_foundation.clock import FixedClock
from creative_program_foundation.errors import (
    ConflictError,
    DuplicateQualificationError,
    FrozenError,
    NotFoundError,
    PermissionDenied,
    ValidationError,
    WindowClosedError,
)
from creative_program_foundation.governance import GovernanceService
from creative_program_foundation.service import DomainService
from creative_program_foundation.storage import Database


class GovernanceTest(unittest.TestCase):
    def setUp(self):
        self.clock = FixedClock(datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc))
        self.database = Database()
        self.base = DomainService(self.database, self.clock)
        self.gov = GovernanceService(self.database, self.clock)
        self._bootstrap()

    def _bootstrap(self):
        b = self.base
        b.register_organization(request_id="ro", actor_id="bootstrap", organization_id="o1", name="组委会")
        b.register_actor(request_id="ra", actor_id="bootstrap", new_actor_id="ad",
                         display_name="管理员", role="admin", organization_id="o1")
        b.register_actor(request_id="rr", actor_id="ad", new_actor_id="rv",
                         display_name="审核员", role="reviewer", organization_id="o1")
        b.register_actor(request_id="rau", actor_id="ad", new_actor_id="au",
                         display_name="审计员", role="auditor", organization_id="o1")
        b.register_site(request_id="rs", actor_id="ad", site_id="s1", organization_id="o1",
                        name="白塔杯", timezone_name="Asia/Shanghai")
        g = self.gov
        g.create_track(request_id="rt1", actor_id="ad", track_id="wenyun", site_id="s1",
                       name="西城纹韵", quota=2, mutex_group="baitabei")
        g.create_track(request_id="rt2", actor_id="ad", track_id="haowu", site_id="s1",
                       name="西城好物", quota=2, mutex_group="baitabei")
        g.create_window(request_id="rw", actor_id="ad", window_id="w1", site_id="s1",
                        opens_at="2026-10-01T00:00:00Z", deadline_at="2026-10-10T00:00:00Z")

    def _person(self, pid, name="作者", minor=False, idnum=None):
        self.gov.register_person(request_id="p-" + pid, actor_id="ad", person_id=pid,
                                 display_name=name, is_minor=minor,
                                 id_number=idnum or ("ID-" + pid))

    def _token(self, pid, label="t"):
        return self.gov.mint_participant_token(request_id="tok-" + pid + label, actor_id="ad",
                                               person_id=pid, label=label)["token"]

    def tearDown(self):
        self.database.close()

    def _adult_with_token(self, pid="p1"):
        self._person(pid)
        return pid, self._token(pid)

    def test_reviewer_cannot_create_track(self):
        with self.assertRaises(PermissionDenied):
            self.gov.create_track(request_id="x", actor_id="rv", track_id="t3", site_id="s1",
                                  name="X", quota=1, mutex_group="g")

    def test_window_prevents_submit_before_open(self):
        self.gov.create_window(request_id="rw2", actor_id="ad", window_id="w2", site_id="s1",
                               opens_at="2026-11-01T00:00:00Z", deadline_at="2026-11-10T00:00:00Z")
        pid, token = self._adult_with_token()
        with self.assertRaises(WindowClosedError):
            self.gov.submit(request_id="s1", caller_person_id=pid, window_id="w2",
                            track_id="wenyun", title="t", material={"f": 1})

    def test_duplicate_submit_request_is_idempotent(self):
        pid, _ = self._adult_with_token()
        first = self.gov.submit(request_id="dup", caller_person_id=pid, window_id="w1",
                                track_id="wenyun", title="t", material={"f": 1})
        second = self.gov.submit(request_id="dup", caller_person_id=pid, window_id="w1",
                                 track_id="wenyun", title="t", material={"f": 1})
        self.assertTrue(second.replayed)
        self.assertEqual(first.resource_id, second.resource_id)
        self.assertEqual(1, len(self.gov.list_submissions("rv")))

    def test_same_beneficiary_cannot_hold_two_mutex_slots(self):
        # 同一自然人以个人身份投纹韵，又以工作室（其本人为受益控制人）投好物
        pid, _ = self._adult_with_token()
        self.gov.register_organization(request_id="po", actor_id="ad", organization_party_id="studio",
                                       legal_name="工作室", org_type="studio",
                                       registration_number="REG-1", beneficial_person_id=pid)
        self.gov.submit(request_id="s1", caller_person_id=pid, window_id="w1",
                        track_id="wenyun", title="A", material={"f": 1})
        with self.assertRaises(DuplicateQualificationError):
            self.gov.submit(request_id="s2", caller_person_id=pid, window_id="w1",
                            track_id="haowu", title="B", material={"f": 2},
                            organization_party_id="studio")
        # 第二份作品不得残留
        self.assertEqual(1, len(self.gov.list_submissions("rv")))
        # 冲突依据必须持久留痕
        conflicts = self.gov.list_conflicts("rv")
        self.assertEqual(1, len(conflicts))
        self.assertEqual(pid, conflicts[0]["beneficiary_person_id"])

    def test_quota_enforced(self):
        self._person("a"); self._person("b"); self._person("c")
        for pid in ("a", "b"):
            self.gov.submit(request_id="q-" + pid, caller_person_id=pid, window_id="w1",
                            track_id="wenyun", title=pid, material={"f": 1})
        with self.assertRaises(ConflictError):
            self.gov.submit(request_id="q-c", caller_person_id="c", window_id="w1",
                            track_id="wenyun", title="c", material={"f": 1})

    def test_withdraw_releases_slot_for_reuse(self):
        self._person("a"); self._person("b")
        r = self.gov.submit(request_id="qa", caller_person_id="a", window_id="w1",
                            track_id="wenyun", title="a", material={"f": 1})
        self.gov.withdraw(request_id="wa", caller_person_id="a", submission_id=r.resource_id)
        # 名额释放后 b 可以占用
        self.gov.submit(request_id="qb", caller_person_id="b", window_id="w1",
                        track_id="wenyun", title="b", material={"f": 1})

    def test_switch_track_is_versioned_and_keeps_single_hold(self):
        pid, _ = self._adult_with_token()
        r = self.gov.submit(request_id="s", caller_person_id=pid, window_id="w1",
                            track_id="wenyun", title="t", material={"f": 1})
        self.clock.set_to(datetime(2026, 10, 2, tzinfo=timezone.utc))
        self.gov.switch_track(request_id="sw", caller_person_id=pid,
                              submission_id=r.resource_id, target_track_id="haowu")
        timeline = self.gov.timeline(submission_id=r.resource_id, actor_id="rv")
        self.assertEqual("haowu", timeline["submission"]["track_id"])
        self.assertEqual(2, len(timeline["versions"]))
        # 只有一个生效占位
        holds = [h for h in self.database.connection.execute(
            "SELECT status FROM qualification_holds WHERE submission_id=?",
            (r.resource_id,)).fetchall()]
        self.assertEqual(1, len([h for h in holds if h["status"] == "held"]))

    def test_correction_and_review_flow_and_explain(self):
        pid, _ = self._adult_with_token()
        r = self.gov.submit(request_id="s", caller_person_id=pid, window_id="w1",
                            track_id="wenyun", title="t", material={"f": 1})
        sid = r.resource_id
        self.clock.set_to(datetime(2026, 10, 2, tzinfo=timezone.utc))
        self.gov.decide_review(request_id="d1", actor_id="rv", submission_id=sid,
                               decision="correction_requested", reason="补材料")
        self.assertEqual("awaiting_correction",
                         self.gov.explain(submission_id=sid, actor_id="rv")["status"])
        self.clock.set_to(datetime(2026, 10, 3, tzinfo=timezone.utc))
        self.gov.correct(request_id="c1", caller_person_id=pid, submission_id=sid,
                         material={"f": 2})
        self.clock.set_to(datetime(2026, 10, 4, tzinfo=timezone.utc))
        self.gov.decide_review(request_id="d2", actor_id="rv", submission_id=sid,
                               decision="approved", reason="齐全")
        now_view = self.gov.explain(submission_id=sid, actor_id="rv")
        self.assertEqual("approved", now_view["status"])
        self.assertEqual(2, now_view["effective_version"])
        # 时间旅行：10-02 12:00 处于待补正
        past = self.gov.explain(submission_id=sid, at="2026-10-02T12:00:00Z", actor_id="rv")
        self.assertEqual("awaiting_correction", past["status"])
        self.assertEqual(1, past["effective_version"])

    def test_concurrent_duplicate_requests_create_one_submission(self):
        import concurrent.futures

        self._person("a")
        def submit():
            return self.gov.submit(request_id="same-req", caller_person_id="a", window_id="w1",
                                   track_id="wenyun", title="t", material={"f": 1})
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = [pool.submit(submit) for _ in range(8)]
            receipts = [f.result() for f in results]
        ids = {r.resource_id for r in receipts}
        self.assertEqual(1, len(ids))
        self.assertEqual(1, len(self.gov.list_submissions("rv")))

    def test_double_review_decision_rejected(self):
        pid, _ = self._adult_with_token()
        r = self.gov.submit(request_id="s", caller_person_id=pid, window_id="w1",
                            track_id="wenyun", title="t", material={"f": 1})
        self.gov.decide_review(request_id="d1", actor_id="rv", submission_id=r.resource_id,
                               decision="approved", reason="ok")
        with self.assertRaises(ConflictError):
            self.gov.decide_review(request_id="d2", actor_id="rv", submission_id=r.resource_id,
                                   decision="rejected", reason="change")

    def test_minor_guardian_submission_and_scope(self):
        self._person("minor", "未成年", minor=True, idnum="MINOR-1")
        self._person("guard", "监护人", minor=False, idnum="GUARD-1")
        self.gov.register_representation(request_id="rel", actor_id="ad", relation_id="rel1",
                                         subject_person_id="minor",
                                         representative_person_id="guard", kind="guardianship",
                                         scope=["submit"], valid_from="2026-09-01T00:00:00Z",
                                         valid_until="2026-12-01T00:00:00Z", evidence_ref="book")
        r = self.gov.submit(request_id="s", caller_person_id="guard", submitter_person_id="minor",
                            relation_id="rel1", window_id="w1", track_id="wenyun",
                            title="童作", material={"f": 1})
        sub = self.database.connection.execute(
            "SELECT beneficiary_person_id FROM submissions WHERE submission_id=?",
            (r.resource_id,)).fetchone()
        self.assertEqual("minor", sub["beneficiary_person_id"])
        # 授权范围不含 correct
        with self.assertRaises(PermissionDenied):
            self.gov.correct(request_id="c", caller_person_id="guard",
                             submission_id=r.resource_id, material={"f": 2})

    def test_guardianship_requires_minor(self):
        self._person("adult1", idnum="A1"); self._person("adult2", idnum="A2")
        with self.assertRaises(ValidationError):
            self.gov.register_representation(request_id="rel", actor_id="ad", relation_id="r",
                                             subject_person_id="adult1",
                                             representative_person_id="adult2",
                                             kind="guardianship", scope=["submit"],
                                             valid_from="2026-09-01T00:00:00Z",
                                             valid_until=None, evidence_ref="x")

    def test_revoked_relation_blocks_agent(self):
        self._person("owner", idnum="O1"); self._person("agent", idnum="G1")
        self.gov.register_representation(request_id="rel", actor_id="ad", relation_id="r1",
                                         subject_person_id="owner",
                                         representative_person_id="agent", kind="authorization",
                                         scope=["submit", "correct"],
                                         valid_from="2026-09-01T00:00:00Z",
                                         valid_until=None, evidence_ref="poa")
        self.gov.revoke_representation(request_id="rev", actor_id="ad", relation_id="r1")
        with self.assertRaises(PermissionDenied):
            self.gov.submit(request_id="s", caller_person_id="agent", submitter_person_id="owner",
                            relation_id="r1", window_id="w1", track_id="wenyun",
                            title="t", material={"f": 1})

    def test_team_member_change_is_appended_and_restricted_after_deadline(self):
        self._person("lead"); self._person("m1"); self._person("m2")
        self.gov.create_team(request_id="team", actor_id="lead", team_id="tm",
                             name="团队", creator_person_id="lead")
        self.gov.change_team_member(request_id="add", caller_person_id="lead", team_id="tm",
                                    person_id="m1", change="joined")
        self.gov.designate_team_beneficiary(request_id="des", actor_id="lead",
                                            team_id="tm", person_id="lead")
        r = self.gov.submit(request_id="s", caller_person_id="lead", window_id="w1",
                            track_id="wenyun", title="team", material={"f": 1}, team_id="tm")
        self.clock.set_to(datetime(2026, 10, 10, 0, 1, tzinfo=timezone.utc))
        with self.assertRaises(WindowClosedError):
            self.gov.change_team_member(request_id="late", caller_person_id="lead", team_id="tm",
                                        person_id="m2", change="joined",
                                        submission_id=r.resource_id)

    def test_beneficiary_cannot_leave_team(self):
        self._person("lead"); self._person("m1")
        self.gov.create_team(request_id="t", actor_id="lead", team_id="tm", name="团",
                             creator_person_id="lead")
        self.gov.designate_team_beneficiary(request_id="d", actor_id="lead",
                                            team_id="tm", person_id="lead")
        with self.assertRaises(ConflictError):
            self.gov.change_team_member(request_id="leave", caller_person_id="lead", team_id="tm",
                                        person_id="lead", change="left")

    def test_team_cannot_swap_beneficiary_while_holding_slot(self):
        self._person("lead"); self._person("m1")
        self.gov.create_team(request_id="t", actor_id="lead", team_id="tm", name="团",
                             creator_person_id="lead")
        self.gov.change_team_member(request_id="add", caller_person_id="lead", team_id="tm",
                                    person_id="m1", change="joined")
        self.gov.designate_team_beneficiary(request_id="d1", actor_id="lead",
                                            team_id="tm", person_id="lead")
        self.gov.submit(request_id="s", caller_person_id="lead", window_id="w1",
                        track_id="wenyun", title="t", material={"f": 1}, team_id="tm")
        # 已持有生效名额，不能把受益人改成另一名成员
        with self.assertRaises(ConflictError):
            self.gov.designate_team_beneficiary(request_id="d2", actor_id="lead",
                                                team_id="tm", person_id="m1")

    def test_team_and_individual_same_beneficiary_conflict(self):
        self._person("lead"); self._person("other")
        self.gov.create_team(request_id="t", actor_id="lead", team_id="tm", name="团",
                             creator_person_id="lead")
        self.gov.designate_team_beneficiary(request_id="d", actor_id="lead",
                                            team_id="tm", person_id="lead")
        self.gov.submit(request_id="s1", caller_person_id="lead", window_id="w1",
                        track_id="wenyun", title="团队作", material={"f": 1}, team_id="tm")
        # 同一受益自然人再以个人身份投好物
        with self.assertRaises(DuplicateQualificationError):
            self.gov.submit(request_id="s2", caller_person_id="lead", window_id="w1",
                            track_id="haowu", title="个人作", material={"f": 2})

    def test_freeze_blocks_writes_and_late_material_goes_to_appeal(self):
        pid, _ = self._adult_with_token()
        r = self.gov.submit(request_id="s", caller_person_id=pid, window_id="w1",
                            track_id="wenyun", title="t", material={"f": 1})
        sid = r.resource_id
        self.clock.set_to(datetime(2026, 10, 10, 0, 0, tzinfo=timezone.utc))
        self.gov.freeze_window(request_id="fz", actor_id="ad", window_id="w1")
        # 冻结后任何补正/撤回/换赛道都被拒
        with self.assertRaises((WindowClosedError, FrozenError)):
            self.gov.correct(request_id="late-c", caller_person_id=pid, submission_id=sid,
                             material={"f": 9})
        # 迟到材料只能申诉
        self.gov.file_appeal(request_id="ap", caller_person_id=pid, submission_id=sid,
                             material={"late": 9}, reason="快递延误")
        # 申诉不产生新版本
        self.assertEqual(1, len(self.gov.timeline(submission_id=sid, actor_id="rv")["versions"]))
        snapshot = self.database.connection.execute(
            "SELECT version_seq FROM freeze_snapshots WHERE submission_id=?", (sid,)).fetchone()
        self.assertEqual(1, snapshot["version_seq"])

    def test_freeze_is_idempotent_replay_after_deadline(self):
        pid, _ = self._adult_with_token()
        self.gov.submit(request_id="s", caller_person_id=pid, window_id="w1",
                        track_id="wenyun", title="t", material={"f": 1})
        self.clock.set_to(datetime(2026, 10, 10, 0, 0, tzinfo=timezone.utc))
        first = self.gov.freeze_window(request_id="fz", actor_id="ad", window_id="w1")
        # 截止后用同一 request_id 重试，回放原回执而非报错
        second = self.gov.freeze_window(request_id="fz", actor_id="ad", window_id="w1")
        self.assertTrue(second.replayed)
        self.assertEqual(first.resource_id, second.resource_id)

    def test_appeal_only_after_deadline(self):
        pid, _ = self._adult_with_token()
        r = self.gov.submit(request_id="s", caller_person_id=pid, window_id="w1",
                            track_id="wenyun", title="t", material={"f": 1})
        with self.assertRaises(ConflictError):
            self.gov.file_appeal(request_id="ap", caller_person_id=pid,
                                 submission_id=r.resource_id, material={"x": 1}, reason="太早了")

    def test_appeal_accepted_requeues_review(self):
        pid, _ = self._adult_with_token()
        r = self.gov.submit(request_id="s", caller_person_id=pid, window_id="w1",
                            track_id="wenyun", title="t", material={"f": 1})
        sid = r.resource_id
        self.gov.decide_review(request_id="d", actor_id="rv", submission_id=sid,
                               decision="rejected", reason="不符")
        self.clock.set_to(datetime(2026, 10, 10, 0, 1, tzinfo=timezone.utc))
        self.gov.freeze_window(request_id="fz", actor_id="ad", window_id="w1")
        ap = self.gov.file_appeal(request_id="ap", caller_person_id=pid, submission_id=sid,
                                  material={"proof": 1}, reason="误判")
        self.gov.decide_appeal(request_id="ad2", actor_id="rv", appeal_id=ap.resource_id,
                               decision="accepted", decision_note="材料有效")
        task = self.database.connection.execute(
            "SELECT state FROM review_tasks WHERE submission_id=?", (sid,)).fetchone()
        self.assertEqual("queued", task["state"])

    def test_participant_cannot_read_others_submission(self):
        p1, _ = self._adult_with_token("p1")
        self._person("p2")
        r = self.gov.submit(request_id="s", caller_person_id="p1", window_id="w1",
                            track_id="wenyun", title="t", material={"f": 1})
        with self.assertRaises(PermissionDenied):
            self.gov.timeline(submission_id=r.resource_id, person_id="p2")

    def test_auditor_read_only(self):
        pid, _ = self._adult_with_token()
        r = self.gov.submit(request_id="s", caller_person_id=pid, window_id="w1",
                            track_id="wenyun", title="t", material={"secret": "x"})
        sid = r.resource_id
        # 审计员可读但只见材料哈希，不见内容
        aud_timeline = self.gov.timeline(submission_id=sid, actor_id="au")
        self.assertFalse(aud_timeline["material_visible"])
        self.assertNotIn("material", aud_timeline["versions"][0])
        self.assertIn("material_hash", aud_timeline["versions"][0])
        # 审核员可见材料全文
        rev_timeline = self.gov.timeline(submission_id=sid, actor_id="rv")
        self.assertTrue(rev_timeline["material_visible"])
        self.assertEqual("x", rev_timeline["versions"][0]["material"]["secret"])
        # 审计员不能审核
        with self.assertRaises(PermissionDenied):
            self.gov.decide_review(request_id="x", actor_id="au", submission_id=sid,
                                   decision="approved")

    def test_review_resumes_after_restart(self):
        db_path = Path(tempfile.mkdtemp()) / "restart.sqlite3"
        database = Database(db_path)
        clock = FixedClock(datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc))
        base = DomainService(database, clock)
        gov = GovernanceService(database, clock)
        base.register_organization(request_id="ro", actor_id="bootstrap",
                                   organization_id="o1", name="组")
        base.register_actor(request_id="ra", actor_id="bootstrap", new_actor_id="ad",
                            display_name="管", role="admin", organization_id="o1")
        base.register_actor(request_id="rr", actor_id="ad", new_actor_id="rv",
                            display_name="审", role="reviewer", organization_id="o1")
        base.register_site(request_id="rs", actor_id="ad", site_id="s1", organization_id="o1",
                           name="杯", timezone_name="Asia/Shanghai")
        gov.create_track(request_id="rt", actor_id="ad", track_id="wenyun", site_id="s1",
                         name="纹韵", quota=5, mutex_group="g")
        gov.create_window(request_id="rw", actor_id="ad", window_id="w1", site_id="s1",
                          opens_at="2026-10-01T00:00:00Z", deadline_at="2026-10-10T00:00:00Z")
        gov.register_person(request_id="rp", actor_id="ad", person_id="p1", display_name="作者",
                            is_minor=False, id_number="ID-1")
        r = gov.submit(request_id="s", caller_person_id="p1", window_id="w1",
                       track_id="wenyun", title="t", material={"f": 1})
        database.close()

        # 重启：用新连接打开同一文件
        database2 = Database(db_path)
        gov2 = GovernanceService(database2, clock)
        queue = gov2.review_queue("rv")
        self.assertEqual(1, len(queue))
        gov2.decide_review(request_id="d", actor_id="rv", submission_id=r.resource_id,
                           decision="approved", reason="ok")
        valid, _ = DomainService(database2, clock).verify_audit()
        self.assertTrue(valid)
        database2.close()


if __name__ == "__main__":
    unittest.main()
