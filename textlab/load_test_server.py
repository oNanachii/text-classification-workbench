"""Opt-in loopback benchmark factory; production source and its two prediction slots stay intact."""

import ctypes
import json
import math
import os
from pathlib import Path
import re
import resource
import sys
import threading
from typing import Any

from fastapi import FastAPI, Response

from textlab import app_v2
from textlab.decision import DecisionService, DecisionSettings

PROJECT = Path(__file__).resolve().parents[1]
FORMAL_ROOT = (PROJECT / "var").resolve()
LOOPBACK_HOST = "127.0.0.1"
MARKER_NAME = "load-test-bundle.json"
MARKER_PURPOSE = "local-load-test"
MARKER_MAX_BYTES = 4096
SERVICE_MAX_BYTES = 64 * 1024
LOAD_TEST_REQUESTS_PER_MINUTE = 1_000_000_000
PRODUCTION_PREDICTION_SLOTS = 2
RUN_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,95}\Z")
PUBLIC_RUN_FILES = {"model.json", "weights.npz", "vocabulary.json", "report.json"}
BYTES_PER_KIB = 1024
DARWIN_PROC_PIDTASKINFO = 4
THREADPOOL_TEXT = re.compile(r"[A-Za-z0-9_. +:-]{1,128}\Z")
THREADPOOL_TEXT_FIELDS = (
    "user_api",
    "internal_api",
    "version",
    "threading_layer",
    "architecture",
    "prefix",
)


class _DarwinTaskInfo(ctypes.Structure):
    # Matches proc_taskinfo in the installed macOS SDK's sys/proc_info.h.
    _fields_ = [
        ("virtual_size", ctypes.c_uint64),
        ("resident_size", ctypes.c_uint64),
        ("total_user", ctypes.c_uint64),
        ("total_system", ctypes.c_uint64),
        ("threads_user", ctypes.c_uint64),
        ("threads_system", ctypes.c_uint64),
        ("policy", ctypes.c_int32),
        ("faults", ctypes.c_int32),
        ("pageins", ctypes.c_int32),
        ("cow_faults", ctypes.c_int32),
        ("messages_sent", ctypes.c_int32),
        ("messages_received", ctypes.c_int32),
        ("syscalls_mach", ctypes.c_int32),
        ("syscalls_unix", ctypes.c_int32),
        ("context_switches", ctypes.c_int32),
        ("thread_count", ctypes.c_int32),
        ("running_threads", ctypes.c_int32),
        ("priority", ctypes.c_int32),
    ]


def _thread_count() -> tuple[int, str]:
    try:
        if sys.platform == "darwin":
            library = ctypes.CDLL("/usr/lib/libproc.dylib")
            query = library.proc_pidinfo
            query.argtypes = [
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_uint64,
                ctypes.c_void_p,
                ctypes.c_int,
            ]
            query.restype = ctypes.c_int
            info = _DarwinTaskInfo()
            result = query(
                os.getpid(), DARWIN_PROC_PIDTASKINFO, 0, ctypes.byref(info), ctypes.sizeof(info)
            )
            if result == ctypes.sizeof(info) and info.thread_count > 0:
                return info.thread_count, "darwin_proc_pidinfo"
        elif sys.platform.startswith("linux"):
            for line in Path("/proc/self/status").read_text().splitlines():
                if line.startswith("Threads:"):
                    value = int(line.split(":", 1)[1].strip())
                    if value > 0:
                        return value, "linux_proc_status"
    except (OSError, ValueError, AttributeError):
        pass
    return threading.active_count(), "python_threading_active_count_fallback"


def _threadpool_info() -> tuple[bool, list[dict[str, Any]]]:
    try:
        from threadpoolctl import threadpool_info
    except ImportError:
        return False, []
    try:
        values = threadpool_info()
    except Exception:
        return False, []
    clean = []
    for item in values:
        if not isinstance(item, dict):
            continue
        entry = {}
        for key in THREADPOOL_TEXT_FIELDS:
            value = item.get(key)
            if isinstance(value, str) and THREADPOOL_TEXT.fullmatch(value):
                entry[key] = value
        threads = item.get("num_threads")
        if type(threads) is int and threads > 0:
            entry["num_threads"] = threads
        if entry:
            clean.append(entry)
    return True, clean


def _numpy_backend() -> dict[str, Any]:
    result: dict[str, Any] = {
        "available": False,
        "identity_scope": "NumPy build configuration only; not active BLAS thread measurement",
        "runtime_threads_verified": False,
    }
    try:
        import numpy as np
    except ImportError:
        return result
    result["available"] = True
    version = getattr(np, "__version__", None)
    if isinstance(version, str) and THREADPOOL_TEXT.fullmatch(version):
        result["numpy_version"] = version
    config = getattr(getattr(np, "__config__", None), "CONFIG", {})
    if not isinstance(config, dict):
        return result
    dependencies = {}
    for key in ("Build Dependencies", "BuildDependencies", "build_dependencies"):
        candidate = config.get(key)
        if isinstance(candidate, dict):
            dependencies = candidate
            break
    for backend in ("blas", "lapack"):
        details = dependencies.get(backend)
        if not isinstance(details, dict):
            continue
        clean = {}
        for key in ("name", "version"):
            value = details.get(key)
            if isinstance(value, str) and THREADPOOL_TEXT.fullmatch(value):
                clean[key] = value
        if type(details.get("found")) is bool:
            clean["found"] = details["found"]
        if clean:
            result[backend] = clean
    return result


