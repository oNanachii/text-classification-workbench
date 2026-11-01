#!/usr/bin/env python3
"""Audit the preregistered external test with stdlib arithmetic and text-free evidence only."""

from __future__ import annotations

import argparse
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal
import importlib.util
import json
import math
from pathlib import Path
import re
import subprocess
from typing import Any

PROJECT = Path(__file__).resolve().parents[1]
ROOT = PROJECT / "artifacts/external-test"
PLAN_DOCUMENT = "docs/EXTERNAL_TEST_PLAN.md"
FROZEN_CONFIG = "artifacts/llm-comparison/frozen-config.json"
VALIDATION_SUMMARY = "artifacts/llm-comparison/summary.json"
EXTERNAL_COUNT = 733
RAW_COUNT = 5971
CLASS_COUNTS = {"ham": 410, "spam": 323}
RAW_LABEL_COUNTS = {"ham": 4844, "Smishing": 616, "smishing": 22, "spam": 466, "Spam": 23}
BAND = [0.2, 0.8]
AUTHORIZED_CAP = Decimal("4")
STUDY_ID = "mendeley-f45bkkt8pr-v1-external-20261101"
COMMIT_SHA = re.compile(r"[a-f0-9]{40}\Z")
INPUT_FILES = (
    "plan.json",
    "audit.json",
    "cohort.json",
    "results.jsonl",
    "ledger.json",
    "budget.json",
    "summary.json",
    "evaluation-attempt.json",
)
EXECUTION_SOURCES = {
    "textlab/external_test.py",
    "textlab/external_data.py",
    "textlab/comparison.py",
    "textlab/comparison_budget.py",
    "textlab/comparison_analysis.py",
    "textlab/llm_provider.py",
    "textlab/safe_model.py",
    "textlab/data.py",
    "textlab/dataflow.py",
    "textlab/decision.py",
}
SAFE_TEMPLATE = {"role": "user", "content": "<SMS_TEXT_ONLY>"}
CROSS_REASONS = {"normalized_exact", "nonempty_template", "blocked_char_gram_jaccard"}
RESERVED_REASONS = {"reserved_exact_identity", "reserved_nonempty_template_hash"}

Json = dict[str, Any]
Record = Mapping[str, Any]

# This module is an earlier independent stdlib verifier, never a production metric/provider module.
HELPER_PATH = Path(__file__).with_name("verify_llm_comparison.py")
HELPER_SPEC = importlib.util.spec_from_file_location("independent_sms_audit", HELPER_PATH)
if HELPER_SPEC is None or HELPER_SPEC.loader is None:
    raise RuntimeError("Independent verifier helpers are unavailable")
independent = importlib.util.module_from_spec(HELPER_SPEC)
HELPER_SPEC.loader.exec_module(independent)


def _require_path(path: Path) -> bytes:
    resolved = path.resolve()
    formal = (PROJECT / "var").resolve()
    if resolved.is_relative_to(formal) or path.is_symlink() or not path.is_file():
        raise independent.VerificationFailure("Only regular exported evidence files may be read")
    return path.read_bytes()


def _privacy(value: Any, path: tuple[str, ...] = ()) -> None:
    if isinstance(value, dict):
        if path[-2:] == ("deepseek", "user_message_template"):
            if value != SAFE_TEMPLATE:
                raise independent.VerificationFailure(
                    "User message template must contain only the marker"
                )
            return
        private_positions = {"source_row", "source_rows", "source_line", "source_lines"}
        if any(
            key.casefold() in independent.FORBIDDEN_KEYS or key.casefold() in private_positions
            for key in value
        ):
            raise independent.VerificationFailure("Raw-message or credential-bearing evidence key")
        for key, child in value.items():
            _privacy(child, (*path, key))
    elif isinstance(value, list):
        for child in value:
            _privacy(child, path)
    elif isinstance(value, str) and independent.CREDENTIAL_PATTERN.search(value):
        raise independent.VerificationFailure("Credential-shaped value in exported evidence")


def _digest_ids(identities: Sequence[str]) -> str:
    return independent._sha(independent._canonical(sorted(identities)))


def _git_bytes(commit: str, relative: str) -> bytes:
    allowed = EXECUTION_SOURCES | {
        PLAN_DOCUMENT,
        FROZEN_CONFIG,
        VALIDATION_SUMMARY,
        "artifacts/external-test/plan.json",
        "artifacts/external-test/audit.json",
        "artifacts/external-test/cohort.json",
    }
    if COMMIT_SHA.fullmatch(commit) is None or relative not in allowed:
        raise independent.VerificationFailure(
            "Git evidence path or commit is outside the allowlist"
        )
    return subprocess.check_output(["git", "show", f"{commit}:{relative}"], cwd=PROJECT)


