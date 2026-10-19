import unittest
from datetime import date, timedelta
from unittest.mock import patch

from deliverytwin import build_workflow_report, connect, initialize_database, seed_demo
from deliverytwin.app import simulate_sprints
from deliverytwin.commit_ai import ask_whatif, challenge_commitment
from deliverytwin.intake import analyze_intake, evaluate_intake
from deliverytwin.track_ai import build_brief, variance_attribution

try:
    from tests.llm_stub import ScriptedLLM
except ImportError:  # discovery with -s tests
    from llm_stub import ScriptedLLM

REQUEST = "We need one view of every customer across the business. Teams get different answers."
DAY = date(2026, 10, 17)


def with_llm(llm):
    return (patch("deliverytwin.ai_common.openai_configured", return_value=True),
            patch("deliverytwin.ai_common.complete", side_effect=llm))


class CommitAiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = connect(":memory:")
        initialize_database(self.connection)
        seed_demo(self.connection, DAY)
        # Template questions keep these tests offline even when a real key is in the environment.
        with patch("deliverytwin.intake.openai_configured", return_value=False):
            analysis = analyze_intake(self.connection, REQUEST)
        self.questions = analysis["clarifications"]
        self.answers = {item["id"]: item["suggested"] for item in self.questions}

    def tearDown(self) -> None:
        self.connection.close()

    def evaluate(self, adjustments=None):
        return evaluate_intake(self.connection, REQUEST, self.answers, DAY, adjustments=adjustments,
                               questions=self.questions)

    def ask(self, question, llm, adjustments=None):
        configured, complete = with_llm(llm)
        with configured, complete:
            return ask_whatif(self.connection, question, DAY, self.evaluate, adjustments or {})

    def test_challenge_reviews_the_computed_commitment_and_similar_projects(self) -> None:
        llm = ScriptedLLM()
        configured, complete = with_llm(llm)
        evaluation = self.evaluate()
        with configured, complete:
            review = challenge_commitment(self.connection, evaluation)
        self.assertEqual((review["source"], review["verdict"], review["fallback_reason"]), ("llm", "ready_with_conditions", None))
        self.assertEqual([risk["severity"] for risk in review["risks"]], ["high", "medium"])
        self.assertEqual(len(review["conditions"]), 2)
        prompt = llm.prompts[0]
        self.assertIn('"required_fte_p80"', prompt)
        self.assertIn(evaluation["matches"][0]["name"], prompt)
        self.assertEqual(llm.calls, ["challenge"])

    def test_invalid_or_failed_challenge_falls_back_to_labeled_rules(self) -> None:
        evaluation = self.evaluate()
        results = {}
        for name, llm in {"invalid": ScriptedLLM(challenge="invalid"), "failed": ScriptedLLM(fail=("challenge",))}.items():
            configured, complete = with_llm(llm)
            with configured, complete:
                results[name] = challenge_commitment(self.connection, evaluation)
            self.assertEqual(results[name]["source"], "rules")
        self.assertEqual(results["invalid"]["fallback_reason"]["code"], "invalid_response")
        self.assertIn("HTTP 502", results["failed"]["fallback_reason"]["detail"])
        with patch("deliverytwin.ai_common.openai_configured", return_value=False):
            self.assertEqual(challenge_commitment(self.connection, evaluation)["fallback_reason"], {"code": "not_configured"})

    def test_an_infeasible_commitment_is_never_called_ready(self) -> None:
        evaluation = self.evaluate({"allocated_fte": 0.5})
        self.assertFalse(evaluation["commitment"]["feasible"])
        configured, complete = with_llm(ScriptedLLM(challenge="ready"))
        with configured, complete:
            review = challenge_commitment(self.connection, evaluation)
        self.assertEqual((review["source"], review["verdict"]), ("rules", "not_ready"))
        self.assertEqual(review["risks"][0]["severity"], "high")
        self.assertIn("Capacity does not cover", review["risks"][0]["title"])

    def test_losing_an_engineer_is_answered_by_the_commitment_model(self) -> None:
        current = self.evaluate()["commitment"]
        llm = ScriptedLLM()
        answer = self.ask("What if we lose an engineer?", llm)
        self.assertTrue(answer["answerable"])
        self.assertEqual(answer["scenario"]["allocated_fte"], current["allocated_fte"] - 1)
        expected = self.evaluate(answer["adjustments"])["commitment"]
        self.assertEqual(answer["scenario"]["confidence_pct"], expected["confidence_pct"])
        self.assertLess(answer["scenario"]["confidence_pct"], answer["current"]["confidence_pct"])
        self.assertIn("losing one engineer", answer["reading"])
        self.assertEqual(llm.calls, ["ask"])

    def test_delay_and_scope_questions_move_the_other_levers(self) -> None:
        current = self.evaluate()["commitment"]
        delayed = self.ask("What if the start is delayed?", ScriptedLLM())
        self.assertEqual(delayed["scenario"]["target_date"],
                         (date.fromisoformat(current["target_date"]) + timedelta(days=28)).isoformat())
        self.assertGreaterEqual(delayed["scenario"]["confidence_pct"], current["confidence_pct"])
        # The model returns 20.0; it is coerced to a whole percent before validation.
        scope = self.ask("What if scope grows?", ScriptedLLM())
        self.assertEqual(scope["scenario"]["scope_change_pct"], 20)
        self.assertLess(scope["scenario"]["confidence_pct"], current["confidence_pct"])

    def test_unanswerable_out_of_range_and_unchanged_questions_are_reported(self) -> None:
        cases = {
            "What is the weather like?": "cannot be expressed",
            "What if we get a huge team?": "outside the model's limits",
            "Nothing changes": "did not change",
        }
        for question, expected in cases.items():
            answer = self.ask(question, ScriptedLLM())
            self.assertFalse(answer["answerable"], question)
            self.assertIn(expected, answer["reason"])
            self.assertNotIn("scenario", answer)
        for bad in ("", "   ", "x" * 301):
            with self.assertRaises(ValueError):
                self.ask(bad, ScriptedLLM())

    def test_questions_need_the_ai_and_say_so(self) -> None:
        answer = self.ask("What if we lose an engineer?", ScriptedLLM(fail=("ask",)))
        self.assertEqual((answer["source"], answer["answerable"]), ("rules", False))
        self.assertEqual(answer["fallback_reason"]["code"], "request_failed")
        self.assertIn("sliders", answer["reason"])
        with patch("deliverytwin.ai_common.openai_configured", return_value=False):
            offline = ask_whatif(self.connection, "What if we lose an engineer?", DAY, self.evaluate, {})
        self.assertEqual(offline["fallback_reason"], {"code": "not_configured"})


class TrackAiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = connect(":memory:")
        initialize_database(self.connection)
        seed_demo(self.connection, DAY)
        simulate_sprints(self.connection, 3, DAY)
        self.report = build_workflow_report(self.connection)

    def tearDown(self) -> None:
        self.connection.close()

    def test_variance_is_attributed_exactly_to_the_signals_that_moved_the_forecast(self) -> None:
        variance = variance_attribution(self.report)
        drivers = variance["drivers"]
        lost = variance["baseline_confidence_pct"] - variance["current_confidence_pct"]
        self.assertGreater(lost, 0)
        self.assertAlmostEqual(sum(d["confidence_points_lost"] for d in drivers), lost, delta=0.1 * len(drivers))
        self.assertAlmostEqual(sum(d["slip_days_added"] for d in drivers),
                               variance["current_slip_days"] - variance["baseline_slip_days"], delta=1.5)
        points = [d["confidence_points_lost"] for d in drivers]
        self.assertEqual(points, sorted(points, reverse=True))
        self.assertEqual(variance["confidence_change_points"], -lost)

    def test_ai_explains_every_driver_without_changing_the_numbers(self) -> None:
        llm = ScriptedLLM()
        configured, complete = with_llm(llm)
        with configured, complete:
            brief = build_brief(self.report, None)
        self.assertEqual((brief["source"], brief["fallback_reason"]), ("llm", None))
        computed = variance_attribution(self.report)["drivers"]
        self.assertEqual([d["key"] for d in brief["variance"]["drivers"]], [d["key"] for d in computed])
        self.assertEqual([d["confidence_points_lost"] for d in brief["variance"]["drivers"]],
                         [d["confidence_points_lost"] for d in computed])
        self.assertTrue(all(d["explanation"].startswith("Evidence from") for d in brief["variance"]["drivers"]))
        self.assertEqual(len(brief["watch_next"]), 2)
        self.assertEqual(brief["baseline"]["source"], "seed_baseline_no_approval_recorded")
        self.assertEqual(llm.calls, ["brief"])

    def test_incomplete_or_failed_ai_falls_back_to_computed_rules_briefing(self) -> None:
        for name, llm in {"invalid": ScriptedLLM(brief="invalid"), "failed": ScriptedLLM(fail=("brief",))}.items():
            configured, complete = with_llm(llm)
            with configured, complete:
                brief = build_brief(self.report, None)
            self.assertEqual(brief["source"], "rules", name)
            self.assertTrue(brief["variance"]["drivers"])
            self.assertTrue(all("costs about" in d["explanation"] for d in brief["variance"]["drivers"]))
            self.assertIn("on-time", brief["narrative"])
        with patch("deliverytwin.ai_common.openai_configured", return_value=False):
            self.assertEqual(build_brief(self.report, None)["fallback_reason"], {"code": "not_configured"})


if __name__ == "__main__":
    unittest.main()
