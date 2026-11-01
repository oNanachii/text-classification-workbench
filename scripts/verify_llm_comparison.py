#!/usr/bin/env python3
"""Independently verify text-free comparison artifacts using only the Python standard library."""

from __future__ import annotations

import argparse
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal, InvalidOperation, localcontext
import hashlib
import json
import math
from pathlib import Path
import re
import statistics
from typing import Any
from zoneinfo import ZoneInfo

PROJECT = Path(__file__).resolve().parents[1]
DEFAULT_ROOT = PROJECT / "artifacts/llm-comparison"
VALIDATION_COUNT = 783
LOCAL_THRESHOLD = 0.5
MAX_OUTPUT_TOKENS = 32
RESERVATION_FRAMING_TOKENS = 4096
EXPECTED_MODEL = "deepseek-flash"
EXPECTED_ENDPOINT = "https://api.deepseek.com/chat/completions"
EXPECTED_BANDS = ((0.45, 0.55), (0.4, 0.6), (0.3, 0.7), (0.2, 0.8), (0.1, 0.9), (0.0, 1.0))
FROZEN_CONFIG_KEYS = (
    "schema_version",
    "validation_count",
    "validation_sha256",
    "dataset_id",
    "class_counts",
    "local_model",
    "bands",
    "concurrency",
    "timeout_seconds",
    "local_warm_up_count",
    "local_latency_scope",
    "deepseek",
    "price_schedule",
    "source_sha256",
    "jev_status",
)
INPUT_FILES = ("plan.json", "results.jsonl", "ledger.json", "budget.json", "summary.json")
OUTPUT_FILE = "verification.json"
MILLION = Decimal(1_000_000)
MAX_CAP_YUAN = Decimal("1")
PER_THOUSAND = Decimal(1000)
DECIMAL_PRECISION = 28
FLOAT_TOLERANCE = 1e-12
PEAK_RATES = {"cache_hit": Decimal("0.04"), "cache_miss": Decimal("2"), "output": Decimal("8")}
OFFPEAK_RATES = {"cache_hit": Decimal("0.02"), "cache_miss": Decimal("1"), "output": Decimal("4")}
USAGE_FIELDS = (
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "prompt_cache_hit_tokens",
    "prompt_cache_miss_tokens",
)
SHA256 = re.compile(r"[a-f0-9]{64}\Z")
CREDENTIAL_PATTERN = re.compile(r"(?:\bsk-[A-Za-z0-9_-]{8,}|Bearer\s+\S+)")
FORBIDDEN_KEYS = {
    "text",
    "raw_text",
    "sms",
    "messages",
    "content",
    "payload",
    "request_body",
    "response_body",
    "authorization",
    "api_key",
    "deepseek_api_key",
    "typesafe_api_key",
    "headers",
    "choices",
    "reasoning_content",
    "password",
    "secret",
}
ROW_KEYS = {"sample_id", "truth", "local", "deepseek"}
LOCAL_KEYS = {"status", "label", "spam_score", "elapsed_ms", "failure"}
EXTERNAL_KEYS = {
    "sample_id",
    "request_sha256",
    "estimate",
    "started_at_utc",
    "finished_at_utc",
    "status",
    "label",
    "failure",
    "reserved_yuan",
    "cost_yuan",
    "conservative_peak_yuan",
    "usage",
    "requested_model",
    "model",
    "provider_id",
    "request_id",
    "response_id",
    "system_fingerprint",
    "error_code",
    "finish_reason",
    "elapsed_ms",
    "http_status",
    "tariff_rates_yuan_per_million",
    "reservation_exceeded",
}

Json = dict[str, Any]
Record = Mapping[str, Any]


class VerificationFailure(ValueError):
    """Indicate a mismatched evidence invariant without printing message-bearing input values."""