def _audit_cleaning_counts(checks: Any, ingestion: Record, exclusions: Sequence[Record]) -> Json:
    counts = Counter(row["reason"] for row in exclusions)
    for reason, field in (
        ("malformed_record", "malformed_rows_excluded"),
        ("unsupported_label", "unsupported_label_rows_excluded"),
        ("empty_text", "empty_rows_excluded"),
        ("overlong_text", "overlong_rows_excluded"),
        ("ascii_letter_guard", "ascii_letter_guard_rows_excluded"),
    ):
        checks.equal(counts[reason], ingestion[field], f"Single-record exclusion count: {reason}")
    duplicate_groups = counts["internal_exact_duplicate"]
    conflict_groups = counts["conflicting_labels"]
    checks.equal(
        duplicate_groups, ingestion["internal_exact_duplicate_groups"], "Duplicate ID-group count"
    )
    checks.equal(
        conflict_groups,
        ingestion["conflicting_label_groups_quarantined"],
        "Conflict ID-group count",
    )
    duplicate_rows = ingestion["internal_exact_duplicate_rows_collapsed"]
    conflict_rows = ingestion["conflicting_label_rows_quarantined"]
    checks.require(
        duplicate_rows >= duplicate_groups and (duplicate_groups > 0 or duplicate_rows == 0),
        "Frozen duplicate-row multiplicity is consistent with public group count",
    )
    checks.require(
        conflict_rows >= 2 * conflict_groups and (conflict_groups > 0 or conflict_rows == 0),
        "Frozen conflict-row multiplicity is consistent with public group count",
    )
    return {
        "duplicate_groups_directly_counted": duplicate_groups,
        "duplicate_rows_from_frozen_aggregate": duplicate_rows,
        "conflict_groups_directly_counted": conflict_groups,
        "conflict_rows_from_frozen_aggregate": conflict_rows,
        "limitation": (
            "Public records deliberately omit source-row positions. Group counts and aggregate "
            "arithmetic are checked; duplicate/conflict raw-row multiplicities cannot be "
            "independently rederived without the private CSV or row provenance."
        ),
    }


