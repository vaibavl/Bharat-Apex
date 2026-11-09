import os
import sys
import tempfile
import types
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# Use a throwaway database and no real API keys for the whole test module.
_TMP = tempfile.mkdtemp()
os.environ["DATABASE_PATH"] = os.path.join(_TMP, "test.db")
for k in ("GROQ_API_KEY", "GEMINI_API_KEY", "OPENAI_API_KEY"):
    os.environ.pop(k, None)

from fastapi.testclient import TestClient  # noqa: E402

from src.api.app import app  # noqa: E402
from src.agent.llm_router import LLMRouter  # noqa: E402
from src.agent.tools import ToolExecutor  # noqa: E402
from src.banking.core_banking_service import core_banking  # noqa: E402


def login(username, password):
    c = TestClient(app)
    r = c.post("/api/auth/login", json={"username": username, "password": password})
    return c, r


class TestAuth(unittest.TestCase):
    def test_login_required(self):
        c = TestClient(app)
        for path in ("/api/auth/me", "/api/overview", "/api/conversations"):
            self.assertEqual(c.get(path).status_code, 401)
        self.assertEqual(c.post("/api/chat", json={"message": "hi"}).status_code, 401)

    def test_bad_password_rejected(self):
        _, r = login("priya", "wrong")
        self.assertEqual(r.status_code, 401)

    def test_login_sets_httponly_cookie_and_scopes_customer(self):
        c, r = login("aarav", "Apex@101")
        self.assertEqual(r.status_code, 200)
        self.assertIn("httponly", r.headers["set-cookie"].lower())
        self.assertIn("apex_session", r.headers["set-cookie"])
        self.assertEqual(c.get("/api/auth/me").json()["customer_id"], "CUST-IN-101")
        self.assertEqual(c.get("/api/overview").json()["profile"]["name"], "Aarav Singhania")

    def test_logout_ends_session(self):
        c, _ = login("rajesh", "Apex@102")
        self.assertEqual(c.get("/api/auth/me").status_code, 200)
        c.post("/api/auth/logout")
        self.assertEqual(c.get("/api/auth/me").status_code, 401)


class TestConversations(unittest.TestCase):
    def test_chat_creates_persists_and_renames(self):
        c, _ = login("priya", "Apex@103")
        r = c.post("/api/chat", json={"message": "Please unblock my RuPay card right now."})
        self.assertEqual(r.status_code, 200)
        conv_id = r.json()["conversation"]["id"]
        self.assertIn("unblock", r.json()["reply"].lower())

        msgs = c.get(f"/api/conversations/{conv_id}/messages").json()
        self.assertEqual([m["role"] for m in msgs], ["user", "assistant"])

        self.assertEqual(c.patch(f"/api/conversations/{conv_id}", json={"title": "Card help"}).status_code, 200)
        titles = [x["title"] for x in c.get("/api/conversations").json()]
        self.assertIn("Card help", titles)

        self.assertEqual(c.delete(f"/api/conversations/{conv_id}").status_code, 200)
        self.assertEqual(c.get(f"/api/conversations/{conv_id}/messages").status_code, 404)

    def test_conversations_isolated_between_customers(self):
        a, _ = login("aarav", "Apex@101")
        conv_id = a.post("/api/chat", json={"message": "What is my balance?"}).json()["conversation"]["id"]
        b, _ = login("rajesh", "Apex@102")
        self.assertEqual(b.get(f"/api/conversations/{conv_id}/messages").status_code, 404)
        self.assertEqual(b.delete(f"/api/conversations/{conv_id}").status_code, 404)
        self.assertNotIn(conv_id, [x["id"] for x in b.get("/api/conversations").json()])
        r = b.post("/api/chat", json={"conversation_id": conv_id, "message": "hello"})
        self.assertEqual(r.status_code, 404)


class FakeGroq:
    """Minimal stand-in for the Groq SDK: first asks for a tool, then answers."""
    calls = 0

    def __init__(self, api_key=None):
        outer = self

        class Completions:
            def create(self, **kw):
                outer.__class__.calls += 1
                msgs = kw["messages"]
                if msgs[-1]["role"] == "tool":
                    content = "Done. " + msgs[-1]["content"][:40]
                    return types.SimpleNamespace(choices=[types.SimpleNamespace(
                        message=types.SimpleNamespace(content=content, tool_calls=None))])
                tc = types.SimpleNamespace(
                    id="call_1",
                    function=types.SimpleNamespace(name="block_card", arguments="{}"))
                return types.SimpleNamespace(choices=[types.SimpleNamespace(
                    message=types.SimpleNamespace(content=None, tool_calls=[tc]))])

        self.chat = types.SimpleNamespace(completions=Completions())


class TestAgenticLoop(unittest.TestCase):
    def test_tool_calling_loop_executes_banking_tool(self):
        fake_module = types.ModuleType("groq")
        fake_module.Groq = FakeGroq
        sys.modules["groq"] = fake_module
        try:
            router = LLMRouter()
            router.groq_api_key = "gsk_test"
            res = router.agentic_chat("sys", [], "block my card", ToolExecutor("CUST-IN-102"))
        finally:
            del sys.modules["groq"]
        self.assertIsNotNone(res)
        self.assertTrue(res["reply"].startswith("Done."))
        self.assertEqual(res["actions"][0]["tool"], "block_card")
        card = core_banking.get_account_summary("CUST-IN-102")["cards"][0]
        self.assertIn("Frozen", card["status"])
        core_banking.unfreeze_card("CUST-IN-102", card["card_id"])

    def test_tools_cannot_touch_other_customers_data(self):
        ex = ToolExecutor("CUST-IN-103")
        res = ex.run("raise_dispute", {"transaction_id": "TXN-IN-90811", "reason": "test"})
        self.assertFalse(res["success"])
        res = ex.run("release_swift_hold", {"transaction_id": "TXN-IN-90811"})
        self.assertFalse(res["success"])


class TestNewEndpoints(unittest.TestCase):
    def test_new_endpoints(self):
        c, _ = login("aarav", "Apex@101")
        
        # 1. Investment analysis
        r_inv = c.get("/api/investments/analysis")
        self.assertEqual(r_inv.status_code, 200)
        self.assertGreater(r_inv.json()["net_worth_inr"], 0)

        # 2. Memory endpoint
        r_mem = c.get("/api/memory")
        self.assertEqual(r_mem.status_code, 200)
        self.assertEqual(r_mem.json()["customer_id"], "CUST-IN-101")

        # 3. Memory recall test
        r_rec = c.post("/api/memory/recall", json={"query": "wire hold"})
        self.assertEqual(r_rec.status_code, 200)
        self.assertGreater(len(r_rec.json()["results"]), 0)

        # 4. Notifications
        r_not = c.get("/api/notifications")
        self.assertEqual(r_not.status_code, 200)
        self.assertGreater(len(r_not.json()["notifications"]), 0)

        # 5. Card toggle
        card_id = core_banking.get_account_summary("CUST-IN-101")["cards"][0]["card_id"]
        r_card = c.post("/api/cards/toggle", json={"card_id": card_id, "field": "status"})
        self.assertEqual(r_card.status_code, 200)

        # 6. Statement export
        r_stmt = c.get("/api/statement/export")
        self.assertEqual(r_stmt.status_code, 200)
        self.assertIn("text/csv", r_stmt.headers["content-type"])
        self.assertIn("BHARATAPEX COMMERCIAL BANK", r_stmt.text)


if __name__ == "__main__":
    unittest.main()
