"""Benchmark only an owned loopback inference subprocess, never an arbitrary service URL."""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
import csv
from dataclasses import dataclass
from datetime import datetime, timezone
import gzip
import hashlib
import importlib.metadata
import itertools
import json
import math
import os
from pathlib import Path
import platform
import resource
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from typing import Any
from urllib.parse import urlsplit

import httpx

PROJECT = Path(__file__).resolve().parents[1]
FORMAL = PROJECT / "var"
DEFAULT_OUTPUT = PROJECT / "artifacts/load-test"
FIXTURES = PROJECT / "examples/load_test_sms.json"
REPORT = PROJECT / "docs/LOAD_TEST.md"
HOST = "127.0.0.1"
MIB = 1024 * 1024
DEFAULT_DURATION = 60.0
CONCURRENCIES = (1, 10, 50)
WARMUP_REQUESTS = 100
REQUEST_TIMEOUT = 10.0
MODEL_FILES = ("model.json", "weights.npz", "vocabulary.json", "report.json")
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


@dataclass(frozen=True, slots=True)
class RequestRecord:
    """Store numeric timing evidence without retaining request or response bodies."""

    worker: int
    fixture_id: str
    start_seconds: float
    end_seconds: float
    status: int
    error_kind: str

    @property
    def latency_ms(self) -> float:
        """Express full-response receipt time in milliseconds for percentile calculations."""
        return (self.end_seconds - self.start_seconds) * 1000


def sha(content: bytes) -> str:
    """Identify exact source/model/trace bytes so benchmark numbers remain auditable."""
    return hashlib.sha256(content).hexdigest()


def write_json(path: Path, value: Any) -> None:
    """Write a readable benchmark artifact containing no credentials or formal SMS data."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n")


def validate_loopback_url(url: str) -> str:
    """Reject remote, credential-bearing or redirected targets before any load request."""
    parsed = urlsplit(url)
    if (
        parsed.scheme != "http"
        or parsed.hostname != HOST
        or parsed.port is None
        or not 1 <= parsed.port <= 65535
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Load testing only supports the owned http://127.0.0.1:<port> process")
    return f"http://{HOST}:{parsed.port}"


def model_fingerprints(paths: set[Path]) -> dict[str, Any]:
    """Read only selected numerical model files, without traversing the formal workspace."""
    result = {}
    for path in sorted(paths):
        stat = path.stat()
        result[str(path.relative_to(PROJECT))] = {
            "sha256": sha(path.read_bytes()),
            "size": stat.st_size,
            "mode": stat.st_mode,
            "mtime_ns": stat.st_mtime_ns,
        }
    return result


def prepare_bundle(target: Path) -> tuple[dict, dict]:
    """Copy just the selected public JSON/NPZ artifacts into a separate temporary directory."""
    source_service = FORMAL / "service.json"
    allowed = {source_service.resolve()}

    def audit(event: str, args: tuple) -> None:
        if event != "open" or not isinstance(args[0], (str, bytes, os.PathLike)):
            return
        path = Path(os.fsdecode(args[0])).resolve()
        if not path.is_relative_to(FORMAL):
            return
        flags = args[2] or 0
        writes = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC
        if path not in allowed or flags & writes:
            raise PermissionError("Load testing may only read the selected numerical model files")

    sys.addaudithook(audit)
    selected = json.loads(source_service.read_text())["selected"]
    run = selected["run_id"]
    directory = FORMAL / "runs" / run
    if directory.resolve().parent != (FORMAL / "runs").resolve():
        raise ValueError("Invalid selected model directory")
    if selected["kind"] != "mlp16" or selected["threshold"] != 0.5:
        raise ValueError("Benchmark requires the selected online mlp16 / 0.5 model")
    allowed.update((directory / name).resolve() for name in MODEL_FILES)
    before = model_fingerprints(allowed)
    destination = target / "runs" / run
    destination.mkdir(parents=True)
    for name in MODEL_FILES:
        shutil.copyfile(directory / name, destination / name)
    write_json(
        target / "service.json",
        {
            "revision": 1,
            "selected": selected,
            "history": [],
            "activation": "isolated public load-test bundle",
        },
    )
    write_json(
        target / "load-test-bundle.json",
        {
            "schema_version": 1,
            "purpose": "local-load-test",
            "public_bundle": True,
        },
    )
    return selected, {"paths": allowed, "before": before}


def machine_info() -> dict:
    """Record non-identifying local hardware facts rather than assuming a VPS-equivalent host."""

    def sysctl(key: str, *, numeric: bool = False) -> Any:
        try:
            result = subprocess.run(
                ["sysctl", "-n", key], capture_output=True, text=True, check=True
            ).stdout.strip()
            return int(result) if numeric else result
        except (OSError, ValueError, subprocess.CalledProcessError):
            return None

    memory = sysctl("hw.memsize", numeric=True)
    if memory is None:
        try:
            memory = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
        except (ValueError, OSError):
            pass
    return {
        "os": platform.platform(),
        "architecture": platform.machine(),
        "cpu_model": sysctl("machdep.cpu.brand_string") or platform.processor() or "unavailable",
        "hardware_model": sysctl("hw.model"),
        "physical_cores": sysctl("hw.physicalcpu", numeric=True),
        "logical_cores": sysctl("hw.logicalcpu", numeric=True) or os.cpu_count(),
        "memory_bytes": memory,
    }


def peak_rss_bytes() -> int:
    """Normalize this load generator's OS high-water RSS across macOS and Linux."""
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(peak if sys.platform == "darwin" else peak * 1024)


