import unittest
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from src.hindsight.client_wrapper import hindsight_service

class TestHindsightMemory(unittest.TestCase):

    def setUp(self):
        self.bank_id = "test_bank_cust_in_999"
        hindsight_service.reset_bank(self.bank_id)

    def test_retain_and_recall(self):
        fact = "Aarav prefers inward USD remittance purpose code P0802 for tech consultancy."
        res = hindsight_service.retain(
            bank_id=self.bank_id,
            content=fact,
            tags=["firc_hold", "swift_inward", "p0802"]
        )
        self.assertEqual(res["status"], "success")

        recall_res = hindsight_service.recall(
            bank_id=self.bank_id,
            query="Tell me about Aarav's remittance purpose code",
            max_results=3
        )
        self.assertGreater(len(recall_res.results), 0)
        self.assertIn("P0802", recall_res.results[0].fact)

    def test_reflect_disposition(self):
        hindsight_service.retain(
            bank_id=self.bank_id,
            content="Customer was frustrated by repeated FIRC documentation inquiries on inward SWIFT transfers.",
            tags=["frustration_history", "firc_hold"]
        )
        hindsight_service.retain(
            bank_id=self.bank_id,
            content="Customer prefers concise executive summaries in English via WhatsApp.",
            tags=["communication_style", "whatsapp"]
        )

        reflect_res = hindsight_service.reflect(
            bank_id=self.bank_id,
            query="How should the agent handle an incoming SWIFT remittance question?"
        )
        self.assertIsNotNone(reflect_res.disposition)
        self.assertGreater(len(reflect_res.directives), 0)

if __name__ == "__main__":
    unittest.main()