class Checks:
    """Collect named independent checks so a passing result identifies what was actually tested."""

    def __init__(self) -> None:
        """Initialize a private result list without accessing project data or credentials."""
        self.passed: list[str] = []

    def require(self, condition: bool, name: str) -> None:
        """Record one invariant or stop before treating inconsistent evidence as verified."""
        if not condition:
            raise VerificationFailure(name)
        self.passed.append(name)

    def equal(self, observed: Any, expected: Any, name: str) -> None:
        """Compare evidence values, allowing only finite floating-point rounding differences."""
        if isinstance(expected, float):
            valid = (
                isinstance(observed, (int, float))
                and not isinstance(observed, bool)
                and math.isfinite(observed)
                and math.isclose(
                    observed, expected, rel_tol=FLOAT_TOLERANCE, abs_tol=FLOAT_TOLERANCE
                )
            )
        else:
            valid = observed == expected
        self.require(valid, name)


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _unique_object(pairs: list[tuple[str, Any]]) -> Json:
    result: Json = {}
    for key, value in pairs:
        if key in result:
            raise VerificationFailure("JSON evidence contains a duplicate object key")
        result[key] = value
    return result


def _invalid_constant(_value: str) -> None:
    raise VerificationFailure("JSON evidence contains a nonfinite numeric token")


def _read_json(content: bytes) -> Any:
    return json.loads(content, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)


def _amount(value: Any) -> Decimal:
    if not isinstance(value, str):
        raise VerificationFailure("Money evidence must use decimal strings")
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise VerificationFailure("Invalid decimal money evidence") from exc
    if not result.is_finite() or result < 0:
        raise VerificationFailure("Money evidence must be finite and nonnegative")
    return result


def _no_private_payload(value: Any) -> None:
    if isinstance(value, dict):
        if any(key.casefold() in FORBIDDEN_KEYS for key in value):
            raise VerificationFailure("Evidence contains a raw-text or credential-bearing field")
        for child in value.values():
            _no_private_payload(child)
    elif isinstance(value, list):
        for child in value:
            _no_private_payload(child)
    elif isinstance(value, str) and CREDENTIAL_PATTERN.search(value):
        raise VerificationFailure("Evidence contains a credential-shaped value")


def _rate_at(started_iso: str) -> dict[str, Decimal]:
    started = datetime.fromisoformat(started_iso)
    if started.tzinfo is None:
        raise VerificationFailure("Dispatch time lacks a timezone")
    local = started.astimezone(ZoneInfo("Asia/Shanghai"))
    peak = local.weekday() < 5 and (9 <= local.hour < 12 or 14 <= local.hour < 18)
    return PEAK_RATES if peak else OFFPEAK_RATES


def _usage_cost(usage: Record, rates: Record) -> Decimal:
    if set(usage) != set(USAGE_FIELDS):
        raise VerificationFailure("Known usage must contain all five token counters")
    if any(type(usage[field]) is not int or usage[field] < 0 for field in USAGE_FIELDS):
        raise VerificationFailure("Token counters must be nonnegative integers")
    prompt = usage["prompt_tokens"]
    hit = usage["prompt_cache_hit_tokens"]
    miss = usage["prompt_cache_miss_tokens"]
    completion = usage["completion_tokens"]
    if prompt != hit + miss or usage["total_tokens"] != prompt + completion:
        raise VerificationFailure("Token counters do not reconcile")
    return (
        hit * rates["cache_hit"] + miss * rates["cache_miss"] + completion * rates["output"]
    ) / MILLION


