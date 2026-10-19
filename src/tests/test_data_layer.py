import csv
import http.client
import json
import os
import socket
import sqlite3
import tempfile
import threading
import time
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from deliverytwin import connect, initialize_database, seed_demo
from deliverytwin.app import _is_loopback, serve_dashboard
from deliverytwin.data import (
    BaselineRepository,
    IntakeSessionRepository,
    DATASET_DIR,
    ALL_TABLES,
    APP_TABLES,
    TABLES,
    DatabricksDatabase,
    DatabricksSettings,
    DuckDBDatabase,
    HistoryRepository,
    SQLiteDatabase,
    generate_dataset,
    load_dataset,
    render_ddl,
)
from deliverytwin.data.repository import dataset_loaded, read_csv_rows
from deliverytwin.data.schema import check_identifier
from deliverytwin.intake import evaluate_intake

REQUEST = "We need one view of every customer across the business."
ANSWERS = {
    "architecture": "incremental", "data": "event_sync",
    "security": "pii_consent", "reuse": "reuse_mdm",
}


class FakeCursor:
    def __init__(self, log):
        self.log, self.description = log, None

    def execute(self, sql, params=None):
        self.log.append((sql, params))

    def fetchall(self):
        return []

    def close(self):
        pass


class FakeConnection:
    def __init__(self):
        self.log = []

    def cursor(self):
        return FakeCursor(self.log)

    def close(self):
        pass


def _backends():
    sqlite_connection = sqlite3.connect(":memory:")
    yield "sqlite", SQLiteDatabase(sqlite_connection)
    yield "duckdb", DuckDBDatabase()


class DatasetTests(unittest.TestCase):
    def test_checked_in_csvs_match_the_deterministic_generator(self) -> None:
        generated = generate_dataset()
        for name in TABLES:
            expected = [{k: str(v) for k, v in row.items()} for row in generated[name]]
            actual = [
                {k: v for k, v in row.items()}
                for row in read_csv_rows(name, DATASET_DIR)
            ]
            self.assertEqual(len(actual), len(expected), name)
            self.assertEqual(actual, expected, name)
        self.assertEqual(len(list((DATASET_DIR / "ram_transcripts").glob("HIST-*.md"))), 50)

    def test_dataset_has_fifty_internally_consistent_projects(self) -> None:
        tables = generate_dataset()
        estimates = tables["project_estimate_summary"]
        self.assertEqual([row["project_id"] for row in estimates], [f"HIST-{n:03d}" for n in range(1, 51)])
        ids = {row["project_id"] for row in estimates}
        for name in ("project_intakes", "project_role_estimates", "project_resource_assignments",
                     "project_approval_dependencies", "project_lessons"):
            self.assertTrue({row["project_id"] for row in tables[name]} <= ids, name)
        self.assertTrue({row["prjnumber"] for row in tables["devops_workitems_details"]} <= ids)
        for row in estimates:
            self.assertLess(row["p50_hours"], row["p80_hours"])
            self.assertGreater(row["actual_hours"], 0)
            self.assertTrue(0 < row["confidence_score"] <= 1)
        complexity = {row["estimated_complexity"] for row in tables["project_intakes"]}
        self.assertEqual(complexity, {"low", "medium", "high"})
        overruns = sorted(row["actual_hours"] / row["p50_hours"] for row in estimates)
        self.assertGreater(overruns[25], 1.0)
        self.assertGreater(overruns[-1] - overruns[0], 0.4)
        self.assertEqual(len({row["suggested_option"] for row in tables["project_lessons"]}), 10)

    def test_ram_transcripts_use_the_delivery_intel_format(self) -> None:
        sample = next((DATASET_DIR / "ram_transcripts").glob("HIST-001_*.md")).read_text(encoding="utf-8")
        for marker in ("**Project:**", "**Project ID:** HIST-001", "**Meeting date:**", "**Decision:**",
                       "## Participants", "## 00:00 - Scope Review", "## 00:26 - Decision and Action Items"):
            self.assertIn(marker, sample)


