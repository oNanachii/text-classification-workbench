"""Hand-worked paired predictions verify coverage, abstention and cascade accounting."""

from copy import deepcopy
from decimal import Decimal
import json
from typing import Any
import unittest

from textlab.comparison_analysis import CASCADE_BANDS, summarize


def authored_row(
    identity: str,
    truth: str,
    score: float,
    remote_label: str | None,
    *,
    status: str = "ok",
    cost: str | None = "0.001",
    reserved: str = "0.02",
    local_ms: float = 2.0,
    remote_ms: float | None = 10.0,
    failure: bool = False,
) -> dict[str, Any]:
    """Create text-free synthetic predictions with explicit status, timing and billing evidence."""
    return {
        "sample_id": identity,
        "truth": truth,
        "local": {
            "status": "ok",
            "label": "spam" if score >= 0.5 else "ham",
            "spam_score": score,
            "elapsed_ms": local_ms,
        },
        "deepseek": {
            "status": status,
            "label": remote_label,
            "elapsed_ms": remote_ms,
            "usage": {"input_tokens": 10, "output_tokens": 1} if cost is not None else None,
            "cost_yuan": cost,
            "reserved_yuan": reserved,
            "failure": failure,
        },
    }


class ComparisonAnalysisTests(unittest.TestCase):
    def test_complete_models_and_recommendation_match_hand_computation(self) -> None:
        """Prefer the cheapest strictly improving band while exposing the best observed band."""
        rows = [
            authored_row("one", "ham", 0.1, "ham", cost="0.001"),
            authored_row("two", "ham", 0.5, "ham", cost="0.002"),
            authored_row("three", "spam", 0.5, "spam", cost="0.003"),
            authored_row("four", "spam", 0.1, "spam", cost="0.004"),
        ]
        report = summarize(rows)
        local = report["local"]["full_denominator_conservative_metrics"]
        self.assertEqual(local["macro_f1"], 0.5)
        self.assertEqual(local["spam_precision"], 0.5)
        self.assertEqual(local["spam_recall"], 0.5)
        self.assertEqual(local["false_positive_ham_count"], 1)
        self.assertEqual(report["deepseek"]["completed_only_metrics"]["macro_f1"], 1.0)
        self.assertEqual(report["deepseek"]["cost"]["known_cost_yuan"], "0.010")
        self.assertEqual(
            Decimal(report["deepseek"]["cost"]["known_cost_yuan_per_1000_requested_rows"]),
            Decimal("2.5"),
        )
        narrow = report["cascades"][0]
        self.assertEqual(narrow["planned_escalation_count"], 2)
        self.assertEqual(narrow["planned_escalation_fraction"], 0.5)
        self.assertAlmostEqual(
            narrow["full_denominator_conservative_metrics"]["macro_f1"], (0.8 + 2 / 3) / 2
        )
        self.assertEqual(narrow["cost"]["known_cost_yuan"], "0.005")
        self.assertEqual(report["recommendation"]["band"], [0.45, 0.55])
        self.assertEqual(report["best_observed_nonzero_band"]["band"], [0.1, 0.9])
        self.assertEqual(report["best_observed_nonzero_band"]["macro_f1"], 1.0)

    def test_incomplete_predictions_keep_denominators_and_do_not_invent_false_positives(
        self,
    ) -> None:
        """Separate successful-only quality from abstention errors on every requested row."""
        rows = [
            authored_row("unknown", "ham", 0.5, None, status="unknown", cost=None, failure=True),
            authored_row(
                "skipped",
                "spam",
                0.9,
                None,
                status="not_attempted",
                cost=None,
                reserved="0.03",
                remote_ms=None,
            ),
            authored_row("ok", "ham", 0.1, "ham", cost="0.01"),
        ]
        deepseek = summarize(rows)["deepseek"]
        self.assertEqual(deepseek["requested_count"], 3)
        self.assertEqual(deepseek["attempted_count"], 2)
        self.assertEqual(deepseek["completed_count"], 1)
        self.assertEqual(deepseek["unknown_count"], 1)
        self.assertEqual(deepseek["not_attempted_count"], 1)
        self.assertEqual(deepseek["failure_count"], 1)
        self.assertEqual(deepseek["coverage"], 1 / 3)
        complete = deepseek["completed_only_metrics"]
        conservative = deepseek["full_denominator_conservative_metrics"]
        self.assertEqual(complete["denominator"], 1)
        self.assertEqual(complete["accuracy"], 1.0)
        self.assertEqual(conservative["denominator"], 3)
        self.assertAlmostEqual(conservative["macro_f1"], 1 / 3)
        self.assertEqual(conservative["accuracy"], 1 / 3)
        self.assertEqual(conservative["confusion"]["false_positive"], 0)
        self.assertEqual(conservative["confusion"]["false_negative"], 0)
        self.assertEqual(conservative["class_false_negative_counts"], {"ham": 1, "spam": 1})
        self.assertEqual(deepseek["abstention_diagnostics"]["accuracy_lower_bound"], 1 / 3)
        self.assertEqual(deepseek["abstention_diagnostics"]["accuracy_upper_bound"], 1.0)
        self.assertEqual(deepseek["cost"]["unknown_reserved_yuan"], "0.02")
        self.assertEqual(deepseek["cost"]["unpriced_attempt_count"], 1)
        self.assertEqual(deepseek["cost"]["requested_rows_denominator"], 3)
        self.assertAlmostEqual(
            float(deepseek["cost"]["known_cost_yuan_per_1000_requested_rows"]), 10 / 3
        )

    def test_fallback_keeps_local_labels_and_charges_only_the_planned_subset(self) -> None:
        """Unknown escalations retain local output while failed-call reservations stay visible."""
        rows = [
            authored_row(
                "unknown",
                "ham",
                0.5,
                None,
                status="unknown",
                cost=None,
                remote_ms=20,
                failure=True,
            ),
            authored_row("outside", "spam", 0.9, "spam", cost="0.05", local_ms=3),
            authored_row("other", "ham", 0.1, "ham", cost="0.07", local_ms=4),
        ]
        report = summarize(rows)
        cascade = report["cascades"][0]
        self.assertEqual(cascade["planned_escalation_count"], 1)
        self.assertEqual(cascade["escalation_attempted_count"], 1)
        self.assertEqual(cascade["escalation_completed_count"], 0)
        self.assertEqual(cascade["escalation_unknown_count"], 1)
        self.assertEqual(cascade["escalation_failure_count"], 1)
        self.assertEqual(cascade["fallback_count"], 1)
        self.assertEqual(cascade["fallback_completed_count"], 1)
        self.assertEqual(cascade["coverage"], 1.0)
        self.assertEqual(cascade["completed_only_metrics"]["false_positive_ham_count"], 1)
        self.assertEqual(cascade["cost"]["known_cost_yuan"], "0")
        self.assertEqual(cascade["cost"]["unknown_reserved_yuan"], "0.02")
        self.assertFalse(cascade["eligible_for_recommendation"])
        self.assertEqual(cascade["latency_ms"]["completed"]["p50"], 4.0)
        self.assertAlmostEqual(cascade["latency_ms"]["completed"]["p95"], 20.2)
        self.assertIn("Counterfactual replay", cascade["latency_scope"])
        self.assertEqual(report["recommendation"]["strategy_id"], "local")

    def test_band_endpoints_are_inclusive(self) -> None:
        """Scores exactly at both boundaries must enter the planned escalation cohort."""
        rows = [
            authored_row("lower", "ham", 0.45, "ham"),
            authored_row("upper", "spam", 0.55, "spam"),
            authored_row("below", "ham", 0.449, "spam"),
            authored_row("above", "spam", 0.551, "ham"),
        ]
        cascade = summarize(rows)["cascades"][0]
        self.assertEqual(cascade["planned_escalation_count"], 2)
        self.assertEqual(cascade["cost"]["known_cost_yuan"], "0.002")
        self.assertEqual(cascade["completed_only_metrics"]["macro_f1"], 1.0)

    def test_latency_counts_unknown_attempts_and_keeps_missing_measurements_explicit(self) -> None:
        """P50/P95 must not silently drop failed-attempt or missing timing denominators."""
        rows = [
            authored_row("ok", "ham", 0.1, "ham", remote_ms=10),
            authored_row(
                "unknown",
                "spam",
                0.5,
                None,
                status="unknown",
                cost=None,
                remote_ms=20,
                failure=True,
            ),
            authored_row("untimed", "ham", 0.2, "ham", remote_ms=None),
            authored_row(
                "skipped",
                "spam",
                0.6,
                None,
                status="not_attempted",
                cost=None,
                remote_ms=None,
            ),
        ]
        report = summarize(rows)
        timing = report["deepseek"]["latency_ms"]
        self.assertEqual(timing["requested_rows_denominator"], 4)
        self.assertEqual(timing["attempted"]["status_denominator"], 3)
        self.assertEqual(timing["attempted"]["measurement_count"], 2)
        self.assertEqual(timing["attempted"]["missing_measurement_count"], 1)
        self.assertEqual(timing["attempted"]["p50"], 15.0)
        self.assertEqual(timing["attempted"]["p95"], 19.5)
        self.assertEqual(timing["completed"]["status_denominator"], 2)
        self.assertEqual(timing["completed"]["measurement_count"], 1)
        full_band = report["cascades"][-1]
        self.assertEqual(full_band["fallback_count"], 2)
        self.assertEqual(full_band["latency_ms"]["completed"]["measurement_count"], 2)
        self.assertEqual(full_band["latency_ms"]["completed"]["missing_measurement_count"], 2)

    def test_equal_quality_retains_local_and_unpriced_success_is_not_recommended(self) -> None:
        """External cost needs strict observed improvement and complete price evidence."""
        equal = [
            authored_row("ham", "ham", 0.2, "ham"),
            authored_row("spam", "spam", 0.8, "spam"),
        ]
        report = summarize(equal)
        self.assertEqual(report["recommendation"]["strategy_id"], "local")
        self.assertIsNotNone(report["best_observed_nonzero_band"])
        unpriced = [
            authored_row("ham", "ham", 0.5, "ham", cost=None),
            authored_row("spam", "spam", 0.7, "spam"),
        ]
        report = summarize(unpriced)
        self.assertEqual(report["recommendation"]["strategy_id"], "local")
        self.assertFalse(report["cascades"][0]["eligible_for_recommendation"])
        self.assertEqual(report["deepseek"]["coverage"], 1.0)
        self.assertEqual(report["deepseek"]["cost"]["unpriced_attempt_count"], 1)

    def test_explicit_frozen_band_does_not_search_the_validation_band_grid(self) -> None:
        """Evaluate one predeclared external interval while retaining the old default unchanged."""
        rows = [
            authored_row("ham", "ham", 0.1, "ham"),
            authored_row("spam", "spam", 0.5, "spam"),
        ]
        default = summarize(rows)
        self.assertEqual(default, summarize(rows, bands=CASCADE_BANDS))
        self.assertEqual(len(default["cascades"]), 6)
        explicit = summarize(rows, bands=((0.2, 0.8),))
        self.assertEqual(len(explicit["cascades"]), 1)
        self.assertEqual(explicit["cascades"][0]["band"], [0.2, 0.8])
        self.assertEqual(explicit["cascades"][0]["strategy_id"], "cascade[0.2,0.8]")
        self.assertEqual(explicit["local"], default["local"])
        self.assertEqual(explicit["deepseek"], default["deepseek"])
        self.assertEqual(
            explicit["cascades"][0],
            next(item for item in default["cascades"] if item["band"] == [0.2, 0.8]),
        )

    def test_explicit_bands_reject_invalid_or_duplicate_endpoints(self) -> None:
        """Reject ambiguous test protocols before computing any candidate metrics."""
        invalid = [
            None,
            [],
            "0.2,0.8",
            ((0.2,),),
            ((0.2, 0.8, 0.9),),
            ((0.8, 0.2),),
            ((-0.1, 0.8),),
            ((0.2, 1.1),),
            ((True, 0.8),),
            ((0.2, float("nan")),),
            ((0.2, float("inf")),),
            (("0.2", 0.8),),
            ((0.2, 0.8), (0.2, 0.8)),
        ]
        for bands in invalid:
            with self.subTest(bands=bands), self.assertRaises(ValueError):
                summarize([], bands=bands)

    def test_empty_input_is_json_serializable_without_spurious_metrics(self) -> None:
        """No requested rows must produce null rates rather than a false quality claim."""
        report = summarize([])
        self.assertEqual(report["requested_rows"], 0)
        self.assertIsNone(report["local"]["completed_only_metrics"]["macro_f1"])
        self.assertIsNone(report["deepseek"]["coverage"])
        self.assertIsNone(report["deepseek"]["cost"]["known_cost_yuan_per_1000_requested_rows"])
        self.assertEqual(report["recommendation"]["strategy_id"], "local")
        self.assertIsNone(report["best_observed_nonzero_band"])
        json.dumps(report, allow_nan=False)

    def test_input_is_not_mutated_and_invalid_evidence_is_rejected(self) -> None:
        """Keep cached predictions immutable and reject duplicates or incoherent labels."""
        rows = [authored_row("one", "ham", 0.5, "ham")]
        before = deepcopy(rows)
        summarize(rows)
        self.assertEqual(rows, before)
        invalid_rows = [rows + rows]
        for field, value in (("truth", "other"), ("sample_id", "")):
            invalid = deepcopy(rows)
            invalid[0][field] = value
            invalid_rows.append(invalid)
        for field, value in (("spam_score", float("nan")), ("label", "ham")):
            invalid = deepcopy(rows)
            invalid[0]["local"][field] = value
            invalid_rows.append(invalid)
        for field, value in (("elapsed_ms", -1), ("cost_yuan", "NaN"), ("reserved_yuan", "-1")):
            invalid = deepcopy(rows)
            invalid[0]["deepseek"][field] = value
            invalid_rows.append(invalid)
        invalid = deepcopy(rows)
        invalid[0]["deepseek"]["status"] = "unknown"
        invalid_rows.append(invalid)
        for invalid in invalid_rows:
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    summarize(invalid)


if __name__ == "__main__":
    unittest.main()
