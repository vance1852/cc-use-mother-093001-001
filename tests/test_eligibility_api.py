import unittest

from creative_program_foundation.eligibility_api import route_eligibility
from creative_program_foundation.eligibility_service import EligibilityService
from creative_program_foundation.service import DomainService
from creative_program_foundation.storage import Database
from tests.test_eligibility_service import EligibilityFixture


class EligibilityApiTest(EligibilityFixture):
    """通过 HTTP 语义路由调用治理服务。"""

    def call(self, method, path, body=None, actor="rv1"):
        return route_eligibility(self.service, method, path, body or {},
                                 {"X-Actor-Id": actor})

    def test_health_route_falls_through_to_foundation(self):
        from creative_program_foundation import api as foundation_api
        status, payload = foundation_api.route(self.foundation, "GET", "/health", None)
        self.assertEqual(200, status)
        self.assertEqual("ok", payload["status"])
        self.assertIsNone(route_eligibility(self.service, "GET", "/health", None))

    def test_submit_then_replay_returns_201_then_200(self):
        body = {"request_id": "http-sub-1", "window_id": "w1", "track_id": "wenyun",
                "applicant_type": "person", "applicant_id": "lead",
                "title": "初版", "content": {"v": 1}}
        status, payload = self.call("POST", "/eg/submissions", body, actor="person:lead")
        self.assertEqual(201, status)
        self.assertFalse(payload["replayed"])
        status2, payload2 = self.call("POST", "/eg/submissions", body, actor="person:lead")
        self.assertEqual(200, status2)
        self.assertTrue(payload2["replayed"])
        self.assertEqual(payload["resource_id"], payload2["resource_id"])

    def test_studio_double_occupy_returns_conflict_basis(self):
        body = {"request_id": "http-sub-1", "window_id": "w1", "track_id": "wenyun",
                "applicant_type": "person", "applicant_id": "lead",
                "title": "初版", "content": {"v": 1}}
        self.call("POST", "/eg/submissions", body, actor="person:lead")
        dup = {"request_id": "http-sub-2", "window_id": "w1", "track_id": "wenyun",
               "applicant_type": "org", "applicant_id": "studio",
               "title": "工作室版", "content": {"v": 2}}
        status, payload = self.call("POST", "/eg/submissions", dup, actor="org:studio")
        self.assertEqual(409, status)
        self.assertEqual("eligibility_conflict", payload["error"])
        self.assertEqual(1, len(payload["conflicts"]))

    def test_reviewer_queue_and_participant_forbidden(self):
        status, payload = self.call("GET", "/eg/review-tasks", actor="rv1")
        self.assertEqual(200, status)
        self.assertEqual([], payload["items"])
        status, payload = self.call("GET", "/eg/review-tasks", actor="person:lead")
        self.assertEqual(403, status)

    def test_freeze_appeal_flow_over_http(self):
        body = {"request_id": "http-sub-1", "window_id": "w1", "track_id": "wenyun",
                "applicant_type": "person", "applicant_id": "lead",
                "title": "初版", "content": {"v": 1}}
        _, created = self.call("POST", "/eg/submissions", body, actor="person:lead")
        sub_id = created["resource_id"]
        self.clock._value = self.closes
        status, payload = self.call("POST", "/eg/windows/freeze",
                                    {"request_id": "http-freeze", "window_id": "w1"},
                                    actor="a1")
        self.assertEqual(201, status)
        status, detail = self.call("GET", f"/eg/submissions/{sub_id}", actor="rv1")
        self.assertEqual(200, status)
        self.assertTrue(detail["window"]["frozen"])
        self.assertEqual(1, detail["effective_version"])
        appeal_body = {"request_id": "http-appeal-1", "submission_id": sub_id,
                       "reason": "网络故障",
                       "evidence": {"ticket": "1"},
                       "late_title": "申诉版", "late_content": {"v": 2}}
        status, appeal = self.call("POST", "/eg/appeals", appeal_body,
                                   actor="person:lead")
        self.assertEqual(201, status)
        status, payload = self.call("POST", "/eg/appeal-decision",
                                    {"request_id": "http-appeal-ok",
                                     "appeal_id": appeal["resource_id"],
                                     "decision": "accepted", "decision_note": "成立"},
                                    actor="rv1")
        self.assertEqual(201, status)
        status, detail = self.call("GET", f"/eg/submissions/{sub_id}", actor="au1")
        self.assertEqual(200, status)
        self.assertEqual(1, detail["effective_version"])
        self.assertEqual(2, detail["appeal_effective_version"])

    def test_explain_endpoint_requires_at(self):
        body = {"request_id": "http-sub-1", "window_id": "w1", "track_id": "wenyun",
                "applicant_type": "person", "applicant_id": "lead",
                "title": "初版", "content": {"v": 1}}
        _, created = self.call("POST", "/eg/submissions", body, actor="person:lead")
        status, payload = self.call(
            "GET", f"/eg/submissions/{created['resource_id']}/explain", actor="rv1")
        self.assertEqual(400, status)
        status, payload = self.call(
            "GET", f"/eg/submissions/{created['resource_id']}/explain"
            "?at=2026-09-30T00:00:00Z", actor="rv1")
        self.assertEqual(200, status)
        self.assertEqual("not_exists", payload["verdict"])

    def test_conflicts_endpoint_staff_only(self):
        status, payload = self.call("GET", "/eg/conflicts?window_id=w1", actor="au1")
        self.assertEqual(200, status)
        self.assertEqual("w1", payload["window_id"])
        status, payload = self.call("GET", "/eg/conflicts?window_id=w1",
                                    actor="person:lead")
        self.assertEqual(403, status)

    def test_unknown_eg_route_is_404(self):
        status, payload = self.call("GET", "/eg/nope", actor="rv1")
        self.assertEqual(404, status)
        self.assertEqual("route_not_found", payload["error"])

    def test_missing_actor_is_rejected(self):
        status, payload = route_eligibility(
            self.service, "GET", "/eg/submissions", {})
        self.assertEqual(403, status)


if __name__ == "__main__":
    unittest.main()
