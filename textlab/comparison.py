"""Develop an ID-only validation comparison in a separate, budgeted workspace."""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from typing import Any

import httpx

from textlab import llm_provider
from textlab.comparison_analysis import summarize
from textlab.comparison_budget import Ledger, estimate_request
from textlab.safe_model import SafeModel

PROJECT = Path(__file__).resolve().parents[1]
FORMAL = PROJECT / "var"
ARTIFACTS = PROJECT / "artifacts/llm-comparison"
REPORT = PROJECT / "docs/LLM_VS_LOCAL.md"
PUBLIC_URL = "https://textlab.madokanun.top"
VALIDATION_COUNT = 783
THRESHOLD = 0.5
CONCURRENCY = 4
PROGRESS_INTERVAL = 25
MILLISECONDS_PER_SECOND = 1000
PEAK_RATES = {"cache_hit": "0.04", "cache_miss": "2", "output": "8"}
OFFPEAK_RATES = {"cache_hit": "0.02", "cache_miss": "1", "output": "4"}
BANDS = [[0.45, 0.55], [0.4, 0.6], [0.3, 0.7], [0.2, 0.8], [0.1, 0.9], [0.0, 1.0]]


def canonical(value: Any) -> bytes:
    """Serialize evidence consistently without introducing message-bearing log fields."""
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode()


def sha(content: bytes) -> str:
    """Identify inputs by digest so public evidence need not contain SMS text."""
    return hashlib.sha256(content).hexdigest()