class DataLayerContractTests(unittest.TestCase):
    def test_every_local_backend_loads_idempotently_and_returns_identical_history(self) -> None:
        histories = {}
        for name, database in _backends():
            with self.subTest(backend=name):
                self.assertFalse(dataset_loaded(database))
                first = load_dataset(database)
                second = load_dataset(database)
                self.assertEqual(first, second)
                self.assertEqual(first["project_estimate_summary"], 50)
                self.assertTrue(dataset_loaded(database))
                count = database.query(f"SELECT COUNT(*) AS n FROM {database.table('project_estimate_summary')}")
                self.assertEqual(count[0]["n"], 50)
                projects = HistoryRepository(database).list_projects()
                self.assertEqual(len(projects), 50)
                histories[name] = projects
        self.assertEqual(histories["sqlite"], histories["duckdb"])

    def test_history_repository_derives_the_matching_fields(self) -> None:
        database = DuckDBDatabase()
        load_dataset(database)
        repository = HistoryRepository(database)
        project = next(p for p in repository.list_projects() if p["project_id"] == "HIST-008")
        self.assertEqual(project["structure"], "legacy-modernization")
        self.assertIn("customer-data-platform", project["technologies"].split(","))
        self.assertEqual(project["integrations"], len(project["technologies"].split(",")))
        self.assertAlmostEqual(
            project["base_p50_hours"] * (1 + project["integration_buffer_pct"] / 100),
            project["planned_effort_hours"], delta=1,
        )
        self.assertEqual(len(project["lessons"]), 4)
        self.assertIn("Delivery model: incremental-integration", project["description"])
        summary = repository.portfolio_summary()
        self.assertEqual(summary["projects"], 50)
        self.assertGreater(summary["median_effort_overrun"], 1.0)

    def test_sql_identifiers_are_validated(self) -> None:
        database = DuckDBDatabase()
        with self.assertRaises(ValueError):
            database.table("project_intakes; DROP TABLE x")
        with self.assertRaises(ValueError):
            check_identifier("a-b")
        with self.assertRaises(ValueError):
            DatabricksDatabase(DatabricksSettings("h", "/p", catalog="dpm_dev; DROP", schema="x"), FakeConnection())

    def test_values_are_bound_as_parameters_not_interpolated(self) -> None:
        database = DuckDBDatabase()
        database.create_tables()
        hostile = "x'); DROP TABLE project_intakes; --"
        database.upsert("project_lessons", [{
            "project_id": hostile, "category": "data", "lesson": "l", "suggested_option": "o",
        }])
        rows = database.query(f"SELECT project_id FROM {database.table('project_lessons')}")
        self.assertEqual(rows, [{"project_id": hostile}])
        self.assertEqual(database.query(f"SELECT COUNT(*) AS n FROM {database.table('project_intakes')}")[0]["n"], 0)


