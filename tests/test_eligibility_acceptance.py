import unittest

from creative_program_foundation.eligibility_acceptance import run


class EligibilityAcceptanceTest(unittest.TestCase):
    def test_offline_acceptance(self):
        result = run()
        self.assertEqual("ok", result["status"])
        self.assertTrue(result["audit_valid"])
        self.assertTrue(result["duplicate_blocked"])
        self.assertTrue(result["agent_duplicate_blocked"])
        self.assertTrue(result["late_correction_blocked"])
        self.assertTrue(result["post_freeze_write_blocked"])
        self.assertEqual(3, result["frozen_effective_version"])
        self.assertEqual(4, result["appeal_effective_version"])
        self.assertEqual(3, result["work_versions"])
        self.assertEqual("accepted", result["verdicts"]["after"])
        self.assertEqual("awaiting_correction", result["verdicts"]["mid"])
        self.assertEqual("not_exists", result["verdicts"]["before"])
        self.assertGreaterEqual(result["open_tasks_after_restart"], 2)
        self.assertEqual({"haowu": 2}, result["occupancy"])
        self.assertTrue(result["minor_rejected"])


if __name__ == "__main__":
    unittest.main()