def _metric(truths: Sequence[str], outputs: Sequence[Record], conservative: bool) -> Json:
    labels = ("ham", "spam")
    valid = [index for index, output in enumerate(outputs) if output["status"] == "ok"]
    denominator = len(truths) if conservative else len(valid)
    per_class: Json = {}
    abstentions = {label: 0 for label in labels}
    for label in labels:
        true_positive = sum(
            truths[index] == label and outputs[index]["label"] == label for index in valid
        )
        false_positive = sum(
            truths[index] != label and outputs[index]["label"] == label for index in valid
        )
        false_negative = sum(
            truths[index] == label and outputs[index]["label"] != label for index in valid
        )
        abstentions[label] = sum(
            truth == label and output["status"] != "ok"
            for truth, output in zip(truths, outputs, strict=True)
        )
        if conservative:
            false_negative += abstentions[label]
        f1_denominator = 2 * true_positive + false_positive + false_negative
        per_class[label] = {
            "tp": true_positive,
            "fp": false_positive,
            "fn": false_negative,
            "f1": 2 * true_positive / f1_denominator if f1_denominator else 0.0,
            "precision": (
                true_positive / (true_positive + false_positive)
                if true_positive + false_positive
                else None
            ),
            "recall": (
                true_positive / (true_positive + false_negative)
                if true_positive + false_negative
                else None
            ),
        }
    spam = per_class["spam"]
    ham = per_class["ham"]
    # Binary confusion contains only returned predictions, never invented abstention labels.
    confusion = {
        "true_positive": spam["tp"],
        "true_negative": ham["tp"],
        "false_positive": spam["fp"],
        "false_negative": ham["fp"],
    }
    return {
        "denominator": denominator,
        "macro_f1": (spam["f1"] + ham["f1"]) / 2 if denominator else None,
        "spam_precision": spam["precision"],
        "spam_recall": spam["recall"],
        "ham_precision": ham["precision"],
        "ham_recall": ham["recall"],
        "accuracy": (spam["tp"] + ham["tp"]) / denominator if denominator else None,
        "false_positive_ham_count": spam["fp"],
        "confusion": confusion,
        "abstention_count_by_truth": abstentions,
        "class_false_negative_counts": {"spam": spam["fn"], "ham": ham["fn"]},
    }


def _percentile(values: Sequence[float], probability: float) -> float | None:
    if not values:
        return None
    if probability == 0.5:
        return statistics.median(values)
    ordered = sorted(values)
    lower, remainder = divmod((len(ordered) - 1) * probability, 1)
    index = int(lower)
    return ordered[index] * (1 - remainder) + ordered[min(index + 1, len(ordered) - 1)] * remainder


def _latency(outputs: Sequence[Record], completed: bool) -> Json:
    selected = [
        output
        for output in outputs
        if output["status"] == "ok" or (not completed and output["status"] != "not_attempted")
    ]
    values = [output["elapsed_ms"] for output in selected if output.get("elapsed_ms") is not None]
    return {
        "status_denominator": len(selected),
        "measurement_count": len(values),
        "missing_measurement_count": len(selected) - len(values),
        "p50": _percentile(values, 0.5),
        "p95": _percentile(values, 0.95),
    }


def _money_summary(records: Sequence[Record], denominator: int) -> Json:
    attempted = [record for record in records if record["status"] != "not_attempted"]
    known = sum(
        (
            _amount(record["cost_yuan"])
            for record in attempted
            if record.get("cost_yuan") is not None
        ),
        Decimal(0),
    )
    unknown = [record for record in attempted if record.get("cost_yuan") is None]
    reserve = sum((_amount(record.get("reserved_yuan", "0")) for record in unknown), Decimal(0))
    with localcontext() as context:
        context.prec = DECIMAL_PRECISION
        per_thousand = known * PER_THOUSAND / denominator
    return {
        "known_cost_yuan": known,
        "known_cost_yuan_per_1000_requested_rows": per_thousand,
        "requested_rows_denominator": denominator,
        "priced_attempt_count": len(attempted) - len(unknown),
        "returned_usage_count": sum(record.get("usage") is not None for record in attempted),
        "unpriced_attempt_count": len(unknown),
        "unknown_reserved_yuan": reserve,
        "known_plus_unknown_reservation_yuan": known + reserve,
    }


def _compare_fields(checks: Checks, observed: Record, expected: Record, prefix: str) -> None:
    for key, value in expected.items():
        if isinstance(value, dict):
            checks.require(isinstance(observed.get(key), dict), f"{prefix}.{key} object exists")
            _compare_fields(checks, observed[key], value, f"{prefix}.{key}")
        elif isinstance(value, Decimal):
            checks.equal(_amount(observed.get(key)), value, f"{prefix}.{key}")
        else:
            checks.equal(observed.get(key), value, f"{prefix}.{key}")


