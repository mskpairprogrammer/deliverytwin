import csv
import io
import unittest
from datetime import date

from deliverytwin import connect, initialize_database, seed_demo
from deliverytwin.backlog import _split_hours, build_backlog, latest_backlog
from deliverytwin.intake import approve_baseline

DAY = date(2026, 10, 17)
REQUEST = "We need one view of every customer across the business. Teams get different answers."


def baseline(**overrides):
    return {
        "id": "b-1", "request_text": REQUEST, "p50_hours": 4900, "p80_hours": 6100, "allocated_fte": 5.0,
        "capacity_fte": 6.0, "target_date": "2027-01-15", "confidence_pct": 72.5, "approved_at": "2026-10-17T00:00:00",
        "fingerprint": {
            "complexity": "high", "delivery_pattern": "incremental-integration",
            "systems": ["crm", "identity-resolution"], "roles": ["lead-engineer", "privacy-engineer"],
            "dependencies": ["privacy-review", "crm-mdm-integration"],
        },
        **overrides,
    }


class BacklogTests(unittest.TestCase):
    def test_feature_effort_adds_up_to_the_approved_p50(self) -> None:
        backlog = build_backlog(baseline(), DAY)
        epic = backlog["epic"]
        self.assertEqual(sum(feature["effort_hours"] for feature in epic["features"]), 4900)
        self.assertEqual((epic["effort_hours"], epic["p80_hours"], epic["reserve_hours"]), (4900, 6100, 1200))
        self.assertEqual(epic["title"], "We need one view of every customer across the business")
        self.assertEqual([f["kind"] for f in epic["features"]],
                         ["discovery", "dependency", "dependency", "system", "system", "release"])
        self.assertEqual(backlog["source"], "rules")

    def test_dates_stay_inside_the_committed_window(self) -> None:
        epic = build_backlog(baseline(), DAY)["epic"]
        for feature in epic["features"]:
            self.assertGreaterEqual(feature["start_date"], epic["start_date"])
            self.assertLessEqual(feature["target_date"], epic["target_date"])
            self.assertLessEqual(feature["start_date"], feature["target_date"])
        self.assertEqual(epic["features"][-1]["target_date"], "2027-01-15")
        # A start date after the target never produces features past the committed date.
        late = build_backlog(baseline(), date(2027, 2, 1))["epic"]
        self.assertTrue(all(f["target_date"] <= late["target_date"] for f in late["features"]))

    def test_split_is_exact_for_any_total(self) -> None:
        for total in (50, 1043, 4900, 7):
            parts = _split_hours(total, [10, 6, 25, 25, 20])
            self.assertEqual(sum(parts), total)
            self.assertTrue(all(part >= 0 for part in parts))

    def test_csv_uses_the_ado_hierarchy_layout_and_neutralizes_formulas(self) -> None:
        backlog = build_backlog(baseline(request_text="=HYPERLINK(\"http://x\")"), DAY)
        rows = list(csv.reader(io.StringIO(backlog["csv"])))
        self.assertEqual(rows[0][:3], ["Work Item Type", "Title 1", "Title 2"])
        self.assertEqual((rows[1][0], bool(rows[1][1]), rows[1][2]), ("Epic", True, ""))
        self.assertTrue(rows[1][1].startswith("'="))
        self.assertTrue(all(row[0] == "Feature" and row[1] == "" and row[2] for row in rows[2:]))
        self.assertEqual(len(rows) - 2, len(backlog["epic"]["features"]))
        self.assertIn("<li>", rows[2][3])

    def test_latest_backlog_follows_the_approved_baseline(self) -> None:
        connection = connect(":memory:")
        self.addCleanup(connection.close)
        initialize_database(connection)
        seed_demo(connection, DAY)
        self.assertIsNone(latest_backlog(connection, DAY))
        from deliverytwin.intake import analyze_intake

        analysis = analyze_intake(connection, REQUEST)
        answers = {item["id"]: item["suggested"] for item in analysis["clarifications"]}
        approved = approve_baseline(connection, REQUEST, answers, DAY, questions=analysis["clarifications"])
        backlog = latest_backlog(connection, DAY)
        self.assertEqual(backlog["baseline_id"], approved["baseline"]["id"])
        self.assertEqual(backlog["epic"]["effort_hours"], approved["baseline"]["p50_hours"])
        self.assertEqual(backlog["epic"]["target_date"], approved["baseline"]["target_date"])


if __name__ == "__main__":
    unittest.main()
