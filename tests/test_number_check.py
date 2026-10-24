"""Number check: AI text may only quote figures that are in the computed results it was given."""
import json
import unittest
from datetime import date
from unittest.mock import patch

from deliverytwin import build_workflow_report, connect, initialize_database, seed_demo
from deliverytwin.ai_common import unsupported_numbers
from deliverytwin.app import simulate_sprints
from deliverytwin.commit_ai import ask_whatif, challenge_commitment
from deliverytwin.intake import analyze_intake, evaluate_intake
from deliverytwin.recovery import analyze_intervention, predict_scenario
from deliverytwin.track_ai import build_brief

try:
    from tests.llm_stub import ScriptedLLM
except ImportError:  # discovery with -s tests
    from llm_stub import ScriptedLLM

REQUEST = "We need one view of every customer across the business. Teams get different answers."
DAY = date(2026, 10, 17)
INVENTED = " Confidence will reach 97.3% after 41 more days."


def inventing(llm, mutate):
    """Answer like ``llm``, then let ``mutate`` add an invented figure to the parsed reply."""
    def call(messages, json_mode=False):
        data = json.loads(llm(messages, json_mode=json_mode))
        mutate(data)
        return json.dumps(data)
    return call


class UnsupportedNumbersTests(unittest.TestCase):
    def test_supplied_figures_rounded_figures_dates_and_counts_pass(self) -> None:
        context = {"confidence_pct": 84.4, "p80_hours": 5750, "target_date": "2027-03-31",
                   "change_points": -50, "roles": ["a", "b", "c"], "note": "Sprint 3 of PRJ-024"}
        text = ("Confidence is 84.4% (about 84%) on 5,750 P80 hours, down 50 points, due March 31, 2027; "
                "3 roles; Sprint 3 of PRJ-024.")
        self.assertEqual(unsupported_numbers([text], context), [])

    def test_invented_figures_are_reported_once_each(self) -> None:
        self.assertEqual(unsupported_numbers(["Up to 97.3%, then 97.3% again, and 12 weeks.", None], {"pct": 84}),
                         ["97.3", "12"])

    def test_identifiers_with_letters_are_not_figures(self) -> None:
        self.assertEqual(unsupported_numbers(["Use the P80 estimate and the Q4 plan for gpt4."], {}), [])


class CommitAndTrackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = connect(":memory:")
        initialize_database(self.connection)
        seed_demo(self.connection, DAY)

    def tearDown(self) -> None:
        self.connection.close()

    def evaluate(self, adjustments=None):
        with patch("deliverytwin.intake.openai_configured", return_value=False):
            analysis = analyze_intake(self.connection, REQUEST)
        answers = {item["id"]: item["suggested"] for item in analysis["clarifications"]}
        return evaluate_intake(self.connection, REQUEST, answers, DAY, adjustments=adjustments,
                               questions=analysis["clarifications"])

    def with_llm(self, llm):
        return (patch("deliverytwin.ai_common.openai_configured", return_value=True),
                patch("deliverytwin.ai_common.complete", side_effect=llm))

    def test_a_challenge_that_invents_a_figure_falls_back_to_the_rules_review(self) -> None:
        evaluation = self.evaluate()
        configured, complete = self.with_llm(inventing(ScriptedLLM(), lambda data: data.update(
            summary=data["summary"] + INVENTED)))
        with configured, complete:
            review = challenge_commitment(self.connection, evaluation)
        self.assertEqual((review["source"], review["fallback_reason"]["code"]), ("rules", "number_check"))
        self.assertIn("97.3", review["fallback_reason"]["detail"])

    def test_a_what_if_reading_that_invents_a_figure_is_not_answered(self) -> None:
        configured, complete = self.with_llm(inventing(ScriptedLLM(), lambda data: data.update(
            reading=data["reading"] + INVENTED)))
        with configured, complete:
            answer = ask_whatif(self.connection, "What if we lose an engineer?", DAY, self.evaluate, {})
        self.assertFalse(answer["answerable"])
        self.assertEqual(answer["fallback_reason"]["code"], "number_check")

    def test_a_track_brief_that_invents_a_figure_falls_back_to_the_computed_briefing(self) -> None:
        simulate_sprints(self.connection, 3, DAY)
        report = build_workflow_report(self.connection)
        configured, complete = self.with_llm(inventing(ScriptedLLM(), lambda data: data.update(
            narrative=data["narrative"] + INVENTED)))
        with configured, complete:
            brief = build_brief(report, None)
        self.assertEqual((brief["source"], brief["fallback_reason"]["code"]), ("rules", "number_check"))

    def test_a_track_brief_may_quote_how_far_the_slip_moved(self) -> None:
        simulate_sprints(self.connection, 3, DAY)
        report = build_workflow_report(self.connection)
        variance = build_brief(report, None)["variance"]
        moved = variance["current_slip_days"] - variance["baseline_slip_days"]
        configured, complete = self.with_llm(inventing(ScriptedLLM(), lambda data: data.update(
            narrative=data["narrative"] + f" Projected slip grew by {moved} days.")))
        with configured, complete:
            brief = build_brief(report, None)
        self.assertEqual((brief["source"], brief["fallback_reason"]), ("llm", None))


class RecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = connect(":memory:")
        initialize_database(self.connection)
        seed_demo(self.connection, DAY)
        simulate_sprints(self.connection, 3, DAY)
        self.report = build_workflow_report(self.connection)

    def tearDown(self) -> None:
        self.connection.close()

    def with_llm(self, llm):
        return (patch("deliverytwin.recovery.openai_configured", return_value=True),
                patch("deliverytwin.recovery.complete", side_effect=llm))

    def test_an_invented_figure_in_the_diagnosis_falls_back_to_rules_scenarios(self) -> None:
        configured, complete = self.with_llm(inventing(ScriptedLLM(), lambda data: data.update(
            diagnosis=data["diagnosis"] + INVENTED)))
        with configured, complete:
            analysis = analyze_intervention(self.report, [])
        self.assertEqual((analysis["source"], analysis["fallback_reason"]["code"]), ("rules", "number_check"))

    def test_a_scenario_whose_text_invents_a_figure_is_dropped(self) -> None:
        def mutate(data):
            data["scenarios"][2]["commentary"] = INVENTED + data["scenarios"][2]["commentary"]

        configured, complete = self.with_llm(inventing(ScriptedLLM(), mutate))
        with configured, complete:
            analysis = analyze_intervention(self.report, [])
        self.assertEqual((analysis["source"], analysis["dropped_scenarios"]), ("llm", 1))
        self.assertEqual(len(analysis["scenarios"]), 2)

    def test_a_prediction_that_invents_a_figure_uses_the_capacity_model(self) -> None:
        configured, complete = self.with_llm(inventing(ScriptedLLM(), lambda data: data.update(
            commentary=INVENTED + data["commentary"])))
        with configured, complete:
            result = predict_scenario(self.report, {"allocated_fte": 7.5}, [])
        self.assertEqual(result["prediction"]["source"], "rules")
        self.assertEqual(result["prediction"]["fallback_reason"]["code"], "number_check")
        self.assertEqual(result["confidence_pct"], result["model_check_pct"])


if __name__ == "__main__":
    unittest.main()