class AppTableTests(unittest.TestCase):
    ROW = {
        "commitment_baselines": {
            "baseline_id": "b-1", "request_text": "r", "answers_json": "{}", "fingerprint_json": "{}",
            "p50_hours": 4650, "p80_hours": 5750, "integration_buffer_pct": 19,
            "allocated_fte": 7.0, "capacity_fte": 8.5, "target_date": "2027-03-31",
            "confidence_pct": 62.2, "approved_at": "2026-10-19T02:29:25+00:00",
        },
        "intake_sessions": {
            "session_id": "s-1", "started_at": "2026-10-19T02:00:00+00:00",
            "first_rom_at": "2026-10-19T02:05:00+00:00", "approved_at": None,
        },
        "recovery_scenarios": {
            "scenario_id": 1, "initiative_id": "x", "recommended_option": "C", "allocated_fte": 7.0,
            "scope_defer_pct": 10, "target_date": "2027-03-31", "incremental_cost_k": 0,
            "confidence_pct": 48, "saved_at": "2026-10-19T02:29:25+00:00",
        },
        "recovery_target_updates": {
            "scenario_id": 1, "source_key": "servicenow", "payload_json": "{\"action\": \"escalate\"}",
        },
        "track_snapshots": {
            "snapshot_id": "t-1", "initiative_id": "x", "sprint_number": 1, "as_of": "2026-11-01",
            "on_time_pct": 53, "projected_slip_days": 21, "unplanned_story_points": 16,
            "support_hours_per_week": 4, "captured_at": "2026-11-01T09:00:00+00:00",
        },
    }

    def test_every_app_table_round_trips_on_the_local_backends(self) -> None:
        self.assertEqual(set(self.ROW), set(APP_TABLES))
        self.assertFalse(set(APP_TABLES) & set(TABLES))
        for backend, database in _backends():
            with self.subTest(backend=backend):
                database.create_tables()
                for name, row in self.ROW.items():
                    database.upsert(name, [row])
                    database.upsert(name, [row])
                    rows = database.query(f"SELECT * FROM {database.table(name)}")
                    self.assertEqual(len(rows), 1, name)
                    self.assertEqual(rows[0][APP_TABLES[name].key[0]], row[APP_TABLES[name].key[0]])

    def test_dataset_loading_does_not_create_app_tables(self) -> None:
        database = DuckDBDatabase()
        load_dataset(database)
        names = {row["table_name"] for row in database.query("SELECT table_name FROM information_schema.tables")}
        self.assertTrue(set(TABLES) <= names)
        self.assertFalse(set(APP_TABLES) & names)

    def test_databricks_binds_timestamps_as_datetimes(self) -> None:
        connection = FakeConnection()
        database = DatabricksDatabase(DatabricksSettings("h", "/p"), connection)
        database.upsert("intake_sessions", [self.ROW["intake_sessions"]])
        _, params = connection.log[-1]
        self.assertIsInstance(params[1], datetime)
        self.assertIsNone(params[3])


BASELINE = {
    "request_text": "r", "answers_json": "{}", "fingerprint_json": "{}", "p50_hours": 4650,
    "p80_hours": 5750, "integration_buffer_pct": 19, "allocated_fte": 7.0, "capacity_fte": 8.5,
    "target_date": "2027-03-31", "confidence_pct": 62.2,
}


class AppRepositoryTests(unittest.TestCase):
    def test_baselines_get_unique_ids_and_the_latest_wins_on_every_backend(self) -> None:
        for backend, database in _backends():
            with self.subTest(backend=backend):
                repository = BaselineRepository(database)
                self.assertIsNone(repository.latest())
                first = repository.save(BASELINE)
                second = repository.save({**BASELINE, "p50_hours": 5000, "target_date": "2027-06-30"})
                self.assertNotEqual(first, second)
                latest = repository.latest()
                self.assertEqual((latest["id"], latest["p50_hours"], latest["target_date"]),
                                 (second, 5000, "2027-06-30"))
                self.assertEqual(repository.get(first)["p50_hours"], 4650)
                self.assertEqual(latest["status"], "approved")
                self.assertIsNone(repository.get("missing"))

    def test_intake_sessions_keep_the_first_timestamps_on_every_backend(self) -> None:
        start = datetime(2026, 10, 17, 12, tzinfo=timezone.utc)
        for backend, database in _backends():
            with self.subTest(backend=backend):
                repository = IntakeSessionRepository(database)
                repository.record("s-1", "started", start)
                repository.record("s-1", "rom_ready", start + timedelta(minutes=2))
                repository.record("s-1", "rom_ready", start + timedelta(minutes=5))
                repository.record("s-1", "approved", start + timedelta(minutes=10))
                repository.record("s-1", "approved", start + timedelta(minutes=30))
                repository.record("s-2", "started", start)
                sessions = {row["session_id"]: row for row in repository.sessions()}
                self.assertEqual(sessions["s-1"]["started_at"], start)
                self.assertEqual(sessions["s-1"]["first_rom_at"], start + timedelta(minutes=2))
                self.assertEqual(sessions["s-1"]["approved_at"], start + timedelta(minutes=10))
                self.assertIsNone(sessions["s-2"]["first_rom_at"])
                with self.assertRaises(ValueError):
                    repository.record("s-1", "bogus", start)

    def test_repositories_only_create_their_own_table(self) -> None:
        database = DuckDBDatabase()
        BaselineRepository(database)
        names = {row["table_name"] for row in database.query("SELECT table_name FROM information_schema.tables")}
        self.assertEqual(names, {"commitment_baselines"})

    def test_databricks_returns_datetimes_and_dates_normalised(self) -> None:
        columns = [(name,) for name in (
            "baseline_id", "request_text", "answers_json", "fingerprint_json", "p50_hours",
            "p80_hours", "integration_buffer_pct", "allocated_fte", "capacity_fte",
            "target_date", "confidence_pct", "approved_at")]

        class Cursor(FakeCursor):
            def execute(self, sql, params=None):
                super().execute(sql, params)
                self.description = columns if sql.startswith("SELECT") else None

            def fetchall(self):
                return [("b", "r", "{}", "{}", 1, 2, 3, 4, 5, date(2027, 3, 31),
                         6, datetime(2026, 10, 19, 2, 29, 25))]

        class Connection(FakeConnection):
            def cursor(self):
                return Cursor(self.log)

        database = DatabricksDatabase(DatabricksSettings("h", "/p"), Connection())
        latest = BaselineRepository(database).latest()
        self.assertEqual((latest["target_date"], latest["approved_at"]), ("2027-03-31", "2026-10-19T02:29:25"))
        self.assertIn("ORDER BY approved_at DESC", database.connection.log[-1][0])
        self.assertTrue(database.connection.log[-1][0].startswith("SELECT"))


