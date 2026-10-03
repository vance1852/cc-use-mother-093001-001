import unittest
from datetime import datetime, timezone

from creative_program_foundation.api import route
from creative_program_foundation.clock import FixedClock
from creative_program_foundation.governance import GovernanceService
from creative_program_foundation.service import DomainService
from creative_program_foundation.storage import Database


class GovernanceApiTest(unittest.TestCase):
    def setUp(self):
        self.clock = FixedClock(datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc))
        self.database = Database()
        self.base = DomainService(self.database, self.clock)
        self.gov = GovernanceService(self.database, self.clock)
        self.base.register_organization(request_id="ro", actor_id="bootstrap",
                                        organization_id="o1", name="组委会")
        self.base.register_actor(request_id="ra", actor_id="bootstrap", new_actor_id="ad",
                                 display_name="管理员", role="admin", organization_id="o1")
        self.base.register_actor(request_id="rr", actor_id="ad", new_actor_id="rv",
                                 display_name="审核员", role="reviewer", organization_id="o1")
        self.base.register_actor(request_id="rau", actor_id="ad", new_actor_id="au",
                                 display_name="审计员", role="auditor", organization_id="o1")
        self.base.register_site(request_id="rs", actor_id="ad", site_id="s1", organization_id="o1",
                                name="白塔杯", timezone_name="Asia/Shanghai")
        self.gov.create_track(request_id="rt1", actor_id="ad", track_id="wenyun", site_id="s1",
                              name="西城纹韵", quota=5, mutex_group="g")
        self.gov.create_track(request_id="rt2", actor_id="ad", track_id="haowu", site_id="s1",
                              name="西城好物", quota=5, mutex_group="g")
        self.gov.create_window(request_id="rw", actor_id="ad", window_id="w1", site_id="s1",
                               opens_at="2026-10-01T00:00:00Z",
                               deadline_at="2026-10-10T00:00:00Z")
        self.gov.register_person(request_id="rp", actor_id="ad", person_id="p1",
                                 display_name="作者", is_minor=False, id_number="ID-1")
        token_resp = self.gov.mint_participant_token(request_id="tk", actor_id="ad",
                                                     person_id="p1", label="手机")
        self.token = token_resp["token"]

    def tearDown(self):
        self.database.close()

    def call(self, method, path, body=None, actor=None, token=None):
        headers = {}
        if actor:
            headers["X-Actor-Id"] = actor
        if token:
            headers["X-Participant-Token"] = token
        return route(self.base, method, path, body, headers, self.gov)

    def test_health_still_works(self):
        status, payload = self.call("GET", "/health")
        self.assertEqual(200, status)
        self.assertEqual("ok", payload["status"])

    def test_staff_endpoint_requires_actor(self):
        status, payload = self.call("POST", "/gov/tracks",
                                    {"request_id": "x", "track_id": "t", "site_id": "s1",
                                     "name": "n", "quota": 1, "mutex_group": "g"})
        self.assertEqual(403, status)

    def test_participant_submit_and_idempotent(self):
        body = {"request_id": "s1", "window_id": "w1", "track_id": "wenyun",
                "title": "作品", "material": {"file": "a.png"}}
        status1, p1 = self.call("POST", "/submissions", body, token=self.token)
        self.assertEqual(201, status1)
        status2, p2 = self.call("POST", "/submissions", body, token=self.token)
        self.assertEqual(200, status2)
        self.assertTrue(p2["replayed"])
        self.assertEqual(p1["resource_id"], p2["resource_id"])

    def test_submit_requires_participant_token(self):
        status, payload = self.call("POST", "/submissions",
                                    {"request_id": "s", "window_id": "w1", "track_id": "wenyun",
                                     "title": "t", "material": {"f": 1}})
        self.assertEqual(403, status)

    def test_participant_cannot_access_staff_views(self):
        for path in ("/gov/submissions", "/gov/review-queue", "/gov/conflicts", "/gov/appeals"):
            status, _ = self.call("GET", path, token=self.token)
            self.assertEqual(403, status, path)

    def test_three_roles_see_respective_materials(self):
        # 参赛人提交
        sid = self.call("POST", "/submissions",
                        {"request_id": "s", "window_id": "w1", "track_id": "wenyun",
                         "title": "t", "material": {"secret": "x"}}, token=self.token)[1]["resource_id"]
        # 参赛人只看到自己的作品
        status, mine = self.call("GET", "/me/submissions", token=self.token)
        self.assertEqual(200, status)
        self.assertEqual(1, len(mine["items"]))
        self.assertEqual("x", mine["items"][0]["latest_material"]["secret"])
        # 审核员看到全部作品与材料
        status, reviewer = self.call("GET", "/gov/submissions?window_id=w1", actor="rv")
        self.assertEqual(200, status)
        self.assertEqual(1, len(reviewer["items"]))
        # 审计员可列作品
        status, auditor = self.call("GET", "/gov/submissions", actor="au")
        self.assertEqual(200, status)
        self.assertEqual(1, len(auditor["items"]))

    def test_conflict_visible_to_reviewer_after_duplicate(self):
        self.gov.register_organization(request_id="po", actor_id="ad",
                                       organization_party_id="studio", legal_name="工作室",
                                       org_type="studio", registration_number="R1",
                                       beneficial_person_id="p1")
        self.call("POST", "/submissions",
                  {"request_id": "s1", "window_id": "w1", "track_id": "wenyun",
                   "title": "A", "material": {"f": 1}}, token=self.token)
        status, payload = self.call("POST", "/submissions",
                                    {"request_id": "s2", "window_id": "w1", "track_id": "haowu",
                                     "title": "B", "material": {"f": 2},
                                     "organization_party_id": "studio"}, token=self.token)
        self.assertEqual(409, status)
        self.assertEqual("duplicate_qualification", payload["error"])
        status, conflicts = self.call("GET", "/gov/conflicts", actor="rv")
        self.assertEqual(200, status)
        self.assertEqual(1, len(conflicts["items"]))

    def test_timeline_access_control_and_explain_endpoint(self):
        sid = self.call("POST", "/submissions",
                        {"request_id": "s", "window_id": "w1", "track_id": "wenyun",
                         "title": "t", "material": {"f": 1}}, token=self.token)[1]["resource_id"]
        # 参赛人可读自己的时间线
        status, _ = self.call("GET", f"/submissions/{sid}/timeline", token=self.token)
        self.assertEqual(200, status)
        # 审核员可解释任意时点
        status, payload = self.call("GET", f"/submissions/{sid}/explain?at=2026-10-02T00:00:00Z",
                                    actor="rv")
        self.assertEqual(200, status)
        self.assertEqual("pending", payload["status"])
        # 无身份不可读
        status, _ = self.call("GET", f"/submissions/{sid}/timeline")
        self.assertEqual(403, status)

    def test_review_and_appeal_flow_over_http(self):
        sid = self.call("POST", "/submissions",
                        {"request_id": "s", "window_id": "w1", "track_id": "wenyun",
                         "title": "t", "material": {"f": 1}}, token=self.token)[1]["resource_id"]
        status, _ = self.call("POST", "/gov/reviews",
                              {"request_id": "d", "submission_id": sid,
                               "decision": "correction_requested", "reason": "补"}, actor="rv")
        self.assertEqual(201, status)
        status, payload = self.call("POST", "/submissions/correct",
                                    {"request_id": "c", "submission_id": sid,
                                     "material": {"f": 2}}, token=self.token)
        self.assertEqual(201, status)

    def test_unknown_gov_route_404(self):
        status, payload = self.call("GET", "/gov/nope", actor="rv")
        self.assertEqual(404, status)
        self.assertEqual("route_not_found", payload["error"])


if __name__ == "__main__":
    unittest.main()