def _audit_cohort(checks: Any, audit: Record, cohort: Sequence[Record]) -> Json:
    ingestion = audit["ingestion"]
    dedup = audit["dedup"]
    reserved = audit["reserved_identity_exclusion"]
    checks.equal(ingestion["original_rows"], RAW_COUNT, "Pinned source has 5971 records")
    checks.equal(
        ingestion["raw_label_counts"], RAW_LABEL_COUNTS, "All source label spellings are audited"
    )
    invalid = sum(
        ingestion[key]
        for key in (
            "malformed_rows_excluded",
            "unsupported_label_rows_excluded",
            "empty_rows_excluded",
            "overlong_rows_excluded",
            "ascii_letter_guard_rows_excluded",
        )
    )
    checks.equal(
        ingestion["eligible_rows_before_internal_dedup"],
        RAW_COUNT - invalid,
        "Raw rows reconcile with deterministic eligibility exclusions",
    )
    checks.equal(
        ingestion["accepted_unique_rows"],
        RAW_COUNT
        - invalid
        - ingestion["internal_exact_duplicate_rows_collapsed"]
        - ingestion["conflicting_label_rows_quarantined"],
        "Internal duplicate and conflict accounting reconciles",
    )
    checks.equal(
        dedup["input_unique_rows"], ingestion["accepted_unique_rows"], "Dedup stage input count"
    )
    checks.equal(
        dedup["accepted_unique_rows"],
        dedup["input_unique_rows"] - dedup["uci_overlap_rows_excluded"],
        "Train/validation overlap accounting reconciles",
    )
    checks.equal(
        reserved["input_unique_rows"],
        dedup["accepted_unique_rows"],
        "Reserved-ID stage input count",
    )
    checks.equal(
        reserved["accepted_unique_rows"],
        reserved["input_unique_rows"] - reserved["reserved_overlap_rows_excluded"],
        "Reserved identity and template exclusions reconcile",
    )
    checks.equal(
        reserved["accepted_unique_rows"], EXTERNAL_COUNT, "Exactly 733 rows survive all input rules"
    )
    checks.equal(audit["final_count"], EXTERNAL_COUNT, "Final audit count is frozen")
    checks.equal(
        audit["final_class_counts"], CLASS_COUNTS, "Final audit classes are ham410/spam323"
    )
    checks.equal(
        audit["uci_train_validation_reference_count"],
        3619 + 783,
        "UCI references exclude test texts",
    )
    checks.equal(
        audit["additional_authored_training_reference_count"],
        4,
        "Four used authored rows are references",
    )
    checks.equal(
        audit["reserved_test_metadata_records"],
        757,
        "Reserved comparison uses 757 metadata rows only",
    )
    checks.equal(
        dedup["reference_rows"], 3619 + 783 + 4, "All allowed training references are counted"
    )
    identities = []
    classes: Counter[str] = Counter()
    for row in cohort:
        checks.equal(set(row), {"sample_id", "truth"}, "Public cohort contains ID and label only")
        identity = row["sample_id"]
        checks.require(
            isinstance(identity, str) and independent.SHA256.fullmatch(identity) is not None,
            "Cohort sample ID is SHA-256",
        )
        checks.require(identity not in identities, "Cohort IDs are unique")
        identities.append(identity)
        checks.require(row["truth"] in {"ham", "spam"}, "Cohort ground truth is a fixed task label")
        classes[row["truth"]] += 1
    checks.equal(len(cohort), EXTERNAL_COUNT, "Public cohort has 733 unique rows")
    checks.equal(identities, sorted(identities), "Cohort ordering is deterministic by identity")
    checks.equal(dict(classes), CLASS_COUNTS, "Public cohort classes are independently counted")
    checks.equal(
        reserved["accepted_id_sha256"],
        _digest_ids(identities),
        "Final selected-ID hash is reproducible",
    )
    checks.equal(
        reserved["accepted_class_counts"], CLASS_COUNTS, "Final identity-stage class accounting"
    )
    exclusions = audit["exclusions"]
    counts = Counter(row["reason"] for row in exclusions)
    cross_ids = [row["sample_id"] for row in exclusions if row["reason"] in CROSS_REASONS]
    reserved_ids = [row["sample_id"] for row in exclusions if row["reason"] in RESERVED_REASONS]
    checks.equal(
        len(cross_ids), len(set(cross_ids)), "Cross-dataset exclusions contain one witness per ID"
    )
    checks.equal(
        len(reserved_ids), len(set(reserved_ids)), "Reserved exclusions contain one witness per ID"
    )
    checks.require(
        not (set(cross_ids) & set(reserved_ids)), "Exclusion stages do not remove the same ID twice"
    )
    checks.require(
        not ((set(cross_ids) | set(reserved_ids)) & set(identities)),
        "Excluded IDs are absent from cohort",
    )
    checks.equal(
        len(cross_ids), dedup["uci_overlap_rows_excluded"], "Overlap witness count matches audit"
    )
    checks.equal(
        len(reserved_ids),
        reserved["reserved_overlap_rows_excluded"],
        "Reserved witness count matches audit",
    )
    checks.equal(
        {reason: counts[reason] for reason in CROSS_REASONS if counts[reason]},
        dedup["overlap_counts_by_reason"],
        "Cross-dataset reasons reconcile",
    )
    checks.equal(
        {reason: counts[reason] for reason in RESERVED_REASONS if counts[reason]},
        reserved["overlap_counts_by_reason"],
        "Reserved metadata reasons reconcile",
    )
    for entry in exclusions:
        if entry["reason"] in CROSS_REASONS:
            checks.require(
                entry["reference_split"] in {"train", "validation"},
                "No test text supplied as reference",
            )
    multiplicity = _audit_cleaning_counts(checks, ingestion, exclusions)
    after_ingestion = identities + reserved_ids + cross_ids
    after_dedup = identities + reserved_ids
    checks.equal(
        len(after_ingestion),
        ingestion["accepted_unique_rows"],
        "Pre-overlap accepted ID reconstruction",
    )
    checks.equal(
        ingestion["accepted_id_sha256"], _digest_ids(after_ingestion), "Pre-overlap ID hash"
    )
    checks.equal(dedup["accepted_id_sha256"], _digest_ids(after_dedup), "Pre-reserved ID hash")
    return {
        "raw_rows": RAW_COUNT,
        "eligible_unique_before_overlap": len(after_ingestion),
        "uci_overlap_excluded": len(cross_ids),
        "reserved_metadata_excluded": len(reserved_ids),
        "selected_rows": EXTERNAL_COUNT,
        "selected_class_counts": dict(classes),
        "selected_ids_sha256": _digest_ids(identities),
        "cleaning_count_scope": multiplicity,
    }


