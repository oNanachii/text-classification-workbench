"""Keep a durable CNY cap for validation-only calls without storing SMS or credentials."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
from typing import Any
from zoneinfo import ZoneInfo

MODEL = "deepseek-flash"
PRICING_URL = "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/"
PRICING_CHECKED_DATE = "2026-11-01"
PEAK_RATES = {"cache_hit": "0.04", "cache_miss": "2", "output": "8"}
OFFPEAK_RATES = {"cache_hit": "0.02", "cache_miss": "1", "output": "4"}
MILLION = Decimal(1_000_000)
MAX_CAP_YUAN = Decimal("1")
FRAMING_TOKEN_ALLOWANCE = 4096
MAX_REQUESTED_OUTPUT_TOKENS = 4096
HEURISTIC_BYTES_PER_TOKEN = 4
PRIVATE_FILE_MODE = 0o600
SCHEMA_VERSION = 1
BEIJING = ZoneInfo("Asia/Shanghai")
IDENTIFIER = re.compile(r"[A-Za-z0-9_.:-]{1,256}\Z")
SHA256 = re.compile(r"[a-f0-9]{64}\Z")
RESULT_IDENTIFIERS = (
    "requested_model",
    "model",
    "provider_id",
    "request_id",
    "response_id",
    "system_fingerprint",
    "error_code",
    "finish_reason",
)
HOLIDAY_POLICY = "Weekday holidays conservatively use peak rates; no holiday calendar lookup"
ESTIMATE_POLICY = (
    "UTF-8 canonical request bytes plus 4096 framing tokens is a conservative assumption, "
    "not a provider-guaranteed tokenizer bound; reservations use peak cache-miss prices"
)


class BudgetStopped(RuntimeError):
    """Stop dispatch after accounting assumptions fail so the ledger can be inspected."""


class ReservationExceeded(BudgetStopped):
    """Report a durably recorded request whose usage exceeded its initial reservation."""


def _decimal(value: Any) -> Decimal:
    """Validate finite nonnegative amounts before using them in exact cap arithmetic."""
    if isinstance(value, bool):
        raise ValueError("Money must be a finite nonnegative decimal")
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("Money must be a finite nonnegative decimal") from None
    if not amount.is_finite() or amount < 0:
        raise ValueError("Money must be a finite nonnegative decimal")
    return amount


def _canonical(value: Any) -> bytes:
    """Serialize request identities consistently without saving the request body."""
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _validated_usage(usage: dict[str, Any]) -> dict[str, int]:
    """Check token counts so malformed usage cannot release a cost reservation."""
    if not isinstance(usage, dict):
        raise ValueError("Missing or invalid provider usage")
    required = ("prompt_tokens", "completion_tokens", "total_tokens", "prompt_cache_hit_tokens")
    if any(type(usage.get(name)) is not int or usage[name] < 0 for name in required):
        raise ValueError("Incomplete or invalid token counts")
    prompt = usage["prompt_tokens"]
    hit = usage["prompt_cache_hit_tokens"]
    if usage["total_tokens"] != prompt + usage["completion_tokens"] or hit > prompt:
        raise ValueError("Inconsistent token counts")
    miss = usage.get("prompt_cache_miss_tokens", prompt - hit)
    if type(miss) is not int or miss < 0 or hit + miss != prompt:
        raise ValueError("Inconsistent cache token counts")
    return {name: usage[name] for name in required} | {"prompt_cache_miss_tokens": miss}


def price_usage(usage: dict[str, Any], rates: dict[str, str]) -> Decimal:
    """Price validated token usage exactly so cap comparisons do not round through floats."""
    clean = _validated_usage(usage)
    hit_rate = _decimal(rates["cache_hit"])
    miss_rate = _decimal(rates["cache_miss"])
    output_rate = _decimal(rates["output"])
    return (
        Decimal(clean["prompt_cache_hit_tokens"]) * hit_rate
        + Decimal(clean["prompt_cache_miss_tokens"]) * miss_rate
        + Decimal(clean["completion_tokens"]) * output_rate
    ) / MILLION


def tariff_rates(started_iso: str) -> dict[str, str]:
    """Use Beijing time and conservatively price weekday holidays without a calendar lookup."""
    started = datetime.fromisoformat(started_iso)
    if started.tzinfo is None:
        raise ValueError("Request timestamps must include a timezone")
    beijing = started.astimezone(BEIJING)
    peak = beijing.weekday() < 5 and (9 <= beijing.hour < 12 or 14 <= beijing.hour < 18)
    return dict(PEAK_RATES if peak else OFFPEAK_RATES)


def estimate_request(payload: dict[str, Any]) -> dict[str, Any]:
    """Estimate dry-run size and reserve peak cost using an explicit generous token assumption."""
    if not isinstance(payload, dict) or payload.get("model") != MODEL:
        raise ValueError("Only the verified deepseek-flash price schedule is supported")
    maximum = payload.get("max_tokens")
    if type(maximum) is not int or not 1 <= maximum <= MAX_REQUESTED_OUTPUT_TOKENS:
        raise ValueError("Comparison requests require max_tokens in 1..4096")
    content = _canonical(payload)
    bound = len(content) + FRAMING_TOKEN_ALLOWANCE
    reserve = (
        Decimal(bound) * Decimal(PEAK_RATES["cache_miss"])
        + Decimal(maximum) * Decimal(PEAK_RATES["output"])
    ) / MILLION
    return {
        "estimated_input_tokens": math.ceil(len(content) / HEURISTIC_BYTES_PER_TOKEN),
        "estimate_method": "UTF-8 JSON bytes / 4; heuristic, not tokenizer",
        "request_utf8_bytes": len(content),
        "conservative_input_tokens": bound,
        "max_output_tokens": maximum,
        "reservation_peak_yuan": str(reserve),
        "reservation_policy": ESTIMATE_POLICY,
    }


def _sanitized_result(result: dict[str, Any]) -> dict[str, Any]:
    """Keep typed comparison fields while dropping provider bodies, messages and arbitrary text."""
    if not isinstance(result, dict):
        raise ValueError("Comparison results must be an object")
    status = result.get("status")
    status = status if isinstance(status, str) and status in {"ok", "error", "unknown"} else "error"
    label = result.get("label")
    label = label if isinstance(label, str) and label in {"ham", "spam"} else None
    if status == "ok" and label is None:
        status = "unknown"
    clean: dict[str, Any] = {
        "status": status,
        "label": label,
        "failure": result.get("failure", False) is not False or status != "ok" or label is None,
    }
    for name in RESULT_IDENTIFIERS:
        value = result.get(name)
        if isinstance(value, str) and IDENTIFIER.fullmatch(value):
            clean[name] = value
    elapsed = result.get("elapsed_ms")
    if type(elapsed) in (int, float) and math.isfinite(elapsed) and elapsed >= 0:
        clean["elapsed_ms"] = elapsed
    http_status = result.get("http_status")
    if type(http_status) is int and 100 <= http_status <= 599:
        clean["http_status"] = http_status
    return clean


class Ledger:
    """Track single dispatches and cost exposure; the caller holds the evaluation process lock."""

    def __init__(self, path: Path, cap_yuan: Decimal, identity_sha256: str) -> None:
        """Open matching state so restarts cannot reset the cap or retry rows."""
        self.path = Path(path)
        self.cap = _decimal(cap_yuan)
        if not 0 < self.cap <= MAX_CAP_YUAN:
            raise ValueError("Comparison spending cap must be in (0, 1] CNY")
        if not isinstance(identity_sha256, str) or not SHA256.fullmatch(identity_sha256):
            raise ValueError("Comparison identity must be a SHA-256 digest")
        self.identity_sha256 = identity_sha256
        self.records: dict[str, dict[str, Any]] = {}
        self.stopped = False
        self.stop_reason: str | None = None
        if self.path.is_symlink():
            raise ValueError("Budget ledger must not be a symlink")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            self._load()
        else:
            self._save()

    def _load(self) -> None:
        """Validate persisted identity and amounts before trusting previous spending exposure."""
        value = json.loads(self.path.read_text(encoding="utf-8"))
        if (
            value.get("schema_version") != SCHEMA_VERSION
            or value.get("identity_sha256") != self.identity_sha256
            or _decimal(value.get("cap_yuan")) != self.cap
            or not isinstance(value.get("records"), dict)
        ):
            raise ValueError("Existing comparison ledger configuration differs")
        self.records = value["records"]
        for identity, record in self.records.items():
            if not isinstance(record, dict) or record.get("sample_id") != identity:
                raise ValueError("Invalid persisted dispatch record")
            _decimal(record.get("reserved_yuan"))
            for field in ("cost_yuan", "conservative_peak_yuan"):
                if record.get(field) is not None:
                    _decimal(record[field])
            if (record.get("cost_yuan") is None) != (record.get("conservative_peak_yuan") is None):
                raise ValueError("Incomplete persisted usage accounting")
        self.stopped = bool(value.get("stopped"))
        self.stop_reason = value.get("stop_reason")
        if _decimal(self.summary()["cap_exposure_yuan"]) > self.cap and not self.stopped:
            raise ValueError("Persisted accounting exceeds the cap without a recorded stop")

    def _save(self) -> None:
        """Fsync and replace a private complete ledger before dispatch or after accounting."""
        value = {
            "schema_version": SCHEMA_VERSION,
            "identity_sha256": self.identity_sha256,
            "cap_yuan": str(self.cap),
            "pricing_url": PRICING_URL,
            "pricing_checked_date": PRICING_CHECKED_DATE,
            "cost_is_tariff_estimate": True,
            "holiday_policy": HOLIDAY_POLICY,
            "reservation_policy": ESTIMATE_POLICY,
            "stopped": self.stopped,
            "stop_reason": self.stop_reason,
            "records": self.records,
            "summary": self.summary(),
        }
        descriptor, staging = tempfile.mkstemp(prefix=".comparison-budget-", dir=self.path.parent)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(_canonical(value) + b"\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(staging, PRIVATE_FILE_MODE)
            os.replace(staging, self.path)
            directory = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(staging):
                os.unlink(staging)

    def reserve(self, sample_id: str, payload: dict[str, Any]) -> bool:
        """Persist one dispatch before sending, refusing duplicates or exposure above the cap."""
        if not isinstance(sample_id, str) or not IDENTIFIER.fullmatch(sample_id):
            raise ValueError("Sample identity must be a bounded identifier")
        if sample_id in self.records:
            raise ValueError("Sample was already dispatched; retries are forbidden")
        if self.stopped:
            raise BudgetStopped("Comparison accounting has stopped; inspect the ledger")
        estimate = estimate_request(payload)
        reserved = _decimal(estimate["reservation_peak_yuan"])
        if _decimal(self.summary()["cap_exposure_yuan"]) + reserved > self.cap:
            return False
        self.records[sample_id] = {
            "sample_id": sample_id,
            "request_sha256": hashlib.sha256(_canonical(payload)).hexdigest(),
            "estimate": estimate,
            "started_at_utc": datetime.now(timezone.utc).isoformat(),
            "status": "dispatching",
            "label": None,
            "failure": False,
            "reserved_yuan": str(reserved),
            "cost_yuan": None,
            "conservative_peak_yuan": None,
            "usage": None,
        }
        self._save()
        return True

    def finish(self, sample_id: str, result: dict[str, Any]) -> dict[str, Any]:
        """Account valid usage even for failed predictions; keep unknown charges fully reserved."""
        if sample_id not in self.records:
            raise ValueError("Sample has no durable dispatch reservation")
        previous = self.records[sample_id]
        if previous["status"] != "dispatching":
            raise ValueError("Dispatch is already finished; retries and replacement are forbidden")
        record = {**previous, **_sanitized_result(result)}
        record["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
        try:
            usage = _validated_usage(result.get("usage"))
        except ValueError:
            usage = None
        if usage is None:
            record.update(usage=None, status="unknown", failure=True)
        else:
            rates = tariff_rates(record["started_at_utc"])
            record.update(
                usage=usage,
                tariff_rates_yuan_per_million=rates,
                cost_yuan=str(price_usage(usage, rates)),
                conservative_peak_yuan=str(price_usage(usage, PEAK_RATES)),
            )
        self.records[sample_id] = record
        exceeded = record["conservative_peak_yuan"] is not None and _decimal(
            record["conservative_peak_yuan"]
        ) > _decimal(record["reserved_yuan"])
        if exceeded:
            self.stopped = True
            self.stop_reason = "provider_usage_exceeded_reservation"
            record["reservation_exceeded"] = True
        self._save()
        if exceeded:
            raise ReservationExceeded(
                "Provider usage exceeded the reserved amount; dispatch stopped"
            )
        return record

    def recover_interrupted(self) -> None:
        """Keep abandoned dispatches reserved and unknown so restarts never retry them."""
        changed = False
        for record in self.records.values():
            if record["status"] == "dispatching":
                record.update(status="unknown", failure=True, error_code="interrupted_no_retry")
                changed = True
        if changed:
            self._save()

    def summary(self) -> dict[str, Any]:
        """Separate tariff estimates from conservative liability so the cap stays auditable."""
        known = [r for r in self.records.values() if r.get("conservative_peak_yuan") is not None]
        unknown = [r for r in self.records.values() if r.get("conservative_peak_yuan") is None]
        cost = sum((_decimal(r["cost_yuan"]) for r in known), Decimal(0))
        peak = sum((_decimal(r["conservative_peak_yuan"]) for r in known), Decimal(0))
        reserved = sum((_decimal(r["reserved_yuan"]) for r in unknown), Decimal(0))
        return {
            "known_cost_yuan": str(cost),
            "conservative_peak_yuan": str(peak),
            "unknown_reserved_yuan": str(reserved),
            "cap_exposure_yuan": str(peak + reserved),
            "cap_yuan": str(self.cap),
            "attempt_count": len(self.records),
            "known_usage_count": len(known),
            "unknown_usage_count": len(unknown),
            "stopped": self.stopped,
            "stop_reason": self.stop_reason,
        }