def _process_metrics(limits: dict[str, Any]) -> dict[str, Any]:
    pools_available, pools = _threadpool_info()
    numpy_backend = _numpy_backend()
    threads, thread_scope = _thread_count()
    usage = resource.getrusage(resource.RUSAGE_SELF)
    peak = int(usage.ru_maxrss)
    if sys.platform != "darwin":
        peak *= BYTES_PER_KIB
    return {
        "pid": os.getpid(),
        "user_cpu_seconds": usage.ru_utime,
        "system_cpu_seconds": usage.ru_stime,
        "peak_rss_bytes": peak,
        "peak_rss_scope": "OS process-lifecycle maximum, including startup and model loading",
        "thread_count": threads,
        "thread_count_scope": thread_scope,
        "threadpool_info": pools,
        "threadpool_info_available": pools_available,
        "numpy_backend": numpy_backend,
        "limits": dict(limits),
    }


def _read_metadata(path: Path, maximum_bytes: int) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > maximum_bytes:
        raise ValueError("Load-test bundle metadata must be a bounded regular file")
    try:
        value = json.loads(path.read_bytes())
    except (OSError, ValueError, UnicodeDecodeError, RecursionError):
        raise ValueError("Invalid load-test bundle metadata") from None
    if not isinstance(value, dict):
        raise ValueError("Load-test bundle metadata must contain a JSON object")
    return value


def _bundle_root() -> tuple[Path, str]:
    if os.environ.get("SMS_LAB_LOAD_TEST") != "true":
        raise ValueError("Load tests require the explicit SMS_LAB_LOAD_TEST=true opt-in")
    if os.environ.get("SMS_LAB_LOAD_TEST_HOST") != LOOPBACK_HOST:
        raise ValueError("Load tests require the explicit loopback-only host marker")
    configured = os.environ.get("SMS_LAB_ROOT", "")
    if not configured.strip():
        raise ValueError("Load tests require an explicitly supplied independent public bundle")
    supplied = Path(configured)
    if supplied.is_symlink():
        raise ValueError("Load-test bundle root must not be a symlink")
    root = supplied.resolve()
    if not root.is_dir() or root.is_relative_to(FORMAL_ROOT) or FORMAL_ROOT.is_relative_to(root):
        raise ValueError("Load-test bundle must be independent of the formal workspace")
    marker = _read_metadata(root / MARKER_NAME, MARKER_MAX_BYTES)
    if (
        set(marker) != {"schema_version", "purpose", "public_bundle"}
        or type(marker["schema_version"]) is not int
        or marker["schema_version"] != 1
        or marker["purpose"] != MARKER_PURPOSE
        or marker["public_bundle"] is not True
    ):
        raise ValueError("Load-test bundle marker does not match the test-only public protocol")
    service = _read_metadata(root / "service.json", SERVICE_MAX_BYTES)
    selected = service.get("selected")
    if not isinstance(selected, dict):
        raise ValueError("Load-test bundle requires a selected numeric model")
    run_id = selected.get("run_id")
    if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id):
        raise ValueError("Load-test bundle has an invalid model run identity")
    kind = selected.get("kind")
    threshold = selected.get("threshold")
    if (
        not isinstance(kind, str)
        or kind not in {"lr", "mlp16"}
        or type(threshold) not in {int, float}
        or not math.isfinite(threshold)
        or not 0 <= threshold <= 1
    ):
        raise ValueError("Load-test bundle selection must use the safe numeric model format")
    expected_files = {MARKER_NAME, "service.json"} | {
        f"runs/{run_id}/{name}" for name in PUBLIC_RUN_FILES
    }
    expected_directories = {"runs", f"runs/{run_id}"}
    observed_files = set()
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ValueError("Load-test bundles must not contain symlinks")
        relative = path.relative_to(root).as_posix()
        if path.is_dir() and relative in expected_directories:
            continue
        if path.is_file() and relative in expected_files:
            observed_files.add(relative)
            continue
        raise ValueError("Load-test bundle contains a private, historical or unexpected entry")
    if observed_files != expected_files:
        raise ValueError("Load-test bundle is missing a required public numeric file")
    return root, run_id


def create_app() -> FastAPI:
    """Build one isolated public benchmark process with only the minute rate limit relaxed."""
    root, run_id = _bundle_root()
    if app_v2.PREDICTION_CONCURRENCY != PRODUCTION_PREDICTION_SLOTS:
        raise ValueError("Load testing must retain the production two-slot prediction capacity")
    # This module is launched only in its own loopback subprocess. The rate is read per request,
    # so it stays overridden for that process; no source file or other process is changed.
    previous_rate = app_v2.PREDICTIONS_PER_MINUTE
    app_v2.PREDICTIONS_PER_MINUTE = LOAD_TEST_REQUESTS_PER_MINUTE
    try:
        app = app_v2.create_app(
            root=root,
            admin_token="",
            public=True,
            decision_service=DecisionService(
                DecisionSettings(enabled=False, server_key_enabled=False)
            ),
        )
    except BaseException:
        app_v2.PREDICTIONS_PER_MINUTE = previous_rate
        raise
    app.state.load_test_config = {
        "test_only": True,
        "host": LOOPBACK_HOST,
        "public_read_only": True,
        "external_decision_enabled": False,
        "requests_per_minute": LOAD_TEST_REQUESTS_PER_MINUTE,
        "production_requests_per_minute": previous_rate,
        "prediction_slots": PRODUCTION_PREDICTION_SLOTS,
        "run_id": run_id,
        "binding_enforced_by": "Parent harness must launch uvicorn on 127.0.0.1 only",
    }

    @app.get("/__load_test__/metrics", include_in_schema=False)
    def metrics(response: Response) -> dict[str, Any]:
        """Read this isolated process before or after the load interval without polling it."""
        response.headers["Cache-Control"] = "no-store"
        return _process_metrics(app.state.load_test_config)

    return app