def _verify_strategy(
    checks: Checks,
    truths: Sequence[str],
    predictions: Sequence[Record],
    costs: Sequence[Record],
    observed: Record,
    name: str,
) -> Json:
    completed = sum(output["status"] == "ok" for output in predictions)
    attempted = sum(output["status"] != "not_attempted" for output in predictions)
    failure = sum(bool(output.get("failure", False)) for output in predictions)
    unknown = sum(output["status"] == "unknown" for output in predictions)
    n = len(truths)
    counts = {
        "requested_count": n,
        "attempted_count": attempted,
        "completed_count": completed,
        "unknown_count": unknown,
        "not_attempted_count": n - attempted,
        "failure_count": failure,
        "coverage": completed / n,
        "attempt_fraction": attempted / n,
        "failure_fraction_requested": failure / n,
        "failure_fraction_attempted": failure / attempted if attempted else None,
        "unknown_fraction_requested": unknown / n,
        "not_attempted_fraction_requested": (n - attempted) / n,
    }
    _compare_fields(checks, observed, counts, name)
    complete_metrics = _metric(truths, predictions, False)
    conservative_metrics = _metric(truths, predictions, True)
    _compare_fields(
        checks, observed["completed_only_metrics"], complete_metrics, f"{name}.complete"
    )
    _compare_fields(
        checks,
        observed["full_denominator_conservative_metrics"],
        conservative_metrics,
        f"{name}.conservative",
    )
    checks.equal(
        observed["latency_ms"]["requested_rows_denominator"], n, f"{name}.latency denominator"
    )
    for scope, completed_only in (("completed", True), ("attempted", False)):
        _compare_fields(
            checks,
            observed["latency_ms"][scope],
            _latency(predictions, completed_only),
            f"{name}.latency.{scope}",
        )
    cost = _money_summary(costs, n)
    _compare_fields(checks, observed["cost"], cost, f"{name}.cost")
    return {"metrics": conservative_metrics, "completed_metrics": complete_metrics, "cost": cost}