def _accounting(checks: Any, records: Record, budget: Record, started_at: str) -> Json:
    attempt_time = datetime.fromisoformat(started_at)
    checks.require(attempt_time.tzinfo is not None, "Study attempt timestamp is timezone-aware")
    known_cost = Decimal(0)
    known_peak = Decimal(0)
    unknown_reserve = Decimal(0)
    usage_totals: Counter[str] = Counter()
    known_count = 0
    events: list[tuple[datetime, int, Decimal]] = []
    request_hashes = set()
    response_ids = set()
    for identity, record in records.items():
        checks.equal(record["sample_id"], identity, "Ledger dispatch identity is its key")
        checks.require(
            set(record) <= independent.EXTERNAL_KEYS,
            "Ledger keeps whitelisted numeric metadata only",
        )
        digest = record["request_sha256"]
        checks.require(
            isinstance(digest, str)
            and independent.SHA256.fullmatch(digest) is not None
            and digest not in request_hashes,
            "One unique request digest per dispatched ID",
        )
        request_hashes.add(digest)
        checks.require(
            record["status"] in {"ok", "unknown"}, "Every dispatch is terminal, never retrying"
        )
        if record.get("requested_model") is not None:
            checks.equal(
                record["requested_model"],
                independent.EXPECTED_MODEL,
                "Requested model alias is fixed",
            )
        if record["status"] == "ok":
            checks.equal(
                record.get("model"),
                independent.EXPECTED_MODEL,
                "Successful returned model is fixed",
            )
            checks.require(
                record["label"] in {"ham", "spam"}, "Successful response has a task label"
            )
        else:
            checks.equal(record.get("label"), None, "Unknown response does not invent a label")
        response_id = record.get("provider_id")
        if response_id is not None:
            checks.require(response_id not in response_ids, "No duplicate provider response IDs")
            response_ids.add(response_id)
        started = datetime.fromisoformat(record["started_at_utc"])
        checks.require(
            started.tzinfo is not None and started >= attempt_time,
            "Dispatch follows exclusive study marker",
        )
        reserved = independent._amount(record["reserved_yuan"])
        estimate = record["estimate"]
        checks.equal(
            estimate["max_output_tokens"],
            independent.MAX_OUTPUT_TOKENS,
            "Output reservation limit stays frozen",
        )
        byte_count = estimate["request_utf8_bytes"]
        checks.require(type(byte_count) is int and byte_count > 0, "Positive request byte count")
        bound = byte_count + independent.RESERVATION_FRAMING_TOKENS
        checks.equal(
            estimate["conservative_input_tokens"],
            bound,
            "Conservative input allowance is unchanged",
        )
        estimate_cost = (
            bound * independent.PEAK_RATES["cache_miss"]
            + independent.MAX_OUTPUT_TOKENS * independent.PEAK_RATES["output"]
        ) / independent.MILLION
        checks.equal(reserved, estimate_cost, "Independent dispatch reservation arithmetic")
        checks.equal(
            independent._amount(estimate["reservation_peak_yuan"]),
            reserved,
            "Estimate and reservation agree",
        )
        events.append((started, 0, reserved))
        usage = record.get("usage")
        if usage is None:
            checks.equal(record.get("cost_yuan"), None, "Unknown usage has no invented charge")
            checks.equal(
                record.get("conservative_peak_yuan"), None, "Unknown usage retains its peak hold"
            )
            unknown_reserve += reserved
            continue
        rates = independent._rate_at(record["started_at_utc"])
        observed_rates = {
            key: independent._amount(value)
            for key, value in record["tariff_rates_yuan_per_million"].items()
        }
        checks.equal(observed_rates, rates, "Per-call tariff follows Beijing dispatch time")
        cost = independent._usage_cost(usage, rates)
        peak = independent._usage_cost(usage, independent.PEAK_RATES)
        checks.equal(
            independent._amount(record["cost_yuan"]), cost, "Actual-period five-counter usage price"
        )
        checks.equal(
            independent._amount(record["conservative_peak_yuan"]),
            peak,
            "Peak-period five-counter usage price",
        )
        checks.require(
            peak <= reserved and not record.get("reservation_exceeded", False),
            "No request exceeds its hold",
        )
        finished = datetime.fromisoformat(record["finished_at_utc"])
        checks.require(
            finished.tzinfo is not None and finished >= started, "Finish timestamp follows dispatch"
        )
        events.append((finished, 1, peak - reserved))
        known_cost += cost
        known_peak += peak
        known_count += 1
        usage_totals.update(usage)
    checks.equal(
        independent._amount(budget["cap_yuan"]), AUTHORIZED_CAP, "Dispatch cap is exactly four yuan"
    )
    checks.equal(
        independent._amount(budget["authorized_max_cap_yuan"]),
        AUTHORIZED_CAP,
        "Explicit authority is four yuan",
    )
    exposure = Decimal(0)
    maximum = Decimal(0)
    for _time, _kind, delta in sorted(events):
        exposure += delta
        maximum = max(maximum, exposure)
        checks.require(
            0 <= exposure <= AUTHORIZED_CAP,
            "Every chronological reservation exposure stays within four yuan",
        )
    expected = {
        "known_cost_yuan": known_cost,
        "conservative_peak_yuan": known_peak,
        "unknown_reserved_yuan": unknown_reserve,
        "cap_exposure_yuan": known_peak + unknown_reserve,
        "attempt_count": len(records),
        "known_usage_count": known_count,
        "unknown_usage_count": len(records) - known_count,
    }
    independent._compare_fields(checks, budget, expected, "budget")
    checks.equal(
        exposure, known_peak + unknown_reserve, "Final reconstructed exposure matches ledger"
    )
    checks.equal(
        budget["usage"],
        {key: usage_totals[key] for key in independent.USAGE_FIELDS},
        "Five token totals reconcile",
    )
    return {
        **expected,
        "maximum_chronological_exposure_yuan": maximum,
        "five_counter_usage_totals": dict(usage_totals),
        "unique_request_digest_count": len(request_hashes),
        "unique_provider_response_id_count": len(response_ids),
        "authorized_cap_yuan": AUTHORIZED_CAP,
    }