def cpu_seconds() -> float:
    """Measure generator CPU separately from the inference subprocess."""
    usage = resource.getrusage(resource.RUSAGE_SELF)
    return usage.ru_utime + usage.ru_stime


def child_command(port: int) -> list[str]:
    """Pin one h11/asyncio worker and loopback so the harness cannot load a public service."""
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("The owned server needs a port in 1..65535")
    return [
        sys.executable,
        "-B",
        "-m",
        "uvicorn",
        "textlab.load_test_server:create_app",
        "--factory",
        "--host",
        HOST,
        "--port",
        str(port),
        "--workers",
        "1",
        "--loop",
        "asyncio",
        "--http",
        "h11",
        "--no-access-log",
        "--proxy-headers",
        "--forwarded-allow-ips",
        HOST,
    ]


def child_environment(bundle: Path) -> dict[str, str]:
    """Set numeric single-threading before imports and remove external API credentials."""
    return {
        **os.environ,
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUNBUFFERED": "1",
        "SMS_LAB_LOAD_TEST": "true",
        "SMS_LAB_LOAD_TEST_HOST": HOST,
        "SMS_LAB_ROOT": str(bundle),
        "SMS_LAB_PUBLIC": "true",
        "SMS_LAB_ADMIN_TOKEN": "",
        "SMS_LAB_JEV_ENABLED": "false",
        "SMS_LAB_JEV_SERVER_KEY_ENABLED": "false",
        "TYPESAFE_API_KEY": "",
        "DEEPSEEK_API_KEY": "",
        "OMP_NUM_THREADS": "1",
        "OPENBLAS_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1",
        "NUMEXPR_NUM_THREADS": "1",
        "VECLIB_MAXIMUM_THREADS": "1",
    }


def response_error(status: int, payload: Any, expected: dict) -> str:
    """Count busy/rate rejections separately and reject incorrect successful prediction bodies."""
    if status == 429:
        detail = payload.get("detail", "") if isinstance(payload, dict) else ""
        if detail == "Prediction capacity is busy":
            return "capacity_busy"
        if detail == "Please wait before predicting again":
            return "minute_rate_limit"
        return "other_429"
    if status != 200:
        return f"http_{status}"
    if payload != expected:
        return "prediction_mismatch"
    return ""


def quantiles(values: list[float]) -> dict[str, float | None]:
    """Use linear interpolation on the observed request cohort without dropping errors silently."""
    ordered = sorted(values)
    result = {}
    for name, q in (("p50", 0.5), ("p95", 0.95), ("p99", 0.99)):
        if not ordered:
            result[name] = None
            continue
        position = (len(ordered) - 1) * q
        low, high = math.floor(position), math.ceil(position)
        result[name] = ordered[low] + (ordered[high] - ordered[low]) * (position - low)
    return result