def write_json(path: Path, value: Any) -> None:
    """Publish complete evidence atomically so interrupted runs retain usable records."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".comparison-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(
                json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False).encode() + b"\n"
            )
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def protect_formal_files(workspace: Path, allowed: set[Path]) -> dict[str, int]:
    """Deny all other formal-data opens, including every sealed-test or exclusion file."""
    evidence = {"heldout_test_files_opened": 0, "blocked_formal_accesses": 0}
    mutations = {
        "os.remove",
        "os.rename",
        "os.mkdir",
        "os.rmdir",
        "os.chmod",
        "os.chown",
        "os.link",
        "os.symlink",
        "os.truncate",
        "os.utime",
    }

    def formal_path(value: Any) -> Path | None:
        if not isinstance(value, (str, bytes, os.PathLike)):
            return None
        path = Path(os.fsdecode(value)).resolve()
        return path if path.is_relative_to(FORMAL) else None

    def audit(event: str, args: tuple[Any, ...]) -> None:
        if event == "open":
            path = formal_path(args[0])
            if path is None or path.is_relative_to(workspace):
                return
            flags = args[2] or 0
            writes = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
            if path not in allowed or flags & writes:
                evidence["blocked_formal_accesses"] += 1
                raise PermissionError("Comparison may only read its formal input allowlist")
        elif event in mutations:
            for value in args[:2]:
                path = formal_path(value)
                if path is not None and not path.is_relative_to(workspace):
                    evidence["blocked_formal_accesses"] += 1
                    raise PermissionError("Comparison may not mutate formal input files")

    sys.addaudithook(audit)
    return evidence


def fingerprints(paths: set[Path]) -> dict[str, Any]:
    """Record only explicitly permitted inputs, never traverse the formal workspace."""
    result = {}
    for path in sorted(paths):
        stat = path.stat()
        result[str(path.relative_to(PROJECT))] = {
            "sha256": sha(path.read_bytes()),
            "mode": stat.st_mode,
            "mtime_ns": stat.st_mtime_ns,
            "size": stat.st_size,
        }
    return result


def read_validation(path: Path, expected_count: int = VALIDATION_COUNT) -> tuple[dict, list]:
    """Select only validation rows from the v2 dataset and reject ambiguous identities."""
    dataset = json.loads(path.read_text())
    rows = dataset["validation"]
    if len(rows) != expected_count or len({row["id"] for row in rows}) != len(rows):
        raise ValueError("Validation count or identity mismatch")
    for row in rows:
        if row["label"] not in {"ham", "spam"} or sha(row["text"].encode()) != row["id"]:
            raise ValueError("Invalid validation identity or label")
    if set(row["label"] for row in rows) != {"ham", "spam"}:
        raise ValueError("Validation must contain both task classes")
    return dataset["manifest"], rows


def local_results(model: SafeModel, validation: list[dict]) -> list[dict]:
    """Measure single-row numeric inference with the selected frozen model and threshold."""
    model.scores([validation[0]["text"]])
    result = []
    for row in validation:
        started = time.perf_counter()
        score = float(model.scores([row["text"]])[0])
        elapsed = (time.perf_counter() - started) * MILLISECONDS_PER_SECOND
        result.append(
            {
                "sample_id": row["id"],
                "truth": row["label"],
                "local": {
                    "status": "ok",
                    "label": "spam" if score >= THRESHOLD else "ham",
                    "spam_score": score,
                    "elapsed_ms": elapsed,
                },
            }
        )
    return result


def online_model_check(selected: dict) -> dict:
    """Verify the public loaded artifact before calling it the current online model."""
    with httpx.Client(trust_env=False, timeout=30, follow_redirects=False) as client:
        loaded = client.get(PUBLIC_URL + "/api/health").raise_for_status().json()["loaded"]
    if selected != loaded:
        raise ValueError("Local selection differs from the model loaded on the public service")
    return {
        "status": "passed",
        "url": PUBLIC_URL,
        "loaded": loaded,
        "checked_at_utc": datetime.now(timezone.utc).isoformat(),
    }


def read_key(path: Path | None) -> str:
    """Read one named credential into memory without emitting or persisting its value."""
    key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not key and path is not None:
        for line in path.read_text().splitlines():
            name, separator, value = line.partition("=")
            if separator and name.strip() == "DEEPSEEK_API_KEY":
                key = value.strip().strip('"').strip("'")
                break
    if not key:
        raise ValueError("Configure DEEPSEEK_API_KEY before the explicitly requested live run")
    return key


def prepare(workspace: Path, verify_online: bool) -> tuple[dict, list, dict, set[Path]]:
    """Freeze validation, model and request identities before any paid dispatch."""
    allowed = {(FORMAL / "dataset.json").resolve(), (FORMAL / "service.json").resolve()}
    audit = protect_formal_files(workspace, allowed)
    selected = json.loads((FORMAL / "service.json").read_text())["selected"]
    if selected["kind"] != "mlp16" or selected["threshold"] != THRESHOLD:
        raise ValueError("This comparison requires the existing selected mlp16 at threshold 0.5")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,95}", selected["run_id"]):
        raise ValueError("Invalid selected run identity")
    directory = FORMAL / "runs" / selected["run_id"]
    allowed.update(
        (directory / name).resolve() for name in ("model.json", "weights.npz", "vocabulary.json")
    )
    before = fingerprints(allowed)
    manifest, validation = read_validation(FORMAL / "dataset.json")
    model = SafeModel(directory, "mlp16")
    for field in ("weights_sha256", "vocabulary_sha256", "dataset_id", "snapshot_id"):
        if selected[field] != model.metadata[field]:
            raise ValueError("Selected model artifact changed")
    old_plan_path = workspace / "plan.json"
    old_plan = json.loads(old_plan_path.read_text()) if old_plan_path.exists() else None
    online = (
        online_model_check(selected)
        if verify_online
        else (old_plan["online_model_check"] if old_plan else {"status": "not_requested"})
    )
    source_names = (
        "comparison.py",
        "comparison_budget.py",
        "comparison_analysis.py",
        "llm_provider.py",
        "safe_model.py",
        "data.py",
    )
    source_hashes = {
        "textlab/" + name: sha((PROJECT / "textlab" / name).read_bytes()) for name in source_names
    }
    parameters = llm_provider.build_payload(validation[0]["text"])
    parameters.pop("messages")
    estimates = [estimate_request(llm_provider.build_payload(row["text"])) for row in validation]
    estimated_input = sum(item["estimated_input_tokens"] for item in estimates)
    estimated_output = VALIDATION_COUNT * llm_provider.MAX_OUTPUT_TOKENS
    estimated_peak = (
        Decimal(estimated_input) * Decimal(PEAK_RATES["cache_miss"])
        + Decimal(estimated_output) * Decimal(PEAK_RATES["output"])
    ) / 1_000_000
    config = {
        "schema_version": 1,
        "validation_count": len(validation),
        "validation_sha256": sha(canonical(validation)),
        "dataset_id": manifest["dataset_id"],
        "class_counts": dict(Counter(row["label"] for row in validation)),
        "local_model": selected,
        "bands": BANDS,
        "concurrency": CONCURRENCY,
        "timeout_seconds": llm_provider.DEFAULT_TIMEOUT_SECONDS,
        "local_warm_up_count": 1,
        "local_latency_scope": "Single-row numeric inference; excludes HTTP and model loading",
        "deepseek": {
            "model": llm_provider.MODEL,
            "endpoint": llm_provider.ENDPOINT,
            "system_prompt": llm_provider.SYSTEM_PROMPT,
            "prompt_sha256": llm_provider.PROMPT_SHA256,
            "payload_parameters": parameters,
        },
        "price_schedule": {
            "peak": PEAK_RATES,
            "offpeak": OFFPEAK_RATES,
            "source_url": "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/",
            "verified_date": "2026-11-01",
        },
        "source_sha256": source_hashes,
        "jev_status": "not_run; optional provider excluded to retain one CNY budget and scope",
    }
    identity = sha(canonical(config))
    if old_plan and old_plan["identity_sha256"] != identity:
        raise ValueError("Existing experiment configuration differs; never replace its evidence")
    plan = {
        **config,
        "identity_sha256": identity,
        "online_model_check": online,
        "protected_input_fingerprints": before,
        "dry_run": {
            "heuristic_peak_cost_yuan": str(estimated_peak),
            "heuristic_offpeak_cost_yuan": str(estimated_peak / 2),
            "estimated_input_tokens": estimated_input,
            "max_output_tokens_total": estimated_output,
            "method": "UTF-8 JSON bytes / 4 heuristic; max 32 output tokens per SMS; "
            "all input treated as cache misses. Not a tokenizer guarantee.",
            "reservation_method": "Per in-flight request: UTF-8 JSON bytes + 4096 "
            "input tokens plus output limit at peak rates",
        },
    }
    if not workspace.exists():
        workspace.mkdir(parents=True, mode=0o700)
    write_json(workspace / "plan.json", plan)
    local_path = workspace / "local-results.json"
    results = (
        json.loads(local_path.read_text())
        if local_path.exists()
        else local_results(model, validation)
    )
    if [row["sample_id"] for row in results] != [row["id"] for row in validation]:
        raise ValueError("Cached local identities differ from the frozen validation order")
    write_json(local_path, results)
    return plan, validation, audit, allowed


async def dispatch(validation: list[dict], ledger: Ledger, api_key: str) -> None:
    """Dispatch each identity once with durable reservations and a shared no-retry budget."""
    ledger.recover_interrupted()
    pending = iter(row for row in validation if row["id"] not in ledger.records)
    stop_reason = None
    finished = len(ledger.records)
    async with httpx.AsyncClient(
        trust_env=False, follow_redirects=False, limits=httpx.Limits(max_connections=CONCURRENCY)
    ) as client:

        async def worker() -> None:
            nonlocal finished, stop_reason
            while stop_reason is None:
                row = next(pending, None)
                if row is None:
                    return
                payload = llm_provider.build_payload(row["text"])
                if not ledger.reserve(row["id"], payload):
                    stop_reason = "budget_reservation_would_exceed_cap"
                    return
                result = await llm_provider.classify(client, api_key, row["text"])
                ledger.finish(row["id"], result)
                finished += 1
                if result.get("http_status") in {400, 401, 402, 403, 404}:
                    stop_reason = "provider_configuration_or_credential_rejected"
                if finished % PROGRESS_INTERVAL == 0 or finished == VALIDATION_COUNT:
                    print(
                        json.dumps({"completed_attempts": finished, "budget": ledger.summary()}),
                        flush=True,
                    )

        await asyncio.gather(*(worker() for _ in range(CONCURRENCY)))
    if stop_reason:
        print(json.dumps({"dispatch_stopped": stop_reason}), flush=True)


def publish(
    workspace: Path, plan: dict, ledger: Ledger | None, integrity: dict | None = None
) -> dict:
    """Recompute ID-only artifacts and the report without reissuing cached requests."""
    local_path = workspace / "local-results.json"
    if ledger is None:
        rows = [json.loads(line) for line in (workspace / "results.jsonl").read_text().splitlines()]
        budget = json.loads((workspace / "budget.json").read_text())
    else:
        local = json.loads(local_path.read_text())
        rows = [
            {
                **row,
                "deepseek": ledger.records.get(
                    row["sample_id"],
                    {
                        "status": "not_attempted",
                        "label": None,
                        "usage": None,
                        "elapsed_ms": None,
                        "cost_yuan": None,
                        "reserved_yuan": "0",
                        "failure": False,
                    },
                ),
            }
            for row in local
        ]
        budget = ledger.summary()
        budget["usage"] = dict(
            sum((Counter(row["deepseek"].get("usage") or {}) for row in rows), Counter())
        )
        budget["returned_models"] = sorted(
            {row["deepseek"]["model"] for row in rows if row["deepseek"].get("model")}
        )
        budget["system_fingerprints"] = sorted(
            {
                row["deepseek"]["system_fingerprint"]
                for row in rows
                if row["deepseek"].get("system_fingerprint")
            }
        )
        budget["data_integrity"] = integrity
    summary = summarize(rows)
    summary["budget"] = budget
    summary["validation_sha256"] = plan["validation_sha256"]
    summary["configuration_sha256"] = plan["identity_sha256"]
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    for destination in {workspace, ARTIFACTS}:
        write_json(destination / "plan.json", plan)
        write_json(destination / "summary.json", summary)
        write_json(destination / "budget.json", budget)
        content = b"\n".join(canonical(row) for row in rows) + b"\n"
        (destination / "results.jsonl").write_bytes(content)
        if ledger is not None and destination != workspace:
            write_json(
                destination / "ledger.json",
                {"identity_sha256": plan["identity_sha256"], "records": ledger.records},
            )
    from textlab.comparison_report import render_report

    render_report(summary, plan, budget, docs_path=REPORT, artifact_dir=ARTIFACTS)
    return summary


def main() -> None:
    """Dry-run first, then explicitly run or replay one independently rooted comparison."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--replay", action="store_true")
    parser.add_argument("--verify-online", action="store_true")
    parser.add_argument("--cap-yuan", type=Decimal, default=Decimal("1"))
    parser.add_argument("--key-file", type=Path)
    args = parser.parse_args()
    workspace = args.root.resolve()
    if workspace == FORMAL.resolve() or FORMAL.resolve().is_relative_to(workspace):
        parser.error("--root must be a new independent workspace, never the formal root")
    if workspace.exists() and not (workspace / "plan.json").exists():
        parser.error("Existing unrelated workspace cannot be used or overwritten")
    if args.replay and args.live:
        parser.error("Replay never issues live requests")
    if not args.cap_yuan.is_finite() or not 0 < args.cap_yuan <= 1:
        parser.error("The authorized cap must be in (0, 1] CNY")
    if args.replay:
        plan = json.loads((workspace / "plan.json").read_text())
        summary = publish(workspace, plan, None)
        print(
            json.dumps(
                {
                    "mode": "cached_replay",
                    "network_requests": 0,
                    "recommendation": summary["recommendation"],
                }
            )
        )
        return
    if not workspace.exists():
        workspace.mkdir(parents=True, mode=0o700)
    with (workspace / ".run.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        plan, validation, audit, allowed = prepare(workspace, args.verify_online)
        print(
            json.dumps(
                {
                    "mode": "dry_run",
                    "rows": len(validation),
                    "estimate": plan["dry_run"],
                    "cap_yuan": str(args.cap_yuan),
                }
            ),
            flush=True,
        )
        if not args.live:
            return
        if plan["online_model_check"]["status"] != "passed":
            parser.error("The first live comparison requires --verify-online")
        if Decimal(plan["dry_run"]["heuristic_peak_cost_yuan"]) > args.cap_yuan:
            parser.error("Dry-run projected peak cost exceeds the cap; no requests sent")
        ledger = Ledger(workspace / "ledger.json", args.cap_yuan, plan["identity_sha256"])
        key = read_key(args.key_file)
        interrupted = False
        try:
            asyncio.run(dispatch(validation, ledger, key))
        except (Exception, KeyboardInterrupt):
            interrupted = True
            ledger.recover_interrupted()
        finally:
            key = ""
        unchanged = fingerprints(allowed) == plan["protected_input_fingerprints"]
        if not unchanged:
            raise ValueError("A formal input file changed during the comparison")
        integrity = {**audit, "protected_files_unchanged": unchanged}
        summary = publish(workspace, plan, ledger, integrity)
        if interrupted:
            raise RuntimeError(
                "Comparison interrupted; partial evidence and unknown reservations were saved; "
                "dispatched samples must never be retried"
            ) from None
    print(
        json.dumps(
            {
                "status": "completed",
                "budget": summary["budget"],
                "recommendation": summary["recommendation"],
            }
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
