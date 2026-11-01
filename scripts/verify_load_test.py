"""Recompute saved load-test evidence with stdlib only, without models or network calls."""

from __future__ import annotations

import argparse
import ast
from collections import Counter
import csv
from datetime import datetime, timezone
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from typing import Any

PROJECT = Path(__file__).resolve().parents[1]
EXECUTION_COMMIT = "2793a8dcabba93664f8d7cb0922b95ca044b455c"
SOURCE_FILES = (
    "textlab/load_test.py",
    "textlab/load_test_server.py",
    "textlab/app_v2.py",
    "textlab/safe_model.py",
    "textlab/dataflow.py",
    "textlab/data.py",
    "textlab/experiments.py",
    "textlab/decision.py",
)
FIXTURE_FILE = "examples/load_test_sms.json"
UNIT_FILE = "deploy/text-workbench.service"
SCENARIOS = (1, 10, 50)
DURATION_SECONDS = 60.0
BYTES_PER_MIB = 1_048_576
TRACE_COLUMNS = ("worker", "fixture_id", "start_seconds", "end_seconds", "status", "error_kind")
ERROR_KEYS = (
    "capacity_busy",
    "minute_rate_limit",
    "other_429",
    "prediction_mismatch",
    "timeout",
    "transport_error",
)
HEX_DIGEST = re.compile(r"[a-f0-9]{64}\Z")
DECIMAL_INTEGER = re.compile(r"0|[1-9][0-9]*\Z")
RUN_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,95}\Z")


class EvidenceError(ValueError):
    """Reject inconsistent saved evidence instead of silently accepting a reported metric."""


class Checks:
    """Count explicit checks so the receipt states how saved evidence was assessed."""

    def __init__(self) -> None:
        """Start a counter without configuring application modules or external services."""
        self.count = 0

    def require(self, condition: bool, message: str) -> None:
        """Stop on an unsupported claim while keeping the error free of request bodies."""
        self.count += 1
        if not condition:
            raise EvidenceError(message)

    def equal(self, observed: Any, expected: Any, message: str) -> None:
        """Compare exact metadata without treating JSON booleans as integer counts."""
        if type(expected) is bool:
            valid = observed is expected
        elif type(expected) is int:
            valid = type(observed) is int and observed == expected
        else:
            valid = observed == expected
        self.require(valid, message)

    def close(self, observed: Any, expected: float | None, message: str) -> None:
        """Allow only numeric roundoff when comparing independently recomputed ratios."""
        if expected is None:
            self.require(observed is None, message)
            return
        valid = type(observed) in (int, float) and math.isfinite(observed)
        self.require(valid, message)
        self.require(math.isclose(observed, expected, rel_tol=1e-12, abs_tol=1e-9), message)


def digest(content: bytes) -> str:
    """Bind source and compressed traces to their exact bytes without printing their contents."""
    return hashlib.sha256(content).hexdigest()


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for name, value in pairs:
        if name in result:
            raise EvidenceError("JSON evidence contains duplicate keys")
        result[name] = value
    return result


def _invalid_number(_value: str) -> None:
    raise EvidenceError("JSON evidence contains a nonfinite number")


def read_json(path: Path) -> dict[str, Any]:
    """Parse strict saved metadata without accepting duplicate keys or nonfinite values."""
    value = json.loads(
        path.read_bytes(), object_pairs_hook=_unique_object, parse_constant=_invalid_number
    )
    if not isinstance(value, dict):
        raise EvidenceError("JSON evidence must contain an object")
    return value


def _number(value: Any, name: str, *, positive: bool = False) -> float:
    if type(value) not in (int, float) or not math.isfinite(value):
        raise EvidenceError(name + " must be a finite number")
    if value < 0 or (positive and value == 0):
        raise EvidenceError(name + " must be nonnegative or strictly positive as specified")
    return float(value)


def _integer(value: Any, name: str, *, positive: bool = False) -> int:
    if type(value) is not int or value < 0 or (positive and value == 0):
        raise EvidenceError(name + " must be a nonnegative integer")
    return value


def _csv_integer(value: Any, name: str) -> int:
    if not isinstance(value, str) or DECIMAL_INTEGER.fullmatch(value) is None:
        raise EvidenceError(name + " is not an integer trace field")
    return int(value)