def _signed_deltas(current: Record, previous: Record) -> Json:
    now = current["full_denominator_conservative_metrics"]
    old = previous["full_denominator_conservative_metrics"]
    result = {
        key: now[key] - old[key] if now[key] is not None and old[key] is not None else None
        for key in (
            "macro_f1",
            "spam_precision",
            "spam_recall",
            "ham_precision",
            "ham_recall",
            "accuracy",
        )
    }
    for prefix, strategy in (("external", current), ("validation", previous)):
        confusion = strategy["full_denominator_conservative_metrics"]["confusion"]
        abstained_ham = strategy["full_denominator_conservative_metrics"][
            "abstention_count_by_truth"
        ]["ham"]
        ham = confusion["true_negative"] + confusion["false_positive"] + abstained_ham
        result[f"{prefix}_false_positive_ham_count"] = confusion["false_positive"]
        result[f"{prefix}_ham_denominator"] = ham
        result[f"{prefix}_false_positive_ham_rate"] = (
            confusion["false_positive"] / ham if ham else None
        )
    result["false_positive_ham_rate_delta"] = (
        result["external_false_positive_ham_rate"] - result["validation_false_positive_ham_rate"]
    )
    result["count_comparison_scope"] = (
        "Different cohort sizes/class balances; compare rates alongside counts"
    )
    return result


def _paired_diagnostics(rows: Sequence[Record]) -> Json:
    scopes: dict[str, Counter[str]] = {"all_rows": Counter(), "fixed_escalated_rows": Counter()}
    local_errors_by_position = {"inside_fixed_band": 0, "outside_fixed_band": 0}
    error_kinds_by_position = {
        "inside_fixed_band": {"ham_false_positive": 0, "spam_false_negative": 0},
        "outside_fixed_band": {"ham_false_positive": 0, "spam_false_negative": 0},
    }
    for row in rows:
        local = row["local"]
        remote = row["deepseek"]
        inside = BAND[0] <= local["spam_score"] <= BAND[1]
        local_wrong = local["label"] != row["truth"]
        position = "inside_fixed_band" if inside else "outside_fixed_band"
        if local_wrong:
            local_errors_by_position[position] += 1
            kind = "ham_false_positive" if row["truth"] == "ham" else "spam_false_negative"
            error_kinds_by_position[position][kind] += 1
        for scope in ("all_rows", "fixed_escalated_rows") if inside else ("all_rows",):
            counts = scopes[scope]
            counts["rows"] += 1
            counts["local_wrong"] += local_wrong
            if remote["status"] != "ok":
                counts["external_abstentions"] += 1
                counts["local_wrong_external_abstained"] += local_wrong
                continue
            counts["both_completed"] += 1
            external_wrong = remote["label"] != row["truth"]
            counts["external_wrong_completed"] += external_wrong
            if local_wrong and not external_wrong:
                counts["external_fixes_local_errors"] += 1
                counts[f"fixes_{row['truth']}"] += 1
            elif not local_wrong and external_wrong:
                counts["external_regressions_from_local_correct"] += 1
                counts[f"regressions_{row['truth']}"] += 1
            elif local_wrong:
                counts["both_wrong"] += 1
            else:
                counts["both_correct"] += 1
    fields = (
        "rows",
        "both_completed",
        "local_wrong",
        "external_wrong_completed",
        "external_abstentions",
        "local_wrong_external_abstained",
        "external_fixes_local_errors",
        "external_regressions_from_local_correct",
        "both_wrong",
        "both_correct",
        "fixes_ham",
        "fixes_spam",
        "regressions_ham",
        "regressions_spam",
    )
    normalized = {scope: {key: counts[key] for key in fields} for scope, counts in scopes.items()}
    escalated = scopes["fixed_escalated_rows"]
    net_change = (
        escalated["external_regressions_from_local_correct"]
        - escalated["external_fixes_local_errors"]
    )
    return {
        "band": BAND,
        "local_errors_by_position": local_errors_by_position,
        "local_error_kinds_by_position": error_kinds_by_position,
        "paired_outcomes": normalized,
        "fixed_cascade_error_change_vs_local": net_change,
        "expected_fixed_cascade_error_count": scopes["all_rows"]["local_wrong"] + net_change,
        "scope": (
            "Only the preregistered band and the full paired cohort are described. Outside-band "
            "local errors cannot be repaired by this fixed cascade. Scores are uncalibrated; "
            "no new band, threshold, calibration or causal interpretation is selected."
        ),
    }