def summarize_records(records: list[RequestRecord], duration: float) -> dict:
    """Separate completion-window throughput from all-issued-cohort latency and error rates."""
    issued = len(records)
    successful = [r for r in records if not r.error_kind]
    in_window = [r for r in records if r.end_seconds <= duration]
    successful_in_window = sum(not r.error_kind for r in in_window)
    return {
        "issued_requests": issued,
        "completed_requests": issued,
        "completed_in_window": len(in_window),
        "successful_in_window": successful_in_window,
        "successful_requests": len(successful),
        "error_requests": issued - len(successful),
        "qps": len(in_window) / duration,
        "successful_qps": successful_in_window / duration,
        "error_rate": (issued - len(successful)) / issued if issued else None,
        "status_counts": {"0": 0, "200": 0, "429": 0}
        | dict(Counter(str(r.status) for r in records)),
        "error_counts": dict.fromkeys(
            (
                "capacity_busy",
                "minute_rate_limit",
                "other_429",
                "prediction_mismatch",
                "timeout",
                "transport_error",
            ),
            0,
        )
        | dict(Counter(r.error_kind for r in records if r.error_kind)),
        "fixture_counts": dict(Counter(r.fixture_id for r in records)),
        "latency_ms": {
            "successful": quantiles([r.latency_ms for r in successful]),
            "all": quantiles([r.latency_ms for r in records]),
        },
        "drained_requests": sum(r.end_seconds > duration for r in records),
    }


async def wait_ready(client: httpx.AsyncClient, process: subprocess.Popen, selected: dict) -> dict:
    """Pin the responding local process PID and exact model before sending any load."""
    for _ in range(100):
        if process.poll() is not None:
            raise RuntimeError("Owned inference subprocess exited before readiness")
        try:
            response = await client.get("/api/health")
            if response.status_code == 200:
                health = response.json()
                metrics = (await client.get("/__load_test__/metrics")).raise_for_status().json()
                if metrics["pid"] != process.pid or health["loaded"] != selected:
                    raise RuntimeError("Loopback response does not match the owned process/model")
                return health
        except (httpx.HTTPError, ValueError):
            pass
        await asyncio.sleep(0.05)
    raise RuntimeError("Owned inference subprocess did not become ready")


async def issue_load(
    client: httpx.AsyncClient,
    samples: list[dict],
    expected: dict,
    concurrency: int,
    duration: float,
) -> tuple[list[RequestRecord], float]:
    """Run a closed-loop workload, stop issuing at the deadline, then drain existing requests."""
    records: list[RequestRecord] = []
    cycle = itertools.cycle(samples)
    started = time.perf_counter()

    async def worker(index: int) -> None:
        while time.perf_counter() - started < duration:
            sample = next(cycle)
            begin = time.perf_counter() - started
            if begin >= duration:
                return
            status = 0
            try:
                response = await client.post(
                    "/api/predict",
                    content=sample["body"],
                    headers={"Content-Type": "application/json"},
                )
                end = time.perf_counter() - started
                status = response.status_code
                try:
                    payload = response.json()
                except ValueError:
                    payload = None
                error = response_error(status, payload, expected[sample["id"]])
            except httpx.TimeoutException:
                end = time.perf_counter() - started
                error = "timeout"
            except httpx.HTTPError:
                end = time.perf_counter() - started
                error = "transport_error"
            records.append(RequestRecord(index, sample["id"], begin, end, status, error))

    await asyncio.gather(*(worker(index) for index in range(concurrency)))
    return records, time.perf_counter() - started


