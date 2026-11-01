"""Exercise cost accounting on authored requests and temporary files only."""

from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import stat
from tempfile import TemporaryDirectory
import unittest

from textlab.comparison_budget import (
    BudgetStopped,
    Ledger,
    OFFPEAK_RATES,
    PEAK_RATES,
    ReservationExceeded,
    estimate_request,
    price_usage,
    tariff_rates,
)

IDENTITY = hashlib.sha256(b"synthetic-comparison-configuration").hexdigest()
OTHER_IDENTITY = hashlib.sha256(b"different-synthetic-configuration").hexdigest()
AUTHORED_TEXT = "Private authored fixture: bring the blue notebook."
PAYLOAD = {
    "model": "deepseek-flash",
    "messages": [{"role": "user", "content": AUTHORED_TEXT}],
    "max_tokens": 32,
    "thinking": {"type": "disabled"},
    "temperature": 0,
}
USAGE = {
    "prompt_tokens": 100,
    "completion_tokens": 3,
    "total_tokens": 103,
    "prompt_cache_hit_tokens": 80,
    "prompt_cache_miss_tokens": 20,
}


def good_result() -> dict:
    """Return one authored provider result so accounting tests never call an API."""
    return {
        "status": "ok",
        "label": "ham",
        "failure": False,
        "elapsed_ms": 12.5,
        "requested_model": "deepseek-flash",
        "model": "deepseek-flash",
        "provider_id": "synthetic-response-1",
        "request_id": "synthetic-request-1",
        "system_fingerprint": "synthetic-fingerprint",
        "usage": dict(USAGE),
    }


