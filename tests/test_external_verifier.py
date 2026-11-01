"""Hand-worked checks for the independent external audit, without real artifacts or SMS."""

from copy import deepcopy
from decimal import Decimal
import importlib.util
from pathlib import Path
from typing import Any
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/verify_external_test.py"
SPEC = importlib.util.spec_from_file_location("external_audit_under_test", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
verifier = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verifier)


def metric_summary(macro_f1: float, false_positive: int, ham_count: int) -> dict[str, Any]:
    """Build an aggregate-only synthetic summary for signed-rate calculations."""
    return {
        "full_denominator_conservative_metrics": {
            "macro_f1": macro_f1,
            "spam_precision": macro_f1,
            "spam_recall": macro_f1,
            "ham_precision": macro_f1,
            "ham_recall": macro_f1,
            "accuracy": macro_f1,
            "confusion": {
                "true_negative": ham_count - false_positive,
                "false_positive": false_positive,
            },
            "abstention_count_by_truth": {"ham": 0},
        }
    }


class ExternalVerifierTests(unittest.TestCase):
    def test_only_the_literal_user_placeholder_is_allowed(self) -> None:
        """Allow preregistered template metadata while rejecting raw-message and secret fields."""
        verifier._privacy({"deepseek": {"user_message_template": verifier.SAFE_TEMPLATE}})
        for value in (
            {"deepseek": {"user_message_template": {"role": "user", "content": "authored text"}}},
            {"response": {"content": "authored text"}},
            {"request_body": {"safe": True}},
            {"metadata": "sk-authored-dummy-credential"},
            {"source_rows": [1]},
        ):
            with self.subTest(value=value):
                with self.assertRaises(verifier.independent.VerificationFailure):
                    verifier._privacy(value)

    def test_public_duplicate_groups_do_not_imply_private_row_multiplicity(self) -> None:
        """Count published groups while identifying multiplicities as frozen aggregates only."""
        ingestion = {
            "malformed_rows_excluded": 0,
            "unsupported_label_rows_excluded": 0,
            "empty_rows_excluded": 0,
            "overlong_rows_excluded": 0,
            "ascii_letter_guard_rows_excluded": 1,
            "internal_exact_duplicate_groups": 2,
            "internal_exact_duplicate_rows_collapsed": 3,
            "conflicting_label_groups_quarantined": 1,
            "conflicting_label_rows_quarantined": 3,
        }
        exclusions = [
            {"sample_id": "a", "reason": "internal_exact_duplicate"},
            {"sample_id": "b", "reason": "internal_exact_duplicate"},
            {"sample_id": "c", "reason": "conflicting_labels"},
            {"sample_id": "d", "reason": "ascii_letter_guard"},
        ]
        result = verifier._audit_cleaning_counts(
            verifier.independent.Checks(), ingestion, exclusions
        )
        self.assertEqual(result["duplicate_groups_directly_counted"], 2)
        self.assertEqual(result["duplicate_rows_from_frozen_aggregate"], 3)
        self.assertIn("cannot be independently rederived", result["limitation"])
        broken = {**ingestion, "internal_exact_duplicate_rows_collapsed": 1}
        with self.assertRaises(verifier.independent.VerificationFailure):
            verifier._audit_cleaning_counts(verifier.independent.Checks(), broken, exclusions)

    def test_deltas_keep_the_sign_and_use_each_cohorts_ham_denominator(self) -> None:
        """A negative quality difference and positive false-positive rate must remain visible."""
        result = verifier._signed_deltas(metric_summary(0.8, 4, 10), metric_summary(0.9, 2, 20))
        self.assertAlmostEqual(result["macro_f1"], -0.1)
        self.assertEqual(result["external_ham_denominator"], 10)
        self.assertEqual(result["validation_ham_denominator"], 20)
        self.assertAlmostEqual(result["false_positive_ham_rate_delta"], 0.3)

    def test_paired_error_diagnostics_use_only_the_fixed_band(self) -> None:
        """Separate outside-band errors, external fixes, regressions and unknown fallbacks."""
        examples = (
            ("ham", 0.9, "ham"),
            ("ham", 0.6, "ham"),
            ("spam", 0.1, "ham"),
            ("spam", 0.7, "ham"),
            ("ham", 0.3, None),
        )
        rows = [
            {
                "truth": truth,
                "local": {"label": "spam" if score >= 0.5 else "ham", "spam_score": score},
                "deepseek": {
                    "status": "ok" if external is not None else "unknown",
                    "label": external,
                },
            }
            for truth, score, external in examples
        ]
        result = verifier._paired_diagnostics(rows)
        self.assertEqual(
            result["local_errors_by_position"], {"inside_fixed_band": 1, "outside_fixed_band": 2}
        )
        all_rows = result["paired_outcomes"]["all_rows"]
        escalated = result["paired_outcomes"]["fixed_escalated_rows"]
        self.assertEqual(all_rows["external_fixes_local_errors"], 2)
        self.assertEqual(all_rows["external_regressions_from_local_correct"], 1)
        self.assertEqual(all_rows["external_abstentions"], 1)
        self.assertEqual(escalated["rows"], 3)
        self.assertEqual(escalated["external_fixes_local_errors"], 1)
        self.assertEqual(escalated["external_regressions_from_local_correct"], 1)
        self.assertEqual(result["expected_fixed_cascade_error_count"], 3)
        self.assertEqual(result["fixed_cascade_error_change_vs_local"], 0)

    def test_known_usage_and_unknown_reservation_reconcile_chronologically(self) -> None:
        """Hand-priced cache tokens and overlapping dispatches independently exercise the cap."""
        usage = {
            "prompt_tokens": 100,
            "completion_tokens": 5,
            "total_tokens": 105,
            "prompt_cache_hit_tokens": 10,
            "prompt_cache_miss_tokens": 90,
        }
        estimate = {
            "max_output_tokens": 32,
            "request_utf8_bytes": 100,
            "conservative_input_tokens": 4196,
            "reservation_peak_yuan": "0.008648",
        }
        known = {
            "sample_id": "a" * 64,
            "request_sha256": "b" * 64,
            "status": "ok",
            "label": "ham",
            "requested_model": "deepseek-flash",
            "model": "deepseek-flash",
            "provider_id": "authored-response-one",
            "started_at_utc": "2026-11-01T00:00:00+00:00",
            "finished_at_utc": "2026-11-01T00:00:01+00:00",
            "reserved_yuan": "0.008648",
            "estimate": estimate,
            "usage": usage,
            "cost_yuan": "0.0001102",
            "conservative_peak_yuan": "0.0002204",
            "tariff_rates_yuan_per_million": {
                "cache_hit": "0.02",
                "cache_miss": "1",
                "output": "4",
            },
        }
        unknown = {
            "sample_id": "c" * 64,
            "request_sha256": "d" * 64,
            "status": "unknown",
            "label": None,
            "started_at_utc": "2026-11-01T00:00:00.5+00:00",
            "reserved_yuan": "0.008648",
            "estimate": estimate,
            "usage": None,
            "cost_yuan": None,
            "conservative_peak_yuan": None,
        }
        records = {known["sample_id"]: known, unknown["sample_id"]: unknown}
        budget = {
            "cap_yuan": "4",
            "authorized_max_cap_yuan": "4",
            "known_cost_yuan": "0.0001102",
            "conservative_peak_yuan": "0.0002204",
            "unknown_reserved_yuan": "0.008648",
            "cap_exposure_yuan": "0.0088684",
            "attempt_count": 2,
            "known_usage_count": 1,
            "unknown_usage_count": 1,
            "usage": usage,
        }
        result = verifier._accounting(
            verifier.independent.Checks(), records, budget, "2026-10-31T23:59:59+00:00"
        )
        self.assertEqual(result["known_cost_yuan"], Decimal("0.0001102"))
        self.assertEqual(result["maximum_chronological_exposure_yuan"], Decimal("0.017296"))
        self.assertEqual(result["unknown_reserved_yuan"], Decimal("0.008648"))
        broken = deepcopy(budget)
        broken["known_cost_yuan"] = "0.1"
        with self.assertRaises(verifier.independent.VerificationFailure):
            verifier._accounting(
                verifier.independent.Checks(), records, broken, "2026-10-31T23:59:59+00:00"
            )
        repeated = deepcopy(records)
        repeated[unknown["sample_id"]]["request_sha256"] = known["request_sha256"]
        with self.assertRaises(verifier.independent.VerificationFailure):
            verifier._accounting(
                verifier.independent.Checks(), repeated, budget, "2026-10-31T23:59:59+00:00"
            )


if __name__ == "__main__":
    unittest.main()