def verify(root: Path) -> Json:
    """Verify frozen preregistration, external metrics and the complete dispatch budget."""
    root = root.resolve()
    if root.is_relative_to((PROJECT / "var").resolve()):
        raise independent.VerificationFailure(
            "External verification must never read formal or raw data"
        )
    contents = {name: _require_path(root / name) for name in INPUT_FILES}
    objects = {
        name: independent._read_json(content)
        for name, content in contents.items()
        if name != "results.jsonl"
    }
    rows = [
        independent._read_json(line)
        for line in contents["results.jsonl"].splitlines()
        if line.strip()
    ]
    frozen_bytes = _require_path(PROJECT / FROZEN_CONFIG)
    validation_bytes = _require_path(PROJECT / VALIDATION_SUMMARY)
    document = _require_path(PROJECT / PLAN_DOCUMENT)
    frozen = independent._read_json(frozen_bytes)
    validation = independent._read_json(validation_bytes)
    for value in (*objects.values(), rows, frozen, validation):
        _privacy(value)
    checks = independent.Checks()
    plan = objects["plan.json"]
    audit = objects["audit.json"]
    cohort = objects["cohort.json"]
    ledger = objects["ledger.json"]
    budget = objects["budget.json"]
    summary = objects["summary.json"]
    attempt = objects["evaluation-attempt.json"]
    config = {
        key: value for key, value in plan.items() if key not in {"plan_sha256", "document_sha256"}
    }
    checks.equal(
        plan["plan_sha256"],
        independent._sha(independent._canonical(config)),
        "Plan self-hash is reproducible",
    )
    checks.equal(
        plan["document_sha256"],
        independent._sha(document),
        "Preregistered Markdown bytes are immutable",
    )
    checks.equal(
        plan["audit_sha256"],
        independent._sha(independent._canonical(audit)),
        "Input-only audit is frozen",
    )
    checks.equal(
        plan["frozen_config_sha256"],
        independent._sha(frozen_bytes),
        "Original validation-frozen config bytes match",
    )
    checks.equal(
        plan["validation_summary_sha256"],
        independent._sha(validation_bytes),
        "Validation comparison bytes match",
    )
    checks.equal(
        plan["data_audit"],
        {key: value for key, value in audit.items() if key != "exclusions"},
        "Plan and full audit agree",
    )
    checks.equal(
        plan["deepseek"],
        frozen["deepseek"],
        "Prompt/model/request parameters exactly match validation freeze",
    )
    checks.equal(
        plan["local_model"],
        frozen["local"],
        "Local artifact and threshold exactly match validation freeze",
    )
    checks.equal(
        plan["text_preprocessing"],
        frozen["text_preprocessing"],
        "Text preprocessing matches validation freeze",
    )
    checks.equal(plan["cascade_band"], BAND, "External test has only the selected inclusive band")
    checks.equal(
        plan["cascade_band"],
        frozen["cascade"]["recommended_band"],
        "Band matches pre-external selection",
    )
    checks.equal(plan["study_id"], STUDY_ID, "Study identity is fixed")
    checks.equal(plan["external_count"], EXTERNAL_COUNT, "Preregistered cohort count is 733")
    checks.equal(plan["class_counts"], CLASS_COUNTS, "Preregistered class balance is fixed")
    parameters = plan["deepseek"]["payload_parameters"]
    checks.equal(
        parameters,
        {
            "model": "deepseek-flash",
            "temperature": 0,
            "thinking": {"type": "disabled"},
            "response_format": {"type": "json_object"},
            "max_tokens": 32,
            "stream": False,
        },
        "Original label-only deterministic payload parameters are unchanged",
    )
    checks.require(
        type(parameters["temperature"]) is int, "Temperature zero is numeric rather than boolean"
    )
    checks.equal(
        plan["deepseek"]["prompt_sha256"],
        independent._sha(plan["deepseek"]["system_prompt"].encode()),
        "System prompt hash",
    )
    checks.equal(plan["execution"]["no_retry"], True, "The frozen protocol forbids retries")
    checks.equal(
        independent._amount(plan["execution"]["cap_yuan"]),
        AUTHORIZED_CAP,
        "Plan spending cap is four yuan",
    )
    checks.equal(
        independent._amount(plan["execution"]["authorized_max_cap_yuan"]),
        AUTHORIZED_CAP,
        "Plan authority is four yuan",
    )
    checks.equal(attempt["study_id"], STUDY_ID, "Exclusive attempt belongs to the frozen study")
    checks.equal(attempt["plan_sha256"], plan["plan_sha256"], "Attempt binds the exact plan")
    checks.equal(
        attempt["document_sha256"], plan["document_sha256"], "Attempt binds preregistered prose"
    )
    commit = attempt["execution_commit"]
    checks.require(
        isinstance(commit, str) and COMMIT_SHA.fullmatch(commit) is not None,
        "Attempt records a full Git commit",
    )
    for relative, current in (
        (PLAN_DOCUMENT, document),
        (FROZEN_CONFIG, frozen_bytes),
        (VALIDATION_SUMMARY, validation_bytes),
        ("artifacts/external-test/plan.json", contents["plan.json"]),
        ("artifacts/external-test/audit.json", contents["audit.json"]),
        ("artifacts/external-test/cohort.json", contents["cohort.json"]),
    ):
        checks.equal(
            _git_bytes(commit, relative), current, f"Git preregistration blob matches: {relative}"
        )
    sources = plan["execution"]["source_sha256"]
    checks.equal(
        set(sources), EXECUTION_SOURCES, "Every executed inference/data boundary source is frozen"
    )
    for relative, expected in sources.items():
        checks.equal(
            independent._sha(_require_path(PROJECT / relative)),
            expected,
            f"Current execution source: {relative}",
        )
        checks.equal(
            independent._sha(_git_bytes(commit, relative)),
            expected,
            f"Attempt-commit execution source: {relative}",
        )
    cohort_audit = _audit_cohort(checks, audit, cohort)
    checks.equal(
        audit["ingestion"]["source_csv_sha256"],
        plan["dataset"]["csv_sha256"],
        "Pinned source CSV digest agrees",
    )
    checks.equal(
        summary["selection_performed"], False, "No selection performed on external outcomes"
    )
    checks.require(
        "recommendation" not in summary and "best_observed_nonzero_band" not in summary,
        "No external recommender ran",
    )
    checks.equal(summary["local"]["threshold"], 0.5, "Reported local threshold remains frozen")
    checks.equal(summary["fixed_cascade_band"], BAND, "Reported external band stays fixed")
    checks.equal(
        summary["requested_rows"],
        EXTERNAL_COUNT,
        "Every selected sample stays in report denominator",
    )
    checks.equal(len(summary["cascades"]), 1, "Only one cascade was evaluated")
    checks.equal(summary["cascades"][0]["band"], BAND, "No cascade sweep appears in results")
    checks.equal(summary["plan_sha256"], plan["plan_sha256"], "Summary uses original plan")
    checks.equal(
        summary["external_sha256"],
        plan["external_sha256"],
        "Private cohort digest is consistently referenced",
    )
    checks.equal(summary["budget"], budget, "Budget and summary are identical")
    checks.equal(
        ledger["plan_sha256"],
        plan["plan_sha256"],
        "Public ledger belongs to the frozen external study",
    )
    checks.equal(
        [row["sample_id"] for row in rows],
        [row["sample_id"] for row in cohort],
        "Results exactly match all frozen cohort IDs and order",
    )
    records = ledger["records"]
    identities = {row["sample_id"] for row in cohort}
    checks.require(
        set(records) <= identities, "Every dispatch belongs to the frozen external cohort"
    )
    for row, fixed in zip(rows, cohort, strict=True):
        checks.equal(set(row), independent.ROW_KEYS, "Result row is ID/label/safe outputs only")
        checks.equal(row["truth"], fixed["truth"], "Ground truth was fixed before predictions")
        local = row["local"]
        remote = row["deepseek"]
        checks.require(set(local) <= independent.LOCAL_KEYS, "Local output whitelist")
        checks.require(set(remote) <= independent.EXTERNAL_KEYS, "External output whitelist")
        checks.equal(local["status"], "ok", "Every local prediction completed")
        score = local["spam_score"]
        checks.require(
            type(score) in (int, float) and math.isfinite(score) and 0 <= score <= 1,
            "Local score is finite",
        )
        checks.equal(
            local["label"], "spam" if score >= 0.5 else "ham", "Local frozen-threshold label"
        )
        for output in (local, remote):
            elapsed = output.get("elapsed_ms")
            checks.require(
                elapsed is None
                or (type(elapsed) in (int, float) and math.isfinite(elapsed) and elapsed >= 0),
                "Latency is finite/nonnegative or explicitly missing",
            )
        if row["sample_id"] in records:
            checks.equal(
                remote,
                records[row["sample_id"]],
                "Per-ID request hash and metadata match ledger exactly",
            )
        else:
            checks.equal(
                remote["status"], "not_attempted", "Undispatched row is explicitly counted"
            )
            checks.equal(
                remote.get("label"), None, "Undispatched row has no predicted external label"
            )
    integrity = budget["data_integrity"]
    checks.equal(
        integrity["heldout_test_files_opened"], 0, "Runtime audit records zero held-out text reads"
    )
    checks.equal(
        integrity["blocked_formal_accesses"], 0, "Runtime respected the formal-data allowlist"
    )
    checks.equal(integrity["protected_files_unchanged"], True, "Formal inputs remained unchanged")
    accounting = _accounting(checks, records, budget, attempt["started_at_utc"])
    truths = [row["truth"] for row in rows]
    local_predictions = [row["local"] for row in rows]
    remote_predictions = [row["deepseek"] for row in rows]
    verified = {
        "local": independent._verify_strategy(
            checks, truths, local_predictions, (), summary["local"], "local"
        ),
        "deepseek": independent._verify_strategy(
            checks, truths, remote_predictions, remote_predictions, summary["deepseek"], "deepseek"
        ),
    }
    cascade_predictions = []
    upgraded = []
    upgraded_ids = []
    fallbacks = 0
    for row in rows:
        local = row["local"]
        remote = row["deepseek"]
        prediction = dict(local)
        if BAND[0] <= local["spam_score"] <= BAND[1]:
            upgraded_ids.append(row["sample_id"])
            upgraded.append(remote)
            if remote["status"] == "ok":
                prediction = dict(remote)
            else:
                fallbacks += 1
            elapsed = local.get("elapsed_ms"), remote.get("elapsed_ms")
            prediction["elapsed_ms"] = (
                sum(elapsed)
                if remote["status"] != "not_attempted" and None not in elapsed
                else None
            )
        cascade_predictions.append(prediction)
    observed_cascade = summary["cascades"][0]
    verified["cascade"] = independent._verify_strategy(
        checks, truths, cascade_predictions, upgraded, observed_cascade, "fixed_cascade"
    )
    attempted = sum(output["status"] != "not_attempted" for output in upgraded)
    completed = sum(output["status"] == "ok" for output in upgraded)
    independent._compare_fields(
        checks,
        observed_cascade,
        {
            "planned_escalation_count": len(upgraded),
            "planned_escalation_fraction": len(upgraded) / EXTERNAL_COUNT,
            "escalation_attempted_count": attempted,
            "escalation_completed_count": completed,
            "escalation_unknown_count": sum(output["status"] == "unknown" for output in upgraded),
            "escalation_not_attempted_count": len(upgraded) - attempted,
            "escalation_failure_count": sum(
                bool(output.get("failure", False)) for output in upgraded
            ),
            "fallback_count": fallbacks,
            "fallback_completed_count": fallbacks,
        },
        "fixed_cascade",
    )
    paired_diagnostics = _paired_diagnostics(rows)
    cascade_confusion = verified["cascade"]["metrics"]["confusion"]
    checks.equal(
        paired_diagnostics["expected_fixed_cascade_error_count"],
        cascade_confusion["false_positive"] + cascade_confusion["false_negative"],
        "Paired fixes/regressions exactly explain the fixed cascade error count",
    )
    validation_cascade = next(item for item in validation["cascades"] if item["band"] == BAND)
    deltas = {
        "local": _signed_deltas(summary["local"], validation["local"]),
        "deepseek": _signed_deltas(summary["deepseek"], validation["deepseek"]),
        "cascade": _signed_deltas(observed_cascade, validation_cascade),
    }
    return {
        "status": "passed",
        "verifier": "Independent stdlib-only external-test verifier",
        "execution_commit": commit,
        "plan_sha256": plan["plan_sha256"],
        "input_sha256": {name: independent._sha(content) for name, content in contents.items()},
        "independent_helper_sha256": independent._sha(HELPER_PATH.read_bytes()),
        "passed_check_count": len(checks.passed),
        "check_categories": sorted(set(checks.passed)),
        "cohort_audit": cohort_audit,
        "accounting": accounting,
        "recomputed": verified,
        "fixed_cascade": {
            "band": BAND,
            "upgraded_count": len(upgraded),
            "fallback_count": fallbacks,
            "upgraded_ids_sha256": _digest_ids(upgraded_ids),
        },
        "signed_external_minus_validation": deltas,
        "paired_error_diagnostics": paired_diagnostics,
        "limits": [
            "Only exported IDs/labels/numbers, frozen documentation, allowed source files "
            "and Git blobs were read.",
            "No formal var data, raw external CSV, sealed SMS, snapshots, credentials "
            "or network were accessed.",
            "SMS bodies are intentionally absent: source CSV, private cohort-content "
            "and request-body digests "
            "cannot be independently regenerated here; frozen and repeated saved digests "
            "are checked instead.",
            "One dispatch record per ID and unique returned IDs are checked; "
            "no transport retry proof is "
            "inferred solely from a final ledger.",
            "Peak usage plus chronological reservations is a dispatch exposure check; "
            "it does not verify a provider bill.",
            "Raw row positions and SMS are absent. Duplicate/conflict multiplicities are frozen "
            "aggregates checked for arithmetic consistency, not independently reparsed CSV counts.",
            "This audit performs no model selection and makes no statistical or causal claim.",
        ],
    }


def main() -> None:
    """Audit a completed exported run and save its verification without issuing model requests."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args()
    result = verify(args.root)
    target = args.root.resolve() / "verification.json"
    if target.is_symlink():
        raise independent.VerificationFailure("Verification output must not be a symlink")
    target.write_text(
        json.dumps(independent._json_ready(result), indent=2, ensure_ascii=False, allow_nan=False)
        + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"status": result["status"], "checks": result["passed_check_count"]}))


if __name__ == "__main__":
    main()