def _csv_number(value: Any, name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        raise EvidenceError(name + " is not a numeric trace field") from None
    return _number(number, name)


def deny_formal_opens(project: Path) -> dict[str, int]:
    """Fail closed on accidental formal-data opens; verification requires no model or SMS files."""
    formal = project / "var"
    evidence = {"blocked_formal_opens": 0}

    def audit(event: str, args: tuple[Any, ...]) -> None:
        if event != "open" or not isinstance(args[0], (str, bytes, os.PathLike)):
            return
        path = Path(os.fsdecode(args[0])).resolve()
        if path.is_relative_to(formal):
            evidence["blocked_formal_opens"] += 1
            raise PermissionError("Load-test verification never opens formal data or models")

    sys.addaudithook(audit)
    return evidence


def _git_blob(project: Path, relative: str) -> bytes:
    return subprocess.check_output(["git", "show", EXECUTION_COMMIT + ":" + relative], cwd=project)


def _global_literal(source: bytes, name: str) -> Any:
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name for target in node.targets
        ):
            return ast.literal_eval(node.value)
    raise EvidenceError("Required production or wrapper constant is missing")


def verify_sources(result: dict, project: Path, checks: Checks) -> tuple[set[str], dict]:
    """Verify the exact execution commit, current code and authored fixture against Git blobs."""
    checks.equal(result["source_commit"], EXECUTION_COMMIT, "Execution commit is not pinned")
    captured = result["source_sha256"]
    checks.equal(set(captured), set(SOURCE_FILES), "Execution source allowlist differs")
    current = {}
    for relative in SOURCE_FILES:
        checks.require(
            isinstance(captured[relative], str)
            and HEX_DIGEST.fullmatch(captured[relative]) is not None,
            "Execution source digest is invalid",
        )
        content = (project / relative).read_bytes()
        committed = _git_blob(project, relative)
        checks.equal(content, committed, "Execution source differs from the committed version")
        checks.equal(
            digest(content), captured[relative], "Captured execution source digest differs"
        )
        current[relative] = content
    production = current["textlab/app_v2.py"]
    checks.equal(
        _global_literal(production, "PREDICTIONS_PER_MINUTE"), 30, "Production minute rate changed"
    )
    checks.equal(
        _global_literal(production, "PREDICTION_CONCURRENCY"),
        2,
        "Production prediction slots changed",
    )
    checks.equal(
        _global_literal(production, "PREDICTION_RATE_WINDOW_SECONDS"),
        60,
        "Production rate window changed",
    )
    wrapper = current["textlab/load_test_server.py"]
    checks.equal(
        _global_literal(wrapper, "LOAD_TEST_REQUESTS_PER_MINUTE"),
        1_000_000_000,
        "Test wrapper minute rate changed",
    )
    checks.equal(
        _global_literal(wrapper, "PRODUCTION_PREDICTION_SLOTS"),
        2,
        "Test wrapper prediction slots changed",
    )
    checks.equal(
        result["workload"]["fixture_file"],
        FIXTURE_FILE,
        "Fixture path is outside the authored fixture allowlist",
    )
    fixture_content = (project / FIXTURE_FILE).read_bytes()
    checks.equal(
        fixture_content,
        _git_blob(project, FIXTURE_FILE),
        "Authored fixture differs from the execution commit",
    )
    checks.equal(
        digest(fixture_content),
        result["workload"]["fixture_sha256"],
        "Authored fixture checksum differs",
    )
    fixture = json.loads(fixture_content, object_pairs_hook=_unique_object)
    samples = fixture["samples"]
    identities = {row["id"] for row in samples}
    checks.equal(len(identities), len(samples), "Authored fixture identities are not unique")
    checks.require(
        all(isinstance(value, str) and value for value in identities),
        "Authored fixture identities are invalid",
    )
    lengths = [len(row["text"]) for row in samples]
    checks.equal(result["workload"]["fixture_count"], len(samples), "Fixture count differs")
    checks.equal(result["workload"]["text_min_chars"], min(lengths), "Fixture minimum differs")
    checks.equal(result["workload"]["text_max_chars"], max(lengths), "Fixture maximum differs")
    unit = (project / UNIT_FILE).read_bytes()
    checks.equal(unit, _git_blob(project, UNIT_FILE), "Production deployment template changed")
    for setting in (
        b"MemoryHigh=192M",
        b"MemoryMax=256M",
        b"CPUQuota=50%",
        b"TasksMax=64",
        b"LimitNOFILE=2048",
    ):
        checks.require(setting in unit.splitlines(), "Production resource setting differs")
    return identities, {
        "verified_source_files": len(SOURCE_FILES),
        "fixture_count": len(samples),
        "production_unit_sha256": digest(unit),
    }