class SmokeTests(unittest.TestCase):
    def test_smoke_checks_pass_on_every_local_backend(self) -> None:
        from deliverytwin.data.smoke import format_report, run_checks

        for backend, database in _backends():
            with self.subTest(backend=backend):
                results = run_checks(database)
                self.assertEqual([name for name, ok, _ in results if not ok], [], format_report(results))
                self.assertEqual(len(results), 8)

    def test_smoke_reports_failures_instead_of_raising(self) -> None:
        from deliverytwin.data.smoke import format_report, run_checks

        class Broken(DuckDBDatabase):
            def execute(self, sql, params=()):
                raise RuntimeError("permission denied")

        results = run_checks(Broken())
        self.assertTrue(any(not ok and "permission denied" in detail for _, ok, detail in results))
        self.assertIn("checks passed", format_report(results))

    def test_databricks_smoke_requires_workspace_settings(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            from deliverytwin.app import main

            with self.assertRaises(SystemExit):
                main(["databricks-smoke"])


class DatabricksAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = FakeConnection()
        self.database = DatabricksDatabase(
            DatabricksSettings("adb.azuredatabricks.net", "/sql/1.0/warehouses/test"),
            self.connection,
        )

    def test_ddl_is_unity_catalog_delta(self) -> None:
        self.database.create_tables()
        statements = [sql for sql, _ in self.connection.log]
        self.assertEqual(len(statements), len(ALL_TABLES))
        self.assertTrue(all("USING DELTA" in sql for sql in statements))
        self.assertTrue(all(sql.startswith("CREATE TABLE IF NOT EXISTS dpm_dev.mlops.") for sql in statements))
        self.assertTrue(any("submitted_date DATE" in sql for sql in statements))

    def test_upsert_uses_merge_with_positional_parameters(self) -> None:
        self.database.upsert("project_lessons", [{
            "project_id": "HIST-001", "category": "data", "lesson": "text", "suggested_option": "event_sync",
        }])
        sql, params = self.connection.log[-1]
        self.assertTrue(sql.startswith("MERGE INTO dpm_dev.mlops.project_lessons AS t USING (SELECT ? AS project_id"))
        self.assertIn("t.project_id = s.project_id AND t.category = s.category", sql)
        self.assertIn("WHEN MATCHED THEN UPDATE SET t.lesson = s.lesson", sql)
        self.assertNotIn("HIST-001", sql)
        self.assertEqual(params, ["HIST-001", "data", "text", "event_sync"])

    def test_dates_are_bound_as_dates_for_delta_date_columns(self) -> None:
        self.database.upsert("project_intakes", [{"project_id": "HIST-001", "submitted_date": "2024-03-04"}])
        _, params = self.connection.log[-1]
        self.assertIn(date(2024, 3, 4), params)

    def test_settings_come_from_the_environment_and_require_a_warehouse(self) -> None:
        settings = DatabricksSettings.from_env({
            "DATABRICKS_HOST": "https://adb-1.azuredatabricks.net/", "DATABRICKS_HTTP_PATH": "/sql/1.0/warehouses/w",
            "DELIVERYTWIN_SCHEMA": "dt_test",
        })
        self.assertEqual((settings.server_hostname, settings.schema, settings.catalog),
                         ("adb-1.azuredatabricks.net", "dt_test", "dpm_dev"))
        with self.assertRaises(RuntimeError):
            DatabricksSettings.from_env({})

    def test_checked_in_delta_ddl_matches_the_schema(self) -> None:
        checked_in = (Path(__file__).parents[1] / "deliverytwin" / "data" / "databricks_ddl.sql").read_text(encoding="utf-8")
        self.assertEqual(checked_in.strip(), render_ddl("databricks", "dpm_dev", "mlops").strip())


class CommitRolesTests(unittest.TestCase):
    def test_commitment_carries_its_own_role_allocations_without_a_simulation(self) -> None:
        connection = connect(":memory:")
        initialize_database(connection)
        seed_demo(connection, date(2026, 10, 17))
        commitment = evaluate_intake(connection, REQUEST, ANSWERS, date(2026, 10, 17))["commitment"]
        self.assertEqual(
            {row["role_name"]: row["allocated_fte"] for row in commitment["resource_allocations"]},
            {"CRM Integration Engineer": 2.8, "Customer 360 Lead": 1.0,
             "Customer Data Engineer": 2.0, "Customer Privacy Engineer": 1.2},
        )


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class SecurityGuardTests(unittest.TestCase):
    def test_loopback_detection(self) -> None:
        for host in ("127.0.0.1", "::1", "localhost"):
            self.assertTrue(_is_loopback(host), host)
        for host in ("0.0.0.0", "10.1.2.3", "", "example.com"):
            self.assertFalse(_is_loopback(host), host)

    def test_refuses_to_expose_the_api_without_a_trusted_gateway(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DELIVERYTWIN_TRUSTED_AUTH", None)
            with self.assertRaises(SystemExit) as raised:
                serve_dashboard(":memory:", "0.0.0.0", _free_port())
        self.assertIn("DELIVERYTWIN_TRUSTED_AUTH", str(raised.exception))

    def _serve(self, demo_mode: str | None):
        directory = tempfile.mkdtemp()
        database = os.path.join(directory, "guard.db")
        port = _free_port()
        env = {"DELIVERYTWIN_DEMO_MODE": demo_mode} if demo_mode is not None else {}
        with patch.dict(os.environ, env):
            if demo_mode is None:
                os.environ.pop("DELIVERYTWIN_DEMO_MODE", None)
            thread = threading.Thread(
                target=serve_dashboard, args=(database, "127.0.0.1", port), daemon=True
            )
            thread.start()
            deadline = time.time() + 5
            while time.time() < deadline:
                try:
                    socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                    break
                except OSError:
                    time.sleep(0.05)
        return port

    @staticmethod
    def _post(port: int, route: str, headers: dict[str, str] | None = None) -> int:
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        body = json.dumps({"confirm": True})
        connection.request("POST", route, body, {"Content-Type": "application/json", **(headers or {})})
        status = connection.getresponse().status
        connection.close()
        return status

    def test_cross_origin_posts_are_rejected(self) -> None:
        port = self._serve(None)
        self.assertEqual(self._post(port, "/api/reset-all", {"Origin": "http://evil.example"}), 403)
        self.assertEqual(self._post(port, "/api/reset-all", {"Origin": f"http://127.0.0.1:{port}"}), 200)

    def test_demo_operations_can_be_disabled(self) -> None:
        port = self._serve("0")
        self.assertEqual(self._post(port, "/api/reset-all"), 403)
        self.assertEqual(self._post(port, "/api/simulate"), 403)


if __name__ == "__main__":
    unittest.main()
