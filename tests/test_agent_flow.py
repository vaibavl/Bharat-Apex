import unittest
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from src.agent.orchestrator import support_agent
from src.agent.comparison import MemoryBenchmarkComparator

class TestAgentFlow(unittest.TestCase):

    def test_aarav_firc_wire_hold_resolution(self):
        res = support_agent.process_message(
            customer_id="CUST-IN-101",
            message="Why is my $5,000 SWIFT wire from Silicon Valley Bank on hold?",
            use_memory=True
        )
        self.assertIn("Aarav", res["reply"])
        self.assertGreater(len(res["recalled_memories"]), 0)
        self.assertTrue(any("firc" in m["fact"].lower() or "swift" in m["fact"].lower() for m in res["recalled_memories"]))

    def test_rajesh_settlement_resolution(self):
        res = support_agent.process_message(
            customer_id="CUST-IN-102",
            message="Where is my weekend batch settlement of 385000?",
            use_memory=True
        )
        self.assertIn("Rajesh", res["reply"])
        self.assertIn("Begum Bazaar", res["reply"])
        self.assertIn("06:00 IST", res["reply"])

    def test_unfreeze_card_resolution(self):
        res = support_agent.process_message(
            customer_id="CUST-IN-103",
            message="Please unblock my RuPay card now",
            use_memory=True
        )
        self.assertIn("unblocked", res["reply"].lower())

    def test_dispute_charge_resolution(self):
        res = support_agent.process_message(
            customer_id="CUST-IN-103",
            message="I did not do the Paris transaction, refund me",
            use_memory=True
        )
        self.assertIn("dispute", res["reply"].lower())
        self.assertNotIn("registered your upcoming travel notice for paris", res["reply"].lower())

    def test_benchmark_comparison(self):
        comp = MemoryBenchmarkComparator.compare(
            customer_id="CUST-IN-101",
            message="Why is my $5,000 SWIFT wire on hold?"
        )
        self.assertIn("stateless", comp)
        self.assertIn("hindsight", comp)
        self.assertEqual(comp["stateless"]["recalled_memories_count"], 0)
        self.assertGreater(len(comp["hindsight"]["recalled_memories"]), 0)

if __name__ == "__main__":
    unittest.main()