def _verify_accounting(checks: Checks, records: Record, budget: Record) -> Json:
    known_cost = Decimal(0)
    known_peak = Decimal(0)
    unknown_reserved = Decimal(0)
    known_count = 0
    usage_totals: Counter[str] = Counter()
    request_hashes = set()
    provider_ids = set()
    events: list[tuple[datetime, int, Decimal]] = []
    for identity, record in records.items():
        checks.equal(
            record.get("sample_id"), identity, "Ledger key equals dispatch sample identity"
        )
        checks.require(set(record) <= EXTERNAL_KEYS, "Ledger record contains only allowed metadata")
        request_hash = record.get("request_sha256")
        checks.require(
            isinstance(request_hash, str) and SHA256.fullmatch(request_hash) is not None,
            "Dispatch request digest is SHA-256",
        )
        checks.require(request_hash not in request_hashes, "No duplicate request digests")
        request_hashes.add(request_hash)
        checks.require(
            record["status"] in {"ok", "unknown"}, "Every dispatch has a terminal status"
        )
        if record.get("requested_model") is not None:
            checks.equal(
                record["requested_model"], EXPECTED_MODEL, "Requested model metadata matches plan"
            )
        if record["status"] == "ok":
            checks.equal(
                record.get("model"), EXPECTED_MODEL, "Successful response model matches plan"
            )
            checks.require(
                record.get("label") in {"ham", "spam"}, "Successful result has a valid label"
            )
        else:
            checks.equal(record.get("label"), None, "Unknown result has no invented label")
        provider_id = record.get("provider_id")
        if provider_id is not None:
            checks.require(
                provider_id not in provider_ids, "No duplicate returned provider response IDs"
            )
            provider_ids.add(provider_id)
        reserved = _amount(record["reserved_yuan"])
        estimate = record["estimate"]
        checks.equal(
            _amount(estimate["reservation_peak_yuan"]), reserved, "Reservation matches estimate"
        )
        checks.equal(
            estimate["max_output_tokens"],
            MAX_OUTPUT_TOKENS,
            "Reservation uses frozen output token limit",
        )
        byte_count = estimate["request_utf8_bytes"]
        checks.require(type(byte_count) is int and byte_count > 0, "Positive request-byte estimate")
        bound = byte_count + RESERVATION_FRAMING_TOKENS
        checks.equal(estimate["conservative_input_tokens"], bound, "Reservation input allowance")
        expected_reserve = (
            Decimal(bound) * PEAK_RATES["cache_miss"] + MAX_OUTPUT_TOKENS * PEAK_RATES["output"]
        ) / MILLION
        checks.equal(reserved, expected_reserve, "Independent pre-dispatch reservation arithmetic")
        started = datetime.fromisoformat(record["started_at_utc"])
        checks.require(started.tzinfo is not None, "Dispatch timestamp is timezone-aware")
        events.append((started, 0, reserved))
        usage = record.get("usage")
        if usage is None:
            checks.equal(record.get("cost_yuan"), None, "Unknown usage has no invented charge")
            checks.equal(
                record.get("conservative_peak_yuan"), None, "Unknown usage retains reservation"
            )
            unknown_reserved += reserved
            continue
        known_count += 1
        rates = _rate_at(record["started_at_utc"])
        checks.equal(
            {key: _amount(value) for key, value in record["tariff_rates_yuan_per_million"].items()},
            rates,
            "Per-request tariff matches Beijing dispatch time",
        )
        cost = _usage_cost(usage, rates)
        peak_cost = _usage_cost(usage, PEAK_RATES)
        checks.equal(
            _amount(record["cost_yuan"]), cost, "Per-request returned-usage tariff arithmetic"
        )
        checks.equal(
            _amount(record["conservative_peak_yuan"]), peak_cost, "Per-request peak arithmetic"
        )
        checks.require(peak_cost <= reserved, "Known peak usage stayed within dispatch reservation")
        checks.require(not record.get("reservation_exceeded", False), "No reservation overflow")
        finished = datetime.fromisoformat(record["finished_at_utc"])
        checks.require(
            finished.tzinfo is not None and finished >= started, "Completion follows dispatch"
        )
        events.append((finished, 1, peak_cost - reserved))
        known_cost += cost
        known_peak += peak_cost
        usage_totals.update(usage)
    cap = _amount(budget["cap_yuan"])
    checks.require(0 < cap <= MAX_CAP_YUAN, "Authorized cap does not exceed one yuan")
    exposure = Decimal(0)
    maximum_exposure = Decimal(0)
    for _time, _kind, change in sorted(events):
        exposure += change
        maximum_exposure = max(maximum_exposure, exposure)
        checks.require(
            0 <= exposure <= cap, "Chronological reservation exposure remains within cap"
        )
    expected = {
        "known_cost_yuan": known_cost,
        "conservative_peak_yuan": known_peak,
        "unknown_reserved_yuan": unknown_reserved,
        "cap_exposure_yuan": known_peak + unknown_reserved,
        "cap_yuan": cap,
        "attempt_count": len(records),
        "known_usage_count": known_count,
        "unknown_usage_count": len(records) - known_count,
    }
    _compare_fields(checks, budget, expected, "budget")
    checks.equal(
        exposure, known_peak + unknown_reserved, "Final chronological exposure matches budget"
    )
    for usage_name in ("usage", "usage_totals"):
        if usage_name in budget:
            checks.equal(budget[usage_name], dict(usage_totals), "Budget token totals reconcile")
    return {
        **{
            key: str(value) if isinstance(value, Decimal) else value
            for key, value in expected.items()
        },
        "maximum_chronological_exposure_yuan": str(maximum_exposure),
        "five_field_usage_totals": dict(usage_totals),
        "unique_request_digest_count": len(request_hashes),
        "unique_provider_response_id_count": len(provider_ids),
    }