def percentiles(values: list[float]) -> dict[str, float | None]:
    """Independently interpolate observed latencies at (N-1) times each reported quantile."""
    ordered = sorted(values)
    answer = {}
    for label, fraction in (("p50", 0.5), ("p95", 0.95), ("p99", 0.99)):
        if not ordered:
            answer[label] = None
            continue
        position = (len(ordered) - 1) * fraction
        lower = int(position)
        upper = min(lower + 1, len(ordered) - 1)
        answer[label] = ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)
    return answer


def _valid_status_error(status: int, error: str) -> bool:
    if status == 0:
        return error in {"timeout", "transport_error"}
    if status == 200:
        return error in {"", "prediction_mismatch"}
    if status == 429:
        return error in {"capacity_busy", "minute_rate_limit", "other_429"}
    return 100 <= status <= 599 and error == "http_" + str(status)


def verify_trace(case: dict, artifact_dir: Path, fixture_ids: set[str], checks: Checks) -> dict:
    """Recompute timing cohorts, errors and inflight bounds from the complete compressed trace."""
    concurrency = _integer(case["concurrency"], "Scenario concurrency", positive=True)
    checks.require(concurrency in SCENARIOS, "Unexpected concurrency scenario")
    duration = _number(case["duration_seconds"], "Scenario duration", positive=True)
    checks.close(duration, DURATION_SECONDS, "Every formal scenario must last sixty seconds")
    filename = "requests-c" + str(concurrency) + ".csv.gz"
    checks.equal(case["trace_file"], filename, "Trace path differs from its scenario")
    path = artifact_dir / filename
    checks.require(not path.is_symlink(), "Trace file must not be a symlink")
    checks.equal(digest(path.read_bytes()), case["trace_sha256"], "Compressed trace SHA256 differs")
    status_counts: Counter[str] = Counter()
    error_counts: Counter[str] = Counter()
    fixture_counts: Counter[str] = Counter()
    worker_counts: Counter[int] = Counter()
    previous_end: dict[int, float] = {}
    latencies = []
    successful_latencies = []
    events = []
    total = in_window = successful_in_window = drained = 0
    maximum_end = 0.0
    with gzip.open(path, "rt", encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream, strict=True)
        checks.equal(reader.fieldnames, list(TRACE_COLUMNS), "Trace columns differ")
        for row in reader:
            total += 1
            checks.equal(set(row), set(TRACE_COLUMNS), "Trace row has missing or extra fields")
            worker = _csv_integer(row["worker"], "Trace worker")
            status = _csv_integer(row["status"], "Trace HTTP status")
            begin = _csv_number(row["start_seconds"], "Trace start")
            end = _csv_number(row["end_seconds"], "Trace end")
            error = row["error_kind"]
            checks.require(0 <= worker < concurrency, "Trace worker is outside its scenario")
            checks.require(0 <= begin < duration and end >= begin, "Trace timing is invalid")
            checks.require(
                begin >= previous_end.get(worker, 0.0), "Closed-loop worker requests overlap"
            )
            checks.require(
                _valid_status_error(status, error), "Trace status/error combination differs"
            )
            checks.require(
                row["fixture_id"] in fixture_ids, "Trace used an unknown fixture identity"
            )
            previous_end[worker] = end
            worker_counts[worker] += 1
            fixture_counts[row["fixture_id"]] += 1
            status_counts[str(status)] += 1
            latency = (end - begin) * 1000.0
            latencies.append(latency)
            if error:
                error_counts[error] += 1
            else:
                successful_latencies.append(latency)
            if end <= duration:
                in_window += 1
                successful_in_window += int(not error)
            else:
                drained += 1
            maximum_end = max(maximum_end, end)
            if end > begin:
                events.extend(((begin, 1), (end, -1)))
    checks.require(total > 0, "Scenario contains no request observations")
    checks.equal(
        set(worker_counts), set(range(concurrency)), "Not every declared worker issued load"
    )
    checks.equal(set(fixture_counts), fixture_ids, "Not every authored fixture was exercised")
    checks.require(
        max(fixture_counts.values()) - min(fixture_counts.values()) <= 1,
        "Fixture distribution differs from round-robin issuing",
    )
    inflight = peak = 0
    for _timestamp, delta in sorted(events):
        inflight += delta
        checks.require(0 <= inflight <= concurrency, "Global inflight requests exceed concurrency")
        peak = max(peak, inflight)
    checks.equal(inflight, 0, "Trace leaves an unfinished inflight interval")
    successful = len(successful_latencies)
    expected_counts = {
        "issued_requests": total,
        "completed_requests": total,
        "completed_in_window": in_window,
        "successful_in_window": successful_in_window,
        "successful_requests": successful,
        "error_requests": total - successful,
        "drained_requests": drained,
    }
    for field, expected in expected_counts.items():
        checks.equal(case[field], expected, "Scenario count differs: " + field)
    checks.equal(
        case["status_counts"],
        {"0": 0, "200": 0, "429": 0} | dict(status_counts),
        "Scenario HTTP status counts differ",
    )
    checks.equal(
        case["error_counts"],
        dict.fromkeys(ERROR_KEYS, 0) | dict(error_counts),
        "Scenario error counts differ",
    )
    checks.equal(case["fixture_counts"], dict(fixture_counts), "Scenario fixture counts differ")
    checks.close(case["qps"], in_window / duration, "Completion-window QPS differs")
    checks.close(case["successful_qps"], successful_in_window / duration, "Successful QPS differs")
    checks.close(case["error_rate"], (total - successful) / total, "Full-cohort error rate differs")
    for category, values in (("all", latencies), ("successful", successful_latencies)):
        for percentile, expected in percentiles(values).items():
            checks.close(
                case["latency_ms"][category][percentile],
                expected,
                "Latency percentile differs: " + category + "/" + percentile,
            )
    wall = _number(case["wall_seconds"], "Scenario observed wall time", positive=True)
    checks.require(wall >= duration and wall >= maximum_end, "Wall time truncates load or drain")
    checks.close(case["drain_seconds"], wall - duration, "Drain duration differs")
    return {
        "concurrency": concurrency,
        "duration_seconds": duration,
        **expected_counts,
        "qps": in_window / duration,
        "successful_qps": successful_in_window / duration,
        "error_rate": (total - successful) / total,
        "maximum_inflight": peak,
        "status_counts": dict(status_counts),
        "error_counts": dict(error_counts),
        "successful_latency_ms": percentiles(successful_latencies),
        "all_latency_ms": percentiles(latencies),
    }


