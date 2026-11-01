"""Pure, denominator-explicit analysis of paired local and external SMS predictions."""

from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation, localcontext
import math
from typing import Any

LABELS = ("ham", "spam")
STATUSES = ("ok", "unknown", "not_attempted")
LOCAL_THRESHOLD = 0.5
CASCADE_BANDS = ((0.45, 0.55), (0.4, 0.6), (0.3, 0.7), (0.2, 0.8), (0.1, 0.9), (0.0, 1.0))
COST_SCALE_ROWS = 1000
COST_PRECISION_DIGITS = 28
P50_QUANTILE = 0.5
P95_QUANTILE = 0.95

JsonObject = dict[str, Any]
Record = Mapping[str, Any]


def _money(value: Any, field: str, *, optional: bool = False) -> Decimal | None:
    if value is None and optional:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a nonnegative decimal string")
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"{field} must be a nonnegative decimal string") from exc
    if not number.is_finite() or number < 0:
        raise ValueError(f"{field} must be a nonnegative decimal string")
    return number


def _elapsed(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("elapsed_ms must be a nonnegative finite number or null")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError("elapsed_ms must be a nonnegative finite number or null")
    return number


def _validate(rows: Sequence[Record]) -> None:
    identities = set()
    for row in rows:
        identity = row.get("sample_id")
        if not isinstance(identity, str) or not identity or identity in identities:
            raise ValueError("sample_id must be a unique nonempty string")
        identities.add(identity)
        if row.get("truth") not in LABELS:
            raise ValueError("truth must be ham or spam")
        for engine in ("local", "deepseek"):
            output = row.get(engine)
            if not isinstance(output, Mapping) or output.get("status") not in STATUSES:
                raise ValueError(f"{engine} has an invalid prediction status")
            if output["status"] == "ok":
                if output.get("label") not in LABELS:
                    raise ValueError(f"{engine} completed without a valid label")
            elif output.get("label") is not None:
                raise ValueError(f"{engine} abstention must not contain a predicted label")
            _elapsed(output.get("elapsed_ms"))
            if not isinstance(output.get("failure", False), bool):
                raise ValueError(f"{engine} failure flag must be boolean")
        local = row["local"]
        score = local.get("spam_score")
        if (
            local["status"] != "ok"
            or isinstance(score, bool)
            or not isinstance(score, (int, float))
            or not math.isfinite(score)
            or not 0 <= score <= 1
        ):
            raise ValueError("local must contain a completed finite probability score")
        expected = "spam" if score >= LOCAL_THRESHOLD else "ham"
        if local["label"] != expected:
            raise ValueError("local label differs from the fixed 0.5 threshold")
        deepseek = row["deepseek"]
        _money(deepseek.get("cost_yuan"), "cost_yuan", optional=True)
        _money(deepseek.get("reserved_yuan", "0"), "reserved_yuan")
        if deepseek.get("usage") is not None and not isinstance(deepseek["usage"], Mapping):
            raise ValueError("usage must be an object or null")


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _metrics(truths: Sequence[str], outputs: Sequence[Record], *, conservative: bool) -> JsonObject:
    confusion = {"true_positive": 0, "true_negative": 0, "false_positive": 0, "false_negative": 0}
    abstentions = {label: 0 for label in LABELS}
    for truth, output in zip(truths, outputs, strict=True):
        if output["status"] != "ok":
            abstentions[truth] += 1
        elif truth == "spam":
            key = "true_positive" if output["label"] == "spam" else "false_negative"
            confusion[key] += 1
        else:
            key = "true_negative" if output["label"] == "ham" else "false_positive"
            confusion[key] += 1
    tp = confusion["true_positive"]
    tn = confusion["true_negative"]
    fp = confusion["false_positive"]
    fn = confusion["false_negative"]
    missing_spam = abstentions["spam"] if conservative else 0
    missing_ham = abstentions["ham"] if conservative else 0
    denominator = len(truths) if conservative else tp + tn + fp + fn
    spam_denominator = 2 * tp + fp + fn + missing_spam
    ham_denominator = 2 * tn + fn + fp + missing_ham
    spam_f1 = 2 * tp / spam_denominator if spam_denominator else 0.0
    ham_f1 = 2 * tn / ham_denominator if ham_denominator else 0.0
    return {
        "denominator": denominator,
        "macro_f1": (spam_f1 + ham_f1) / 2 if denominator else None,
        "spam_precision": _ratio(tp, tp + fp),
        "spam_recall": _ratio(tp, tp + fn + missing_spam),
        "ham_precision": _ratio(tn, tn + fn),
        "ham_recall": _ratio(tn, tn + fp + missing_ham),
        "accuracy": _ratio(tp + tn, denominator),
        "false_positive_ham_count": fp,
        "confusion": confusion,
        "abstention_count_by_truth": abstentions,
        "class_false_negative_counts": {
            "spam": fn + missing_spam,
            "ham": fp + missing_ham,
        },
        "abstention_policy": (
            "Each abstention is a false negative for its true class and an accuracy error; "
            "no predicted label or false positive is invented."
            if conservative
            else "Metrics exclude abstentions; coverage and the full denominator are separate."
        ),
        "macro_f1_label_set": list(LABELS),
        "undefined_class_f1": 0.0,
    }


def _quantile(values: Sequence[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _latency(outputs: Sequence[Record], *, completed: bool) -> JsonObject:
    eligible = (
        [output for output in outputs if output["status"] == "ok"]
        if completed
        else [output for output in outputs if output["status"] != "not_attempted"]
    )
    values = [
        value for output in eligible if (value := _elapsed(output.get("elapsed_ms"))) is not None
    ]
    return {
        "status_denominator": len(eligible),
        "measurement_count": len(values),
        "missing_measurement_count": len(eligible) - len(values),
        "p50": _quantile(values, P50_QUANTILE),
        "p95": _quantile(values, P95_QUANTILE),
        "quantile_method": "linear interpolation at (n - 1) * quantile",
    }


def _cost(
    outputs: Sequence[Record], requested_rows: int, *, local_only: bool = False
) -> JsonObject:
    known = Decimal(0)
    unknown_reserved = Decimal(0)
    priced = 0
    unpriced_attempts = 0
    usage_returned = 0
    for output in outputs if not local_only else ():
        if output["status"] == "not_attempted":
            continue
        if output.get("usage") is not None:
            usage_returned += 1
        cost = _money(output.get("cost_yuan"), "cost_yuan", optional=True)
        if cost is None:
            unpriced_attempts += 1
            reservation = _money(output.get("reserved_yuan", "0"), "reserved_yuan")
            assert reservation is not None
            unknown_reserved += reservation
        else:
            known += cost
            priced += 1
    with localcontext() as context:
        context.prec = COST_PRECISION_DIGITS
        per_thousand = known * COST_SCALE_ROWS / requested_rows if requested_rows else None
    return {
        "known_cost_yuan": format(known, "f"),
        "known_cost_yuan_per_1000_requested_rows": (
            format(per_thousand, "f") if per_thousand is not None else None
        ),
        "requested_rows_denominator": requested_rows,
        "priced_attempt_count": priced,
        "returned_usage_count": usage_returned,
        "unpriced_attempt_count": unpriced_attempts,
        "unknown_reserved_yuan": format(unknown_reserved, "f"),
        "known_plus_unknown_reservation_yuan": format(known + unknown_reserved, "f"),
        "scope": (
            "External API cost is zero for local-only inference; local compute is not priced."
            if local_only
            else "Known cost uses returned token-priced charges. Unknown reservations are separate "
            "budget holds, not measured charges or a billing guarantee."
        ),
    }


def _summary(
    truths: Sequence[str],
    outputs: Sequence[Record],
    cost_outputs: Sequence[Record],
    *,
    local_only: bool = False,
) -> JsonObject:
    requested = len(truths)
    completed = sum(output["status"] == "ok" for output in outputs)
    attempted = sum(output["status"] != "not_attempted" for output in outputs)
    metrics = _metrics(truths, outputs, conservative=True)
    correct = metrics["confusion"]["true_positive"] + metrics["confusion"]["true_negative"]
    abstained = requested - completed
    unknown = sum(output["status"] == "unknown" for output in outputs)
    failures = sum(bool(output.get("failure", False)) for output in outputs)
    return {
        "requested_count": requested,
        "attempted_count": attempted,
        "completed_count": completed,
        "unknown_count": unknown,
        "not_attempted_count": requested - attempted,
        "failure_count": failures,
        "failure_fraction_requested": _ratio(failures, requested),
        "failure_fraction_attempted": _ratio(failures, attempted),
        "unknown_fraction_requested": _ratio(unknown, requested),
        "not_attempted_fraction_requested": _ratio(requested - attempted, requested),
        "failure_count_scope": "Failure flags can overlap unknown status; counts are not additive.",
        "coverage": _ratio(completed, requested),
        "attempt_fraction": _ratio(attempted, requested),
        "completed_only_metrics": _metrics(truths, outputs, conservative=False),
        "full_denominator_conservative_metrics": metrics,
        "abstention_diagnostics": {
            "correct_completed_count": correct,
            "incorrect_completed_count": completed - correct,
            "abstention_count": abstained,
            "accuracy_lower_bound": _ratio(correct, requested),
            "accuracy_upper_bound": _ratio(correct + abstained, requested),
            "bound_scope": (
                "Unknown predictions could all be wrong or all be correct; no imputation."
            ),
        },
        "latency_ms": {
            "requested_rows_denominator": requested,
            "completed": _latency(outputs, completed=True),
            "attempted": _latency(outputs, completed=False),
        },
        "cost": _cost(cost_outputs, requested, local_only=local_only),
    }


def _cascade(rows: Sequence[Record], low: float, high: float) -> JsonObject:
    predictions = []
    escalations = []
    fallback_count = 0
    fallback_completed = 0
    for row in rows:
        local = row["local"]
        if not low <= local["spam_score"] <= high:
            predictions.append(dict(local))
            continue
        remote = row["deepseek"]
        escalations.append(remote)
        if remote["status"] == "ok":
            prediction = dict(remote)
        else:
            prediction = dict(local)
            fallback_count += 1
            fallback_completed += local["status"] == "ok"
        local_elapsed = _elapsed(local.get("elapsed_ms"))
        remote_elapsed = _elapsed(remote.get("elapsed_ms"))
        prediction["elapsed_ms"] = (
            local_elapsed + remote_elapsed
            if remote["status"] != "not_attempted"
            and local_elapsed is not None
            and remote_elapsed is not None
            else None
        )
        predictions.append(prediction)
    result = _summary([row["truth"] for row in rows], predictions, escalations)
    planned = len(escalations)
    completed = sum(output["status"] == "ok" for output in escalations)
    attempted = sum(output["status"] != "not_attempted" for output in escalations)
    result.update(
        {
            "strategy_id": f"cascade[{low:g},{high:g}]",
            "band": [low, high],
            "selection_rule": "Escalate when low <= local spam_score <= high, endpoints included.",
            "planned_escalation_count": planned,
            "planned_escalation_fraction": _ratio(planned, len(rows)),
            "escalation_attempted_count": attempted,
            "escalation_attempted_fraction_requested": _ratio(attempted, len(rows)),
            "escalation_attempted_fraction_planned": _ratio(attempted, planned),
            "escalation_completed_count": completed,
            "escalation_completed_fraction_planned": _ratio(completed, planned),
            "escalation_unknown_count": sum(
                output["status"] == "unknown" for output in escalations
            ),
            "escalation_not_attempted_count": planned - attempted,
            "escalation_failure_count": sum(
                bool(output.get("failure", False)) for output in escalations
            ),
            "all_planned_escalations_completed": completed == planned,
            "fallback_count": fallback_count,
            "fallback_completed_count": fallback_completed,
            "fallback_policy": (
                "Retain the local label when an escalated output is missing or unknown."
            ),
            "latency_scope": (
                "Counterfactual replay of cached per-row measurements: local latency plus external "
                "latency for escalated rows. Missing external attempts have unknown "
                "cascade latency. "
                "These are not actual sequential cascade trials, scheduling or "
                "capacity measurements."
            ),
            "eligible_for_recommendation": (
                bool(rows)
                and result["coverage"] == 1.0
                and completed == planned
                and result["cost"]["unpriced_attempt_count"] == 0
            ),
        }
    )
    return result


def _quality(strategy: Record) -> float | None:
    return strategy["full_denominator_conservative_metrics"]["macro_f1"]


def _selection_key(strategy: Record) -> tuple[Decimal, float, float, float]:
    low, high = strategy["band"]
    quality = _quality(strategy)
    if quality is None:
        raise ValueError("A strategy without observed rows cannot be ranked")
    return Decimal(strategy["cost"]["known_cost_yuan"]), -quality, high - low, low


def _best_observed_key(strategy: Record) -> tuple[float, Decimal, float, float]:
    cost, negative_quality, width, lower = _selection_key(strategy)
    return negative_quality, cost, width, lower


def _validated_bands(bands: Sequence[Sequence[float]]) -> tuple[tuple[float, float], ...]:
    if not isinstance(bands, Sequence) or isinstance(bands, (str, bytes)) or not bands:
        raise ValueError("Cascade bands must be a nonempty sequence of endpoint pairs")
    result = []
    for band in bands:
        if (
            not isinstance(band, Sequence)
            or isinstance(band, (str, bytes))
            or len(band) != 2
            or any(
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                for value in band
            )
        ):
            raise ValueError("Each cascade band must contain two finite numeric endpoints")
        low, high = float(band[0]), float(band[1])
        if not 0 <= low <= high <= 1 or (low, high) in result:
            raise ValueError("Cascade bands must be distinct and within ordered bounds [0, 1]")
        result.append((low, high))
    return tuple(result)


def summarize(
    rows: Sequence[Record], *, bands: Sequence[Sequence[float]] = CASCADE_BANDS
) -> JsonObject:
    """Analyze only the supplied bands so a frozen external test cannot expand its search."""
    checked_bands = _validated_bands(bands)
    _validate(rows)
    truths = [row["truth"] for row in rows]
    local_outputs = [row["local"] for row in rows]
    remote_outputs = [row["deepseek"] for row in rows]
    local = _summary(truths, local_outputs, (), local_only=True)
    local["strategy_id"] = "local"
    local["threshold"] = LOCAL_THRESHOLD
    remote = _summary(truths, remote_outputs, remote_outputs)
    remote["strategy_id"] = "deepseek"
    cascades = [_cascade(rows, low, high) for low, high in checked_bands]
    eligible = [strategy for strategy in cascades if strategy["eligible_for_recommendation"]]
    baseline_quality = _quality(local)
    improved = [
        strategy
        for strategy in eligible
        if baseline_quality is not None and _quality(strategy) > baseline_quality
    ]
    selected = min(improved, key=_selection_key) if improved else local
    nonzero = [strategy for strategy in eligible if strategy["planned_escalation_count"] > 0]
    best_observed = min(nonzero, key=_best_observed_key) if nonzero else None
    return {
        "schema_version": 1,
        "requested_rows": len(rows),
        "cohort_policy": "Every strategy uses the same supplied sample IDs and truth labels.",
        "local": local,
        "deepseek": remote,
        "cascades": cascades,
        "recommendation": {
            "strategy_id": selected["strategy_id"],
            "band": selected.get("band"),
            "selection_rule": (
                "Require complete predictions and priced successful planned escalations, "
                "then strict "
                "macro-F1 improvement over local. Choose lowest known API cost, "
                "then higher macro-F1, "
                "narrower band, and lower endpoint. With no strict improvement retain local."
            ),
            "reason": (
                "A complete cascade strictly improves macro-F1 at the lowest eligible API cost."
                if improved
                else "No fully observed cascade strictly improves macro-F1; retain local inference."
            ),
            "eligible_cascade_count": len(eligible),
            "improving_cascade_count": len(improved),
            "statistical_significance": "Not assessed; observed differences are not causal claims.",
        },
        "best_observed_nonzero_band": (
            {
                "strategy_id": best_observed["strategy_id"],
                "band": best_observed["band"],
                "macro_f1": _quality(best_observed),
                "known_cost_yuan": best_observed["cost"]["known_cost_yuan"],
                "selection_rule": "Highest observed macro-F1, then lowest cost and narrowest band.",
                "scope": "Complete cache replay candidate only; requires independent evaluation.",
            }
            if best_observed is not None
            else None
        ),
        "interpretation": (
            "Completed-only metrics describe returned predictions, not the full cohort. "
            "Conservative "
            "metrics retain every requested row and count abstentions as true-class "
            "false negatives. "
            "Costs per 1000 always divide by requested rows, never only successes."
        ),
    }
