"""Approvals log: every approved COMMIT baseline and applied INTERVENE plan records who did it."""
import json
import os
import tempfile
import threading
import time
import socket
import unittest
import urllib.request
from datetime import date
from unittest.mock import patch

from deliverytwin import build_workflow_report, connect, initialize_database, seed_demo
from deliverytwin.app import clear_demo_data, save_recovery_scenario, serve_dashboard, simulate_sprints
from deliverytwin.approvals import LOCAL_APPROVER, PRINCIPAL_HEADER, approver_from_headers
from deliverytwin.extras import baseline_history
from deliverytwin.intake import analyze_intake, approve_baseline, latest_baseline
from deliverytwin.recovery import predict_scenario
from deliverytwin.storage import ApprovalRepository

REQUEST = "We need one view of every customer across the business. Teams get different answers."
DAY = date(2026, 10, 17)
APP_SERVICE = {"WEBSITE_SITE_NAME": "deliverytwin-demo"}
PAT = {"name": "pat@example.com", "source": "entra_id"}


class ApproverIdentityTests(unittest.TestCase):
    def test_app_service_trusts_the_entra_id_principal_header(self) -> None:
        self.assertEqual(approver_from_headers({PRINCIPAL_HEADER: " pat@example.com "}, APP_SERVICE), PAT)

    def test_outside_app_service_the_header_is_ignored(self) -> None:
        self.assertEqual(approver_from_headers({PRINCIPAL_HEADER: "mallory@example.com"}, {}), LOCAL_APPROVER)

    def test_no_signed_in_user_is_the_local_user(self) -> None:
        self.assertEqual(approver_from_headers({}, APP_SERVICE), LOCAL_APPROVER)
        self.assertEqual(approver_from_headers(None, APP_SERVICE), LOCAL_APPROVER)


class ApprovalLogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = connect(":memory:")
        initialize_database(self.connection)
        seed_demo(self.connection, DAY)
        with patch("deliverytwin.intake.openai_configured", return_value=False):
            analysis = analyze_intake(self.connection, REQUEST)
        self.questions = analysis["clarifications"]
        self.answers = {item["id"]: item["suggested"] for item in self.questions}

    def tearDown(self) -> None:
        self.connection.close()

    def approve(self, approver=None):
        return approve_baseline(self.connection, REQUEST, self.answers, DAY, questions=self.questions,
                                approver=approver)

    def test_an_approved_baseline_records_its_approver_and_figures(self) -> None:
        baseline = self.approve(PAT)["baseline"]
        self.assertEqual(baseline["approved_by"], PAT)
        self.assertEqual(latest_baseline(self.connection)["approved_by"], PAT)
        [entry] = ApprovalRepository(self.connection.app_database()).history()
        self.assertEqual((entry["stage"], entry["record_id"], entry["approver"]), ("commit", baseline["id"], PAT["name"]))
        self.assertEqual(entry["summary"]["confidence_pct"], baseline["confidence_pct"])
        self.assertEqual(entry["summary"]["target_date"], baseline["target_date"])

    def test_without_an_approver_the_local_user_is_recorded(self) -> None:
        self.assertEqual(self.approve()["baseline"]["approved_by"], LOCAL_APPROVER)

    def test_baseline_history_shows_who_approved_each_one(self) -> None:
        self.approve(PAT)
        self.approve()
        history = baseline_history(self.connection)["baselines"]
        self.assertEqual([item["approved_by"]["name"] for item in history], [LOCAL_APPROVER["name"], PAT["name"]])

    def test_an_applied_recovery_plan_records_its_approver(self) -> None:
        self.approve()
        simulate_sprints(self.connection, 3, DAY)
        report = build_workflow_report(self.connection)
        with patch("deliverytwin.recovery.openai_configured", return_value=False):
            preview = predict_scenario(report, {"allocated_fte": 7.5}, [])
        applied = save_recovery_scenario(self.connection, preview, PAT)
        self.assertEqual(applied["approved_by"], PAT)
        entry = ApprovalRepository(self.connection.app_database()).history()[0]
        self.assertEqual((entry["stage"], entry["record_id"]), ("intervene", str(applied["id"])))
        self.assertEqual(entry["summary"]["allocated_fte"], 7.5)

    def test_clearing_demo_data_clears_the_approvals_log(self) -> None:
        self.approve(PAT)
        clear_demo_data(self.connection)
        self.assertEqual(ApprovalRepository(self.connection.app_database()).history(), [])


class ApprovalEndpointTests(unittest.TestCase):
    """The server reads the approver from the request on App Service, and ignores the header elsewhere."""

    def serve(self) -> int:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        database = os.path.join(tempfile.mkdtemp(), "approvals.db")
        threading.Thread(target=serve_dashboard, args=(database, "127.0.0.1", port), daemon=True).start()
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                return port
            except OSError:
                time.sleep(0.05)
        self.fail("server did not start")

    def post(self, port: int, route: str, body: dict, headers: dict) -> dict:
        request = urllib.request.Request(f"http://127.0.0.1:{port}{route}", data=json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json", **headers}, method="POST")
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)

    def test_the_approve_endpoint_records_the_signed_in_user_on_app_service(self) -> None:
        port = self.serve()
        with patch("deliverytwin.intake.openai_configured", return_value=False):
            analysis = self.post(port, "/api/intake/analyze", {"text": REQUEST}, {})
        body = {"text": REQUEST, "questions": analysis["clarifications"],
                "answers": {item["id"]: item["suggested"] for item in analysis["clarifications"]}}
        with patch.dict(os.environ, APP_SERVICE):
            signed_in = self.post(port, "/api/intake/approve", body, {PRINCIPAL_HEADER: PAT["name"]})
        spoofed = self.post(port, "/api/intake/approve", body, {PRINCIPAL_HEADER: "mallory@example.com"})
        self.assertEqual(signed_in["baseline"]["approved_by"], PAT)
        self.assertEqual(spoofed["baseline"]["approved_by"], LOCAL_APPROVER)


if __name__ == "__main__":
    unittest.main()