def write_trace(path: Path, records: list[RequestRecord]) -> None:
    """Compress the complete numeric request trace only after the timed window finishes."""
    with gzip.open(path, "wt", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(
            ["worker", "fixture_id", "start_seconds", "end_seconds", "status", "error_kind"]
        )
        for r in records:
            writer.writerow(
                [
                    r.worker,
                    r.fixture_id,
                    repr(r.start_seconds),
                    repr(r.end_seconds),
                    r.status,
                    r.error_kind,
                ]
            )


async def run_case(
    bundle: Path,
    selected: dict,
    samples: list[dict],
    concurrency: int,
    duration: float,
    warmup: int,
    output: Path,
) -> dict:
    """Use a fresh owned backend for one scenario so other scenarios cannot retain its queues."""
    with socket.socket() as listener:
        listener.bind((HOST, 0))
        port = listener.getsockname()[1]
    url = validate_loopback_url(f"http://{HOST}:{port}")
    with (output / f"server-c{concurrency}.txt").open("w") as log:
        process = subprocess.Popen(
            child_command(port),
            cwd=PROJECT,
            env=child_environment(bundle),
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            async with httpx.AsyncClient(
                base_url=url,
                trust_env=False,
                follow_redirects=False,
                timeout=httpx.Timeout(REQUEST_TIMEOUT, connect=2),
                limits=httpx.Limits(
                    max_connections=concurrency, max_keepalive_connections=concurrency
                ),
            ) as client:
                await wait_ready(client, process, selected)
                expected = {}
                for sample in samples:
                    response = await client.post(
                        "/api/predict",
                        content=sample["body"],
                        headers={"Content-Type": "application/json"},
                    )
                    expected[sample["id"]] = response.raise_for_status().json()
                    value = expected[sample["id"]]
                    if value["model_version"] != selected["run_id"] or value["threshold"] != 0.5:
                        raise ValueError("Authored-fixture prediction loaded the wrong model")
                for number in range(warmup):
                    sample = samples[number % len(samples)]
                    response = await client.post(
                        "/api/predict",
                        content=sample["body"],
                        headers={"Content-Type": "application/json"},
                    )
                    if response_error(
                        response.status_code, response.json(), expected[sample["id"]]
                    ):
                        raise RuntimeError("Authored-fixture warmup did not complete successfully")
                before = (await client.get("/__load_test__/metrics")).raise_for_status().json()
                client_cpu_before = cpu_seconds()
                records, wall = await issue_load(client, samples, expected, concurrency, duration)
                after = (await client.get("/__load_test__/metrics")).raise_for_status().json()
                client_cpu = cpu_seconds() - client_cpu_before
            result = summarize_records(records, duration)
            server_cpu = (
                after["user_cpu_seconds"]
                + after["system_cpu_seconds"]
                - before["user_cpu_seconds"]
                - before["system_cpu_seconds"]
            )
            trace = output / f"requests-c{concurrency}.csv.gz"
            write_trace(trace, records)
            result.update(
                {
                    "concurrency": concurrency,
                    "duration_seconds": duration,
                    "wall_seconds": wall,
                    "drain_seconds": max(0, wall - duration),
                    "memory": {
                        "server_peak_rss_mib": after["peak_rss_bytes"] / MIB,
                        "server_warmup_peak_rss_mib": before["peak_rss_bytes"] / MIB,
                        "peak_scope": "Fresh process lifecycle: startup, warmup, load and drain",
                    },
                    "cpu": {
                        "server_cpu_seconds": server_cpu,
                        "average_server_cores": server_cpu / wall,
                        "client_cpu_seconds": client_cpu,
                        "average_client_cores": client_cpu / wall,
                    },
                    "server_metrics_before": before,
                    "server_metrics_after": after,
                    "trace_file": trace.name,
                    "trace_sha256": sha(trace.read_bytes()),
                    "warmup_requests": warmup,
                    "fixture_preflight_requests": len(samples),
                    "local_pid": process.pid,
                }
            )
            return result
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


async def benchmark(
    output: Path, duration: float, warmup: int, production_reference: Path | None
) -> dict:
    """Run the required three local-only scenarios with explicit model and source provenance."""
    fixture = json.loads(FIXTURES.read_text())
    samples = [
        {**sample, "body": json.dumps({"text": sample["text"]}).encode()}
        for sample in fixture["samples"]
    ]
    result = {
        "schema_version": 1,
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=PROJECT, text=True
        ).strip(),
        "source_sha256": {name: sha((PROJECT / name).read_bytes()) for name in SOURCE_FILES},
        "machine": machine_info(),
        "versions": {
            "python": platform.python_version(),
            **{
                name: importlib.metadata.version(name)
                for name in ("numpy", "fastapi", "starlette", "anyio", "uvicorn", "h11", "httpx")
            },
        },
        "server_config": {
            "workers": 1,
            "host": HOST,
            "loop": "asyncio",
            "http": "h11",
            "public": True,
            "numeric_threads": 1,
            "prediction_slots": 2,
            "production_rate_limit": 30,
            "test_rate_limit": 1_000_000_000,
            "rate_window_seconds": 60,
            "production_resource_limits": {
                "cpu_quota": "50%",
                "memory_high_mib": 192,
                "memory_max_mib": 256,
                "tasks_max": 64,
                "file_descriptors": 2048,
            },
            "production_resource_limits_source": "deploy/text-workbench.service",
            "local_resource_limits_replicated": False,
        },
        "workload": {
            "fixture_file": "examples/load_test_sms.json",
            "fixture_sha256": sha(FIXTURES.read_bytes()),
            "fixture_count": len(samples),
            "text_min_chars": min(len(s["text"]) for s in samples),
            "text_max_chars": max(len(s["text"]) for s in samples),
            "pattern": "closed-loop keep-alive round-robin AI-authored English SMS",
            "warmup_requests": warmup,
            "qps_definition": "Completions within exact duration / duration",
            "latency_error_scope": "All requests issued inside the window, including drain",
            "percentile_method": "linear interpolation at (N-1)*quantile",
        },
        "production_reference": json.loads(production_reference.read_text())
        if production_reference
        else None,
        "scenarios": [],
    }
    with tempfile.TemporaryDirectory(prefix="textlab-load-model-") as temporary:
        bundle = Path(temporary)
        selected, protection = prepare_bundle(bundle)
        result["model"] = selected
        if result["production_reference"] and result["production_reference"]["loaded"] != selected:
            raise ValueError("Model differs from the independently obtained online metadata")
        for concurrency in CONCURRENCIES:
            case = await run_case(bundle, selected, samples, concurrency, duration, warmup, output)
            result["scenarios"].append(case)
            write_json(output / "results.partial.json", result)
            print(
                json.dumps(
                    {
                        "concurrency": concurrency,
                        "qps": case["qps"],
                        "successful_qps": case["successful_qps"],
                        "error_rate": case["error_rate"],
                        "server_peak_rss_mib": case["memory"]["server_peak_rss_mib"],
                    }
                ),
                flush=True,
            )
        unchanged = model_fingerprints(protection["paths"]) == protection["before"]
        if not unchanged:
            raise ValueError("Source model files changed during the benchmark")
        result["integrity"] = {
            "source_model_files_unchanged": unchanged,
            "source_model_fingerprints": protection["before"],
            "online_load_requests": 0,
            "external_model_calls": 0,
        }
    result["load_generator"] = {"peak_rss_mib": peak_rss_bytes() / MIB}
    return result


def main() -> None:
    """Benchmark a fresh local process per scenario or regenerate the report from saved metrics."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--duration", type=float, default=DEFAULT_DURATION)
    parser.add_argument("--warmup", type=int, default=WARMUP_REQUESTS)
    parser.add_argument("--production-reference", type=Path)
    parser.add_argument("--replay", type=Path)
    args = parser.parse_args()
    if args.replay:
        result = json.loads(args.replay.read_text())
        from textlab.load_report import render_report

        render_report(result, docs_path=REPORT, artifact_dir=args.replay.resolve().parent)
        print(json.dumps({"mode": "offline_replay", "network_requests": 0}))
        return
    if not math.isfinite(args.duration) or args.duration <= 0 or args.warmup < 0:
        parser.error("Use a finite positive duration and a nonnegative warmup count")
    output = args.output.resolve()
    if output.exists():
        parser.error("Output already exists; use a fresh directory or --replay the saved results")
    if output.is_relative_to(FORMAL.resolve()) or FORMAL.resolve().is_relative_to(output):
        parser.error("Output must be outside the formal workspace")
    output.mkdir(parents=True)
    result = asyncio.run(benchmark(output, args.duration, args.warmup, args.production_reference))
    write_json(output / "results.json", result)
    from textlab.load_report import render_report

    render_report(result, docs_path=REPORT, artifact_dir=output)


if __name__ == "__main__":
    main()