def verify_resources(case: dict, model: dict, versions: dict, checks: Checks) -> dict:
    """Check recorded PID, unit conversions and CPU deltas without remeasuring the OS."""
    pid = _integer(case["local_pid"], "Owned backend PID", positive=True)
    before = case["server_metrics_before"]
    after = case["server_metrics_after"]
    expected_limits = {
        "test_only": True,
        "host": "127.0.0.1",
        "public_read_only": True,
        "external_decision_enabled": False,
        "requests_per_minute": 1_000_000_000,
        "production_requests_per_minute": 30,
        "prediction_slots": 2,
        "run_id": model["run_id"],
    }
    for snapshot in (before, after):
        checks.equal(snapshot["pid"], pid, "Snapshot PID differs from the owned backend")
        for name, expected in expected_limits.items():
            checks.equal(snapshot["limits"][name], expected, "Backend limit differs: " + name)
        _integer(snapshot["thread_count"], "Recorded process thread count", positive=True)
        checks.require(
            isinstance(snapshot["thread_count_scope"], str),
            "Recorded thread-count source is missing",
        )
        checks.require(
            type(snapshot["threadpool_info_available"]) is bool,
            "Threadpool availability is not explicit",
        )
        pools = snapshot["threadpool_info"]
        checks.require(isinstance(pools, list), "Recorded threadpool metadata is invalid")
        if not snapshot["threadpool_info_available"]:
            checks.equal(pools, [], "Unavailable threadpool info cannot claim observations")
        for pool in pools:
            if "num_threads" in pool:
                _integer(pool["num_threads"], "Recorded BLAS thread count", positive=True)
            if pool.get("user_api") == "blas" and "num_threads" in pool:
                checks.equal(pool["num_threads"], 1, "Observed BLAS thread count exceeds one")
        backend = snapshot["numpy_backend"]
        checks.equal(backend["available"], True, "NumPy backend metadata is missing")
        checks.equal(backend["numpy_version"], versions["numpy"], "Backend NumPy version differs")
        checks.equal(
            backend["runtime_threads_verified"],
            False,
            "Build metadata alone must not claim runtime thread verification",
        )
    checks.equal(before["limits"], after["limits"], "Backend limits changed during the scenario")
    checks.equal(
        before["numpy_backend"],
        after["numpy_backend"],
        "NumPy build metadata changed during the scenario",
    )
    warm_peak = _integer(before["peak_rss_bytes"], "Warmup RSS peak", positive=True)
    final_peak = _integer(after["peak_rss_bytes"], "Lifecycle RSS peak", positive=True)
    checks.require(final_peak >= warm_peak, "OS high-water RSS decreased")
    checks.close(
        case["memory"]["server_warmup_peak_rss_mib"],
        warm_peak / BYTES_PER_MIB,
        "Warmup RSS unit conversion differs",
    )
    checks.close(
        case["memory"]["server_peak_rss_mib"],
        final_peak / BYTES_PER_MIB,
        "Lifecycle RSS unit conversion differs",
    )
    cpu_before = cpu_after = 0.0
    for field in ("user_cpu_seconds", "system_cpu_seconds"):
        earlier = _number(before[field], "Before CPU snapshot")
        later = _number(after[field], "After CPU snapshot")
        checks.require(later >= earlier, "Recorded CPU counter decreased")
        cpu_before += earlier
        cpu_after += later
    delta = cpu_after - cpu_before
    wall = _number(case["wall_seconds"], "Scenario wall time", positive=True)
    client = _number(case["cpu"]["client_cpu_seconds"], "Load-generator CPU time")
    checks.close(case["cpu"]["server_cpu_seconds"], delta, "Server CPU snapshot delta differs")
    checks.close(
        case["cpu"]["average_server_cores"], delta / wall, "Server average-core arithmetic differs"
    )
    checks.close(
        case["cpu"]["average_client_cores"],
        client / wall,
        "Load-generator average-core arithmetic differs",
    )
    return {
        "pid": pid,
        "server_peak_rss_mib": final_peak / BYTES_PER_MIB,
        "server_cpu_seconds": delta,
        "average_server_cores": delta / wall,
        "client_cpu_seconds": client,
        "average_client_cores": client / wall,
    }


