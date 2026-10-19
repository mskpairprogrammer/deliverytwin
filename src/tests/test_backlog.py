import csv
import io
import unittest
from datetime import date

from deliverytwin import connect, initialize_database, seed_demo
from deliverytwin.backlog import _split_hours, build_backlog, diff_backlogs, latest_backlog, to_ado_rest
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
        self.assertEqual(rows[0][:4], ["Work Item Type", "Title 1", "Title 2", "Title 3"])
        self.assertEqual((rows[1][0], bool(rows[1][1]), rows[1][2]), ("Epic", True, ""))
        self.assertTrue(rows[1][1].startswith("'="))
        features = [row for row in rows[2:] if row[0] == "Feature"]
        stories = [row for row in rows[2:] if row[0] == "User Story"]
        self.assertTrue(all(row[1] == "" and row[2] and row[3] == "" for row in features))
        self.assertTrue(all(row[1] == "" and row[2] == "" and row[3] for row in stories))
        self.assertEqual(len(features), len(backlog["epic"]["features"]))
        self.assertEqual(len(stories), sum(len(f["stories"]) for f in backlog["epic"]["features"]))
        self.assertEqual(rows[3][0], "User Story")
        self.assertIn("<li>", features[0][4])

    def test_effort_is_split_across_roles_by_the_planned_team(self) -> None:
        allocations = [{"role_name": "lead-engineer", "allocated_fte": 1.0}, {"role_name": "data-engineer", "allocated_fte": 3.0}]
        epic = build_backlog(baseline(), DAY, allocations)["epic"]
        feature = next(f for f in epic["features"] if f["kind"] == "system")
        self.assertEqual(sum(a["hours"] for a in feature["assignments"]), feature["effort_hours"])
        hours = {a["role"]: a["hours"] for a in feature["assignments"]}
        self.assertGreater(hours["data-engineer"], hours["lead-engineer"] * 2)
        self.assertEqual({s["role"] for s in feature["stories"]}, {"data-engineer"})
        dependency = next(f for f in epic["features"] if f["kind"] == "dependency")
        self.assertIn("lead-engineer", dependency["roles"])

    def test_an_applied_recovery_scenario_moves_dates_defers_scope_and_is_diffed(self) -> None:
        base = build_backlog(baseline(), DAY)
        recovery = {"target_date": "2027-03-01", "scope_defer_pct": 40, "allocated_fte": 5.0, "recommended_option": "phase_scope"}
        plan = build_backlog(baseline(), DAY, None, recovery)
        epic = plan["epic"]
        self.assertEqual(epic["target_date"], "2027-03-01")
        self.assertGreater(epic["deferred_hours"], 0)
        self.assertEqual(sum(f["effort_hours"] for f in epic["features"] if f["kind"] != "deferred"), epic["effort_hours"])
        self.assertEqual(epic["effort_hours"] + epic["deferred_hours"], 4900)
        deferred = epic["features"][-1]
        self.assertEqual((deferred["kind"], deferred["start_date"], deferred["stories"]), ("deferred", None, []))
        changes = diff_backlogs(base, plan)
        self.assertTrue(any(c["item"] == "Epic" and "Target date" in c["change"] for c in changes))
        self.assertTrue(any(c["item"] == "Deferred scope: follow-on release" for c in changes))
        rows = list(csv.reader(io.StringIO(plan["csv"])))
        self.assertEqual([r[6:9] for r in rows if r[2].startswith("Deferred")][0][1:], ["", ""])

    def test_ado_rest_export_links_each_item_to_its_parent_and_is_marked_unsent(self) -> None:
        rest = to_ado_rest(build_backlog(baseline(), DAY))
        self.assertIn("NOT sent", rest["note"])
        items = rest["workItems"]
        self.assertEqual((items[0]["type"], items[0]["parent"]), ("Epic", None))
        self.assertTrue(all(i["parent"] for i in items[1:]))
        feature = items[1]
        paths = {op["path"] for op in feature["body"]}
        self.assertIn("/fields/System.Title", paths)
        self.assertIn("/relations/-", paths)
        self.assertTrue(any(i["type"] == "User Story" and i["parent"] == "feature-1" for i in items))
        self.assertTrue(all("{organization}" in i["url"] for i in items))

    def test_latest_backlog_follows_the_approved_baseline_and_the_applied_recovery(self) -> None:
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
        self.assertIsNone(backlog["plan"])
        self.assertTrue(any(a["hours"] for f in backlog["epic"]["features"] for a in f["assignments"]))

        scenarios = connection.app_database()
        record = {
            "scenario_id": 1, "initiative_id": "autopay-modernization", "recommended_option": "phase_scope",
            "allocated_fte": 5, "scope_defer_pct": 40, "target_date": "2027-05-01", "incremental_cost_k": 0,
            "confidence_pct": 70, "saved_at": "2999-01-01T00:00:00+00:00",
        }
        scenarios.upsert("recovery_scenarios", [record])
        followed = latest_backlog(connection, DAY)
        self.assertEqual(followed["plan"]["scenario"]["scope_defer_pct"], 40)
        self.assertEqual(followed["plan"]["epic"]["target_date"], "2027-05-01")
        self.assertTrue(followed["diff"])
        # A scenario saved before the current baseline was approved is not applied to it.
        scenarios.upsert("recovery_scenarios", [{**record, "saved_at": "2000-01-01T00:00:00+00:00"}])
        self.assertIsNone(latest_backlog(connection, DAY)["plan"])


if __name__ == "__main__":
    unittest.main()