def verify(root: Path) -> Json:
    """Recompute reported strategies using only the exported, text-free comparison evidence."""
    root = root.resolve()
    formal = (PROJECT / "var").resolve()
    if root == formal or root.is_relative_to(formal):
        raise VerificationFailure("Verification must not read the formal data workspace")
    paths = {name: root / name for name in INPUT_FILES}
    if any(path.is_symlink() or not path.is_file() for path in paths.values()):
        raise VerificationFailure(
            "All five exported evidence files must exist and be regular files"
        )
    content = {name: path.read_bytes() for name, path in paths.items()}
    objects = {
        name: _read_json(value) for name, value in content.items() if name != "results.jsonl"
    }
    rows = [_read_json(line) for line in content["results.jsonl"].splitlines() if line.strip()]
    for value in (*objects.values(), rows):
        _no_private_payload(value)
    plan = objects["plan.json"]
    ledger = objects["ledger.json"]
    budget = objects["budget.json"]
    summary = objects["summary.json"]
    checks = Checks()
    checks.require(len(rows) == VALIDATION_COUNT, "Exactly 783 requested result rows")
    checks.equal(
        plan["validation_count"], VALIDATION_COUNT, "Plan cohort is the 783-row validation set"
    )
    checks.equal(summary["requested_rows"], VALIDATION_COUNT, "Summary full denominator is 783")
    checks.equal(plan["local_model"]["kind"], "mlp16", "Frozen local model is MLP16")
    checks.equal(plan["local_model"]["threshold"], LOCAL_THRESHOLD, "Local threshold stays at 0.5")
    deepseek = plan["deepseek"]
    checks.equal(deepseek["model"], EXPECTED_MODEL, "Plan requests deepseek-flash")
    checks.equal(deepseek["endpoint"], EXPECTED_ENDPOINT, "Plan uses the fixed official endpoint")
    parameters = deepseek["payload_parameters"]
    checks.equal(
        parameters,
        {
            "model": EXPECTED_MODEL,
            "temperature": 0,
            "thinking": {"type": "disabled"},
            "response_format": {"type": "json_object"},
            "max_tokens": MAX_OUTPUT_TOKENS,
            "stream": False,
        },
        "Plan payload parameters are the frozen deterministic label-only configuration",
    )
    checks.require(
        type(parameters["temperature"]) is int, "Temperature zero is numeric, not boolean"
    )
    checks.equal(
        deepseek["prompt_sha256"],
        _sha(deepseek["system_prompt"].encode("utf-8")),
        "System prompt digest is independently reproducible",
    )
    identity = _sha(_canonical({key: plan[key] for key in FROZEN_CONFIG_KEYS}))
    checks.equal(
        plan["identity_sha256"], identity, "Frozen plan identity is independently reproducible"
    )
    checks.equal(ledger["identity_sha256"], identity, "Ledger configuration identity matches plan")
    checks.equal(summary["configuration_sha256"], identity, "Summary configuration matches plan")
    checks.equal(
        summary["validation_sha256"], plan["validation_sha256"], "Validation digest agreement"
    )
    checks.equal(
        plan["bands"], [list(band) for band in EXPECTED_BANDS], "All six bands were declared"
    )
    for name, rates in (("peak", PEAK_RATES), ("offpeak", OFFPEAK_RATES)):
        checks.equal(
            {key: _amount(value) for key, value in plan["price_schedule"][name].items()},
            rates,
            f"Independently fixed {name} pricing schedule",
        )
    identities = set()
    truth_counts: Counter[str] = Counter()
    records = ledger["records"]
    for row in rows:
        checks.equal(
            set(row), ROW_KEYS, "Result row contains only ID, truth and safe model outputs"
        )
        identity = row["sample_id"]
        checks.require(
            isinstance(identity, str) and SHA256.fullmatch(identity) is not None,
            "Sample identity is a SHA-256 digest",
        )
        checks.require(identity not in identities, "No duplicate sample IDs")
        identities.add(identity)
        checks.require(row["truth"] in {"ham", "spam"}, "Truth label is valid")
        truth_counts[row["truth"]] += 1
        local = row["local"]
        remote = row["deepseek"]
        checks.require(
            set(local) <= LOCAL_KEYS, "Local output contains no raw text or hidden payload"
        )
        checks.require(
            set(remote) <= EXTERNAL_KEYS, "External output contains only allowed metadata"
        )
        checks.equal(local["status"], "ok", "Every local prediction completed")
        score = local["spam_score"]
        checks.require(
            isinstance(score, (int, float))
            and not isinstance(score, bool)
            and math.isfinite(score)
            and 0 <= score <= 1,
            "Local score is a finite probability",
        )
        checks.equal(
            local["label"],
            "spam" if score >= LOCAL_THRESHOLD else "ham",
            "Local fixed-threshold label",
        )
        for output in (local, remote):
            elapsed = output.get("elapsed_ms")
            checks.require(
                elapsed is None
                or (
                    isinstance(elapsed, (int, float))
                    and not isinstance(elapsed, bool)
                    and math.isfinite(elapsed)
                    and elapsed >= 0
                ),
                "Recorded latency is finite and nonnegative or explicitly missing",
            )
        if identity in records:
            checks.equal(
                remote, records[identity], "Per-ID ledger/result request hashes and outputs match"
            )
        else:
            checks.equal(
                remote["status"], "not_attempted", "Missing dispatch is explicitly not attempted"
            )
            checks.equal(
                remote.get("label"), None, "Unattempted sample has no invented external label"
            )
            checks.equal(remote.get("cost_yuan"), None, "Unattempted sample has no external charge")
    checks.equal(dict(truth_counts), plan["class_counts"], "Result class counts match frozen plan")
    checks.require(set(records) <= identities, "Every dispatch belongs to the requested cohort")
    checks.equal(summary["budget"], budget, "Published budget and summary budget agree")
    integrity = budget["data_integrity"]
    checks.equal(
        integrity["heldout_test_files_opened"], 0, "Runtime evidence records no held-out reads"
    )
    checks.equal(
        integrity["blocked_formal_accesses"], 0, "Runtime formal-data allowlist was respected"
    )
    checks.equal(
        integrity["protected_files_unchanged"], True, "Runtime protected-input hashes stayed equal"
    )
    accounting = _verify_accounting(checks, records, budget)
    truths = [row["truth"] for row in rows]
    local_outputs = [row["local"] for row in rows]
    external_outputs = [row["deepseek"] for row in rows]
    recomputed = {
        "local": _verify_strategy(checks, truths, local_outputs, (), summary["local"], "local"),
        "deepseek": _verify_strategy(
            checks, truths, external_outputs, external_outputs, summary["deepseek"], "deepseek"
        ),
    }
    checks.equal(len(summary["cascades"]), len(EXPECTED_BANDS), "Exactly six cascade summaries")
    cascade_results = []
    for observed, (low, high) in zip(summary["cascades"], EXPECTED_BANDS, strict=True):
        predicted = []
        upgraded = []
        membership = []
        fallback = 0
        for row in rows:
            local = row["local"]
            remote = row["deepseek"]
            output = dict(local)
            if low <= local["spam_score"] <= high:
                membership.append(row["sample_id"])
                upgraded.append(remote)
                if remote["status"] == "ok":
                    output = dict(remote)
                else:
                    fallback += 1
                times = (local.get("elapsed_ms"), remote.get("elapsed_ms"))
                output["elapsed_ms"] = (
                    sum(times)
                    if remote["status"] != "not_attempted"
                    and all(time is not None for time in times)
                    else None
                )
            predicted.append(output)
        strategy_id = f"cascade[{low:g},{high:g}]"
        checks.equal(
            observed["strategy_id"], strategy_id, "Cascade strategy identity matches frozen band"
        )
        checks.equal(observed["band"], [low, high], "Cascade uses inclusive declared endpoints")
        attempted = sum(output["status"] != "not_attempted" for output in upgraded)
        completed = sum(output["status"] == "ok" for output in upgraded)
        expected_counts = {
            "planned_escalation_count": len(upgraded),
            "planned_escalation_fraction": len(upgraded) / VALIDATION_COUNT,
            "escalation_attempted_count": attempted,
            "escalation_completed_count": completed,
            "escalation_unknown_count": sum(output["status"] == "unknown" for output in upgraded),
            "escalation_not_attempted_count": len(upgraded) - attempted,
            "escalation_failure_count": sum(
                bool(output.get("failure", False)) for output in upgraded
            ),
            "all_planned_escalations_completed": completed == len(upgraded),
            "fallback_count": fallback,
            "fallback_completed_count": fallback,
        }
        _compare_fields(checks, observed, expected_counts, strategy_id)
        result = _verify_strategy(checks, truths, predicted, upgraded, observed, strategy_id)
        eligible = completed == len(upgraded) and result["cost"]["unpriced_attempt_count"] == 0
        checks.equal(
            observed["eligible_for_recommendation"],
            eligible,
            "Cascade eligibility is complete and priced",
        )
        cascade_results.append(
            {
                "strategy_id": strategy_id,
                "band": [low, high],
                "planned_membership_sha256": _sha(_canonical(sorted(membership))),
                "planned_count": len(upgraded),
                "fallback_count": fallback,
                "eligible": eligible,
                **result,
            }
        )
    baseline = recomputed["local"]["metrics"]["macro_f1"]
    eligible_cascades = [item for item in cascade_results if item["eligible"]]
    improving = [item for item in eligible_cascades if item["metrics"]["macro_f1"] > baseline]

    def rank(item: Record) -> tuple[Decimal, float, float, float]:
        return (
            item["cost"]["known_cost_yuan"],
            -item["metrics"]["macro_f1"],
            item["band"][1] - item["band"][0],
            item["band"][0],
        )

    selected = min(improving, key=rank) if improving else None
    checks.equal(
        summary["recommendation"]["strategy_id"],
        selected["strategy_id"] if selected else "local",
        "Recommendation follows independent complete-data cost and strict-improvement ordering",
    )
    checks.equal(
        summary["recommendation"]["band"],
        selected["band"] if selected else None,
        "Recommended band",
    )
    nonzero = [item for item in eligible_cascades if item["planned_count"] > 0]
    best = (
        min(nonzero, key=lambda item: (-item["metrics"]["macro_f1"], *rank(item)))
        if nonzero
        else None
    )
    observed_best = summary["best_observed_nonzero_band"]
    checks.equal(
        observed_best["strategy_id"] if observed_best else None,
        best["strategy_id"] if best else None,
        "Best observed nonzero band is independently ranked",
    )
    return {
        "status": "passed",
        "verifier": "scripts/verify_llm_comparison.py; stdlib only, independent metric arithmetic",
        "requested_rows": VALIDATION_COUNT,
        "input_sha256": {name: _sha(value) for name, value in content.items()},
        "passed_check_count": len(checks.passed),
        "check_categories": sorted(set(checks.passed)),
        "accounting": accounting,
        "recomputed": recomputed,
        "cascades": cascade_results,
        "recommendation": selected["strategy_id"] if selected else "local",
        "limits": [
            "No formal var files, raw SMS, held-out tests or environment credentials were read.",
            "Request SHA-256 digests match ledger/results; bodies cannot be independently rehashed "
            "because SMS bodies are deliberately absent from the exported evidence.",
            "One durable dispatch per sample and unique response IDs are verified "
            "at evidence level; "
            "the exported records alone cannot prove absence of transport-level retries.",
            "Tariff estimates and reservation exposure are recomputed; "
            "provider billing statements are outside this audit.",
            "Cascade latency is cache replay arithmetic, not a newly measured cascade trial.",
        ],
    }


def _json_ready(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {key: _json_ready(child) for key, child in value.items()}
    if isinstance(value, list):
        return [_json_ready(child) for child in value]
    return value


def main() -> None:
    """Verify a completed exported run and write its independent audit beside the evidence files."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    args = parser.parse_args()
    result = verify(args.root)
    output = args.root.resolve() / OUTPUT_FILE
    if output.is_symlink():
        raise VerificationFailure("Verification output must not be a symlink")
    output.write_text(
        json.dumps(_json_ready(result), indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )
    print(json.dumps({"status": result["status"], "checks": result["passed_check_count"]}))


if __name__ == "__main__":
    main()