def verify_artifacts(artifact_dir: Path, project: Path = PROJECT) -> dict:
    """Verify saved traces and provenance while refusing any formal-data dependency."""
    project = project.resolve()
    artifact_dir = artifact_dir.resolve()
    if artifact_dir.is_relative_to(project / "var"):
        raise EvidenceError("Verification artifacts must be outside the formal workspace")
    access = deny_formal_opens(project)
    checks = Checks()
    result = read_json(artifact_dir / "results.json")
    checks.equal(result["schema_version"], 1, "Benchmark schema version differs")
    fixture_ids, source_receipt = verify_sources(result, project, checks)
    config = result["server_config"]
    expected_config = {
        "workers": 1,
        "host": "127.0.0.1",
        "loop": "asyncio",
        "http": "h11",
        "public": True,
        "numeric_threads": 1,
        "prediction_slots": 2,
        "production_rate_limit": 30,
        "test_rate_limit": 1_000_000_000,
        "rate_window_seconds": 60,
        "local_resource_limits_replicated": False,
        "production_resource_limits_source": UNIT_FILE,
    }
    for name, expected in expected_config.items():
        checks.equal(config[name], expected, "Server configuration differs: " + name)
    checks.equal(
        config["production_resource_limits"],
        {
            "cpu_quota": "50%",
            "memory_high_mib": 192,
            "memory_max_mib": 256,
            "tasks_max": 64,
            "file_descriptors": 2048,
        },
        "Recorded production resource limits differ",
    )
    model = result["model"]
    checks.equal(model["kind"], "mlp16", "Benchmark model kind differs")
    checks.close(model["threshold"], 0.5, "Benchmark model threshold differs")
    checks.require(
        isinstance(model["run_id"], str) and RUN_ID.fullmatch(model["run_id"]) is not None,
        "Selected run identity is invalid",
    )
    for field in ("weights_sha256", "vocabulary_sha256"):
        checks.require(
            isinstance(model[field], str) and HEX_DIGEST.fullmatch(model[field]) is not None,
            "Selected numeric model digest is invalid",
        )
    reference = result.get("production_reference")
    if reference is not None:
        checks.equal(
            reference["loaded"], model, "Saved online metadata differs from the tested model"
        )
    checks.equal(
        [case["concurrency"] for case in result["scenarios"]],
        list(SCENARIOS),
        "Formal scenarios are not exactly 1, 10 and 50 clients",
    )
    receipts = []
    pids = set()
    for case in result["scenarios"]:
        receipt = verify_trace(case, artifact_dir, fixture_ids, checks)
        receipt["resources"] = verify_resources(case, model, result["versions"], checks)
        pids.add(receipt["resources"]["pid"])
        checks.equal(
            case["warmup_requests"],
            result["workload"]["warmup_requests"],
            "Scenario warmup count differs",
        )
        checks.equal(
            case["fixture_preflight_requests"],
            len(fixture_ids),
            "Scenario fixture preflight count differs",
        )
        receipts.append(receipt)
    checks.equal(len(pids), 3, "Scenarios did not use three fresh backend process identities")
    integrity = result["integrity"]
    checks.equal(
        integrity["source_model_files_unchanged"],
        True,
        "Harness did not confirm model-file preservation",
    )
    checks.equal(integrity["online_load_requests"], 0, "Harness reports public load requests")
    checks.equal(integrity["external_model_calls"], 0, "Harness reports external model calls")
    expected_paths = {"var/service.json"} | {
        "var/runs/" + model["run_id"] + "/" + name
        for name in ("model.json", "weights.npz", "vocabulary.json", "report.json")
    }
    snapshots = integrity["source_model_fingerprints"]
    checks.equal(set(snapshots), expected_paths, "Protected source-file snapshot scope differs")
    for snapshot in snapshots.values():
        checks.require(
            isinstance(snapshot["sha256"], str)
            and HEX_DIGEST.fullmatch(snapshot["sha256"]) is not None,
            "Protected source-file snapshot digest is invalid",
        )
        for field in ("size", "mode", "mtime_ns"):
            _integer(snapshot[field], "Protected source-file metadata")
    _number(result["load_generator"]["peak_rss_mib"], "Generator peak RSS", positive=True)
    checks.equal(access["blocked_formal_opens"], 0, "Verifier attempted to open formal inputs")
    return {
        "status": "passed",
        "execution_commit": EXECUTION_COMMIT,
        "checked_at_utc": datetime.now(timezone.utc).isoformat(),
        "independent_stdlib_verifier": "scripts/verify_load_test.py",
        "results_sha256": digest((artifact_dir / "results.json").read_bytes()),
        "checks_passed": checks.count,
        "scenario_count": len(receipts),
        "source_and_fixture": source_receipt,
        "scenarios": receipts,
        "formal_files_opened": 0,
        "model_files_reopened": 0,
        "network_requests": 0,
        "scope": {
            "response_validity": "Harness-recorded flags only; response bodies were not retained",
            "resources": "Recorded OS snapshots: unit conversions and arithmetic rechecked only",
            "model_preservation": "Harness assertion plus saved digests; models not reopened",
            "capacity": "Local loopback evidence; VPS resource limits were not replicated",
        },
    }