class UsageAndTariffTests(unittest.TestCase):
    """Check exact arithmetic and Beijing boundaries independently of filesystem state."""

    def test_cache_and_output_charges_are_exact_decimals(self) -> None:
        """Price cache hits separately so a floating-point error cannot consume the cap."""
        self.assertEqual(price_usage(USAGE, PEAK_RATES), Decimal("0.0000672"))
        self.assertEqual(price_usage(USAGE, OFFPEAK_RATES), Decimal("0.0000336"))
        without_miss = {k: v for k, v in USAGE.items() if k != "prompt_cache_miss_tokens"}
        self.assertEqual(price_usage(without_miss, PEAK_RATES), Decimal("0.0000672"))

    def test_invalid_usage_is_never_treated_as_zero_cost(self) -> None:
        """Reject missing or contradictory counters so unknown charges stay reserved."""
        invalid = [
            {},
            {**USAGE, "total_tokens": 0},
            {**USAGE, "prompt_tokens": True},
            {**USAGE, "completion_tokens": -1},
            {**USAGE, "prompt_cache_hit_tokens": 101},
            {**USAGE, "prompt_cache_miss_tokens": 0},
        ]
        for usage in invalid:
            with self.subTest(usage=usage), self.assertRaises(ValueError):
                price_usage(usage, PEAK_RATES)

    def test_beijing_peak_boundaries_and_weekend(self) -> None:
        """Use local billing windows rather than the host timezone for tariff estimates."""
        examples = {
            "2026-11-02T00:59:59+00:00": OFFPEAK_RATES,
            "2026-11-02T01:00:00+00:00": PEAK_RATES,
            "2026-11-02T03:59:59+00:00": PEAK_RATES,
            "2026-11-02T04:00:00+00:00": OFFPEAK_RATES,
            "2026-11-02T06:00:00+00:00": PEAK_RATES,
            "2026-11-02T10:00:00+00:00": OFFPEAK_RATES,
            "2026-11-01T01:00:00+00:00": OFFPEAK_RATES,
        }
        for started, expected in examples.items():
            with self.subTest(started=started):
                self.assertEqual(tariff_rates(started), expected)
        with self.assertRaises(ValueError):
            tariff_rates("2026-11-02T09:00:00")

    def test_weekday_holiday_remains_conservatively_peak(self) -> None:
        """Avoid understating cost when no Chinese holiday calendar is consulted."""
        self.assertEqual(tariff_rates("2026-10-01T09:00:00+08:00"), PEAK_RATES)

    def test_estimate_uses_utf8_bytes_and_full_output_allowance(self) -> None:
        """Reserve a byte-based allowance while labelling the dry-run token count heuristic."""
        payload = {**PAYLOAD, "messages": [{"role": "user", "content": "authored 中文"}]}
        encoded = json.dumps(
            payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
        result = estimate_request(payload)
        self.assertEqual(result["request_utf8_bytes"], len(encoded))
        self.assertEqual(result["conservative_input_tokens"], len(encoded) + 4096)
        expected = (Decimal(len(encoded) + 4096) * 2 + Decimal(32) * 8) / 1_000_000
        self.assertEqual(Decimal(result["reservation_peak_yuan"]), expected)
        self.assertIn("heuristic", result["estimate_method"])
        self.assertIn("not a provider-guaranteed", result["reservation_policy"])
        for maximum in [True, 0, -1, 4097, None]:
            with self.subTest(maximum=maximum), self.assertRaises(ValueError):
                estimate_request({**PAYLOAD, "max_tokens": maximum})
        with self.assertRaises(ValueError):
            estimate_request({**PAYLOAD, "model": "unpriced-model"})


class LedgerTests(unittest.TestCase):
    """Verify restart safety, cap decisions and privacy in temporary authored ledgers."""

    def setUp(self) -> None:
        """Allocate an isolated ledger so no test touches project or formal data."""
        self.temporary = TemporaryDirectory(prefix="textlab-budget-test-")
        self.path = Path(self.temporary.name) / "accounting.json"

    def tearDown(self) -> None:
        """Remove only the temporary fixture after each independent accounting scenario."""
        self.temporary.cleanup()

    def test_invalid_caps_and_configuration_mismatch_are_rejected(self) -> None:
        """Prevent a restart from changing the spending limit or evaluation identity."""
        for cap in ["0", "-1", "1.0000001", "NaN", "Infinity"]:
            with self.subTest(cap=cap), self.assertRaises(ValueError):
                Ledger(self.path, Decimal(cap), IDENTITY)
        Ledger(self.path, Decimal("1"), IDENTITY)
        with self.assertRaises(ValueError):
            Ledger(self.path, Decimal("0.5"), IDENTITY)
        with self.assertRaises(ValueError):
            Ledger(self.path, Decimal("1"), OTHER_IDENTITY)

    def test_four_yuan_requires_explicit_authority_and_persists_it(self) -> None:
        """Keep one-yuan validation callers unchanged while allowing the authorized external run."""
        with self.assertRaises(ValueError):
            Ledger(self.path, Decimal("4"), IDENTITY)
        self.assertFalse(self.path.exists())
        ledger = Ledger(self.path, Decimal("4"), IDENTITY, authorized_max_cap_yuan=Decimal("4"))
        persisted = json.loads(self.path.read_text())
        self.assertEqual(persisted["authorized_max_cap_yuan"], "4")
        self.assertEqual(ledger.summary()["authorized_max_cap_yuan"], "4")
        reloaded = Ledger(self.path, Decimal("4"), IDENTITY, authorized_max_cap_yuan=Decimal("4"))
        self.assertEqual(reloaded.summary(), ledger.summary())
        with self.assertRaises(ValueError):
            Ledger(self.path, Decimal("4"), IDENTITY)
        for maximum in ["0", "-1", "4.0000001", "NaN", "Infinity"]:
            other = self.path.parent / "invalid-authority.json"
            with self.subTest(maximum=maximum), self.assertRaises(ValueError):
                Ledger(
                    other,
                    Decimal("1"),
                    IDENTITY,
                    authorized_max_cap_yuan=Decimal(maximum),
                )
        self.assertFalse(other.exists())

    def test_restart_cannot_raise_authority_even_when_the_cap_is_unchanged(self) -> None:
        """Require authority to match persisted state instead of silently widening old ledgers."""
        Ledger(self.path, Decimal("1"), IDENTITY)
        with self.assertRaises(ValueError):
            Ledger(self.path, Decimal("1"), IDENTITY, authorized_max_cap_yuan=Decimal("4"))
        before = self.path.read_bytes()
        with self.assertRaises(ValueError):
            Ledger(self.path, Decimal("4"), IDENTITY, authorized_max_cap_yuan=Decimal("4"))
        self.assertEqual(self.path.read_bytes(), before)

    def test_legacy_ledgers_without_authority_remain_limited_to_one_yuan(self) -> None:
        """Keep old records readable under their original one-yuan authority."""
        ledger = Ledger(self.path, Decimal("1"), IDENTITY)
        ledger.reserve("sample-1", PAYLOAD)
        previous = json.loads(self.path.read_text())
        previous.pop("authorized_max_cap_yuan")
        previous["summary"].pop("authorized_max_cap_yuan")
        self.path.write_text(json.dumps(previous))
        reloaded = Ledger(self.path, Decimal("1"), IDENTITY)
        self.assertEqual(reloaded.records, ledger.records)
        self.assertEqual(reloaded.summary()["authorized_max_cap_yuan"], "1")
        with self.assertRaises(ValueError):
            Ledger(self.path, Decimal("1"), IDENTITY, authorized_max_cap_yuan=Decimal("4"))

    def test_explicit_four_yuan_cap_still_reserves_before_refusing_excess(self) -> None:
        """Exercise a real reservation above one yuan without issuing any model requests."""
        payload = {
            **PAYLOAD,
            "messages": [{"role": "user", "content": "x" * 600_000}],
        }
        ledger = Ledger(self.path, Decimal("4"), IDENTITY, authorized_max_cap_yuan=Decimal("4"))
        self.assertTrue(ledger.reserve("first", payload))
        self.assertGreater(Decimal(ledger.summary()["cap_exposure_yuan"]), Decimal("1"))
        self.assertTrue(ledger.reserve("second", payload))
        self.assertTrue(ledger.reserve("third", payload))
        self.assertFalse(ledger.reserve("fourth", payload))
        self.assertLessEqual(Decimal(ledger.summary()["cap_exposure_yuan"]), Decimal("4"))
        self.assertNotIn("fourth", ledger.records)

    def test_exact_cap_is_allowed_and_excess_is_not_dispatched(self) -> None:
        """Permit equality exactly while recording nothing for a request refused before dispatch."""
        reserve = Decimal(estimate_request(PAYLOAD)["reservation_peak_yuan"])
        ledger = Ledger(self.path, reserve, IDENTITY)
        self.assertTrue(ledger.reserve("sample-1", PAYLOAD))
        self.assertFalse(ledger.reserve("sample-2", PAYLOAD))
        self.assertEqual(ledger.summary()["cap_exposure_yuan"], str(reserve))
        self.assertEqual(ledger.summary()["attempt_count"], 1)
        self.assertNotIn("sample-2", ledger.records)
        other = Path(self.temporary.name) / "slightly-too-small.json"
        small = Ledger(other, reserve - Decimal("0.000000001"), IDENTITY)
        self.assertFalse(small.reserve("sample-1", PAYLOAD))
        self.assertEqual(small.summary()["attempt_count"], 0)

    def test_confirmed_usage_releases_unused_reservation_and_prices_failures(self) -> None:
        """Account known token usage even when the provider failed to return a valid label."""
        ledger = Ledger(self.path, Decimal("1"), IDENTITY)
        ledger.reserve("sample-1", PAYLOAD)
        ledger.records["sample-1"]["started_at_utc"] = "2026-11-01T01:00:00+00:00"
        record = ledger.finish("sample-1", {**good_result(), "label": {"invalid": True}})
        self.assertTrue(record["failure"])
        self.assertEqual(record["status"], "unknown")
        self.assertEqual(record["cost_yuan"], "0.0000336")
        self.assertEqual(record["conservative_peak_yuan"], "0.0000672")
        self.assertEqual(ledger.summary()["unknown_reserved_yuan"], "0")
        self.assertEqual(ledger.summary()["cap_exposure_yuan"], "0.0000672")
        reloaded = Ledger(self.path, Decimal("1"), IDENTITY)
        self.assertEqual(reloaded.records, ledger.records)
        self.assertEqual(reloaded.summary()["known_usage_count"], 1)

    def test_missing_or_invalid_usage_keeps_the_reservation(self) -> None:
        """Keep the full charge exposure for unknown outcomes instead of inventing a free call."""
        ledger = Ledger(self.path, Decimal("1"), IDENTITY)
        ledger.reserve("sample-1", PAYLOAD)
        reserved = ledger.records["sample-1"]["reserved_yuan"]
        record = ledger.finish("sample-1", {**good_result(), "usage": {"total_tokens": 0}})
        self.assertTrue(record["failure"])
        self.assertIsNone(record["cost_yuan"])
        self.assertEqual(ledger.summary()["unknown_reserved_yuan"], reserved)
        with self.assertRaises(ValueError):
            ledger.reserve("sample-1", PAYLOAD)
        with self.assertRaises(ValueError):
            ledger.finish("sample-1", good_result())

    def test_recovery_never_redispatches_a_pending_sample(self) -> None:
        """Recover durable pre-dispatch state as unknown so process interruption cannot retry."""
        ledger = Ledger(self.path, Decimal("1"), IDENTITY)
        ledger.reserve("sample-1", PAYLOAD)
        expected = ledger.summary()["cap_exposure_yuan"]
        recovered = Ledger(self.path, Decimal("1"), IDENTITY)
        recovered.recover_interrupted()
        self.assertEqual(recovered.records["sample-1"]["status"], "unknown")
        self.assertTrue(recovered.records["sample-1"]["failure"])
        self.assertEqual(recovered.summary()["cap_exposure_yuan"], expected)
        with self.assertRaises(ValueError):
            recovered.reserve("sample-1", PAYLOAD)
        with self.assertRaises(ValueError):
            recovered.finish("sample-1", good_result())
        persisted = json.loads(self.path.read_text())
        self.assertEqual(persisted["records"]["sample-1"]["error_code"], "interrupted_no_retry")

    def test_privacy_permissions_and_sanitized_result_fields(self) -> None:
        """Keep only typed output and hashes so SMS, provider bodies and keys stay private."""
        ledger = Ledger(self.path, Decimal("1"), IDENTITY)
        ledger.reserve("sample-1", PAYLOAD)
        secret = "authored-not-a-real-key-SECRET"
        record = ledger.finish(
            "sample-1",
            {
                **good_result(),
                "response_id": "synthetic-response-id",
                "http_status": 200,
                "content": AUTHORED_TEXT,
                "raw_response": {"secret": secret},
                "api_key": secret,
                "error": "Unbounded provider body " + secret,
            },
        )
        encoded = self.path.read_text()
        self.assertNotIn(AUTHORED_TEXT, encoded)
        self.assertNotIn(secret, encoded)
        self.assertNotIn("messages", encoded)
        self.assertEqual(record["elapsed_ms"], 12.5)
        self.assertEqual(record["provider_id"], "synthetic-response-1")
        self.assertEqual(record["request_id"], "synthetic-request-1")
        self.assertEqual(record["response_id"], "synthetic-response-id")
        self.assertEqual(record["http_status"], 200)
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        self.assertEqual(list(self.path.parent.glob(".comparison-budget-*")), [])

    def test_reservation_overrun_is_durable_and_stops_new_dispatches(self) -> None:
        """Retain unexpected actual usage before raising so a restart cannot hide the overrun."""
        ledger = Ledger(self.path, Decimal("1"), IDENTITY)
        ledger.reserve("sample-1", PAYLOAD)
        excessive = {
            "prompt_tokens": 6000,
            "completion_tokens": 0,
            "total_tokens": 6000,
            "prompt_cache_hit_tokens": 0,
        }
        with self.assertRaises(ReservationExceeded):
            ledger.finish("sample-1", {**good_result(), "usage": excessive})
        reloaded = Ledger(self.path, Decimal("1"), IDENTITY)
        self.assertTrue(reloaded.summary()["stopped"])
        self.assertEqual(reloaded.summary()["conservative_peak_yuan"], "0.012")
        self.assertTrue(reloaded.records["sample-1"]["reservation_exceeded"])
        with self.assertRaises(BudgetStopped):
            reloaded.reserve("sample-2", PAYLOAD)


if __name__ == "__main__":
    unittest.main()
