import unittest
from fastapi.testclient import TestClient

from src.api.app import app
from src.band.band_service import band_service


class TestBandAIIntegration(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_band_service_status(self):
        status = band_service.get_status()
        self.assertIsInstance(status, dict)
        self.assertIn("configured", status)
        self.assertIn("connected", status)
        self.assertIn("agent_id", status)
        self.assertIn("agent_name", status)
        self.assertEqual(status["agent_id"], "52bcf980-8e98-4556-b55c-851f00e5310e")
        self.assertEqual(status["agent_name"], "Apex Fintech Agent")
        self.assertTrue(status["connected"])

    def test_api_band_status(self):
        res = self.client.get("/api/band/status")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data.get("connected"))
        self.assertEqual(data.get("agent_id"), "52bcf980-8e98-4556-b55c-851f00e5310e")
        self.assertIn("darshannn2007", data.get("handle", ""))

    def test_api_band_chats(self):
        res = self.client.get("/api/band/chats")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIn("chats", data)
        self.assertIsInstance(data["chats"], list)

    def test_api_band_poll(self):
        res = self.client.post("/api/band/poll")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data.get("status"), "polled")
        self.assertIn("processed", data)

    def test_health_includes_band_ai(self):
        res = self.client.get("/api/health")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIn("band_ai", data)
        self.assertTrue(data["band_ai"].get("connected"))


if __name__ == "__main__":
    unittest.main()