def write_receipt(path: Path, report: dict) -> None:
    """Publish a complete verification receipt after checks finish without touching test inputs."""
    content = json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False).encode() + b"\n"
    descriptor, staging = tempfile.mkstemp(prefix=".verify-load-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(staging, path)
    finally:
        if os.path.exists(staging):
            os.unlink(staging)


def main() -> None:
    """Recompute the saved three-scenario receipt without starting a server or load generator."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-dir", type=Path, default=PROJECT / "artifacts/load-test")
    args = parser.parse_args()
    artifact_dir = args.artifact_dir.resolve()
    try:
        report = verify_artifacts(artifact_dir)
        write_receipt(artifact_dir / "verification.json", report)
    except (
        EvidenceError,
        OSError,
        ValueError,
        KeyError,
        TypeError,
        subprocess.CalledProcessError,
        csv.Error,
    ) as error:
        print(
            json.dumps(
                {"status": "failed", "error_type": type(error).__name__, "reason": str(error)}
            )
        )
        raise SystemExit(1) from None
    print(
        json.dumps(
            {
                "status": "passed",
                "checks_passed": report["checks_passed"],
                "scenario_count": report["scenario_count"],
                "requests_verified": sum(row["issued_requests"] for row in report["scenarios"]),
                "formal_files_opened": 0,
                "network_requests": 0,
            }
        )
    )


if __name__ == "__main__":
    main()
