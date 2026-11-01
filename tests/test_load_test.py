"""Test loopback benchmark accounting and launch safety without real model data or load traffic."""

from dataclasses import FrozenInstanceError
import io
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, Mock, patch
from contextlib import redirect_stderr

import httpx

from textlab import load_test as load


class TargetAndLaunchTests(unittest.TestCase):
    """Keep target selection and child configuration local, explicit and credential-free."""

    def test_only_plain_owned_ipv4_loopback_origins_are_accepted(self) -> None:
        """Reject remote names, credentials and paths before a load client can issue requests."""
        self.assertEqual(
            load.validate_loopback_url("http://127.0.0.1:12345/"), "http://127.0.0.1:12345"
        )
        self.assertEqual(load.validate_loopback_url("http://127.0.0.1:1"), "http://127.0.0.1:1")
        self.assertEqual(
            load.validate_loopback_url("http://127.0.0.1:65535"), "http://127.0.0.1:65535"
        )
        invalid = [
            "https://127.0.0.1:12345",
            "http://127.0.0.1",
            "http://127.0.0.1:0",
            "http://127.0.0.1:-1",
            "http://127.0.0.1:65536",
            "http://127.0.0.1:not-a-port",
            "http://localhost:12345",
            "http://[::1]:12345",
            "http://127.0.0.2:12345",
            "http://0.0.0.0:12345",
            "http://example.invalid:12345",
            "http://user:password@127.0.0.1:12345",
            "http://127.0.0.1:12345/api/predict",
            "http://127.0.0.1:12345/?query=1",
            "http://127.0.0.1:12345/#fragment",
        ]
        for url in invalid:
            with self.subTest(url=url), self.assertRaises(ValueError):
                load.validate_loopback_url(url)

    def test_child_command_pins_one_owned_loopback_worker(self) -> None:
        """Pin process arguments so environment and shell settings cannot change binding."""
        command = load.child_command(23456)
        self.assertEqual(
            command[:5],
            [sys.executable, "-B", "-m", "uvicorn", "textlab.load_test_server:create_app"],
        )
        for flag, expected in [
            ("--host", "127.0.0.1"),
            ("--port", "23456"),
            ("--workers", "1"),
            ("--loop", "asyncio"),
            ("--http", "h11"),
        ]:
            self.assertEqual(command[command.index(flag) + 1], expected)
        self.assertIn("--factory", command)
        self.assertIn("--no-access-log", command)
        for port in [0, -1, 65536, True, 2.5, "23456"]:
            with self.subTest(port=port), self.assertRaises(ValueError):
                load.child_command(port)

    def test_child_environment_blanks_credentials_and_disables_external_engines(self) -> None:
        """Clear inherited credentials and flags before importing NumPy or the application."""
        inherited = {
            "SMS_LAB_ADMIN_TOKEN": "authored-admin-secret",
            "DEEPSEEK_API_KEY": "authored-deepseek-secret",
            "TYPESAFE_API_KEY": "authored-typesafe-secret",
            "SMS_LAB_JEV_ENABLED": "true",
            "SMS_LAB_JEV_SERVER_KEY_ENABLED": "true",
            "SMS_LAB_PUBLIC": "false",
            "OPENBLAS_NUM_THREADS": "99",
        }
        with patch.dict(os.environ, inherited, clear=True):
            env = load.child_environment(Path("/authored/temporary/bundle"))
        for name in ["SMS_LAB_ADMIN_TOKEN", "DEEPSEEK_API_KEY", "TYPESAFE_API_KEY"]:
            self.assertEqual(env[name], "")
        for name in ["SMS_LAB_JEV_ENABLED", "SMS_LAB_JEV_SERVER_KEY_ENABLED"]:
            self.assertEqual(env[name], "false")
        for name in [
            "OMP_NUM_THREADS",
            "OPENBLAS_NUM_THREADS",
            "MKL_NUM_THREADS",
            "NUMEXPR_NUM_THREADS",
            "VECLIB_MAXIMUM_THREADS",
        ]:
            self.assertEqual(env[name], "1")
        self.assertEqual(env["SMS_LAB_ROOT"], "/authored/temporary/bundle")
        self.assertEqual(env["SMS_LAB_LOAD_TEST_HOST"], "127.0.0.1")
        self.assertEqual(env["SMS_LAB_PUBLIC"], "true")
        self.assertEqual(env["SMS_LAB_LOAD_TEST"], "true")
        self.assertFalse(any("authored-" in value for value in env.values()))

    def test_output_cannot_enter_any_formal_workspace_subdirectory(self) -> None:
        """Refuse formal data, model and nested output locations before creating a directory."""
        with TemporaryDirectory(prefix="textlab-load-output-test-") as temporary:
            project = Path(temporary).resolve() / "project"
            formal = project / "var"
            for destination in [formal, formal / "runs/new-output", formal / "load-output"]:
                with (
                    self.subTest(destination=destination),
                    patch.object(load, "PROJECT", project),
                    patch.object(load, "FORMAL", formal),
                    patch.object(sys, "argv", ["load", "--output", str(destination)]),
                    patch.object(load, "benchmark", side_effect=AssertionError("real benchmark")),
                    redirect_stderr(io.StringIO()),
                    self.assertRaises(SystemExit),
                ):
                    load.main()
                self.assertFalse(destination.exists())

    def test_bundle_copy_reads_only_selected_numeric_files_and_preserves_source(self) -> None:
        """Run the permanent audit hook in a child that owns only synthetic temporary files."""
        script = """
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from textlab import load_test as load
with TemporaryDirectory(prefix="textlab-load-copy-test-") as temporary:
    project = Path(temporary).resolve()
    load.PROJECT = project
    load.FORMAL = project / "var"
    source = load.FORMAL / "runs/authored-run"
    source.mkdir(parents=True)
    selected = {"run_id":"authored-run","kind":"mlp16","threshold":0.5}
    (load.FORMAL / "service.json").write_text(json.dumps({"selected":selected}))
    for name in load.MODEL_FILES:
        (source / name).write_bytes(("authored numeric placeholder " + name).encode())
    forbidden = load.FORMAL / "heldout-test.json"
    forbidden.write_text("authored forbidden placeholder")
    target = project / "independent-bundle"
    observed, protection = load.prepare_bundle(target)
    assert observed == selected
    assert load.model_fingerprints(protection["paths"]) == protection["before"]
    expected = {"load-test-bundle.json", "service.json"} | {
        "runs/authored-run/" + name for name in load.MODEL_FILES}
    actual = {p.relative_to(target).as_posix() for p in target.rglob("*") if p.is_file()}
    assert actual == expected
    try:
        forbidden.read_bytes()
    except PermissionError:
        pass
    else:
        raise AssertionError("Non-numeric formal file was readable")
    print("synthetic bundle copy passed")
"""
        completed = subprocess.run(
            [sys.executable, "-B", "-c", script], capture_output=True, text=True, check=False
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("synthetic bundle copy passed", completed.stdout)


class AccountingTests(unittest.TestCase):
    """Keep exact-window throughput distinct from full issued-cohort latency and errors."""

    def test_quantiles_interpolate_and_empty_cohorts_are_explicit(self) -> None:
        """Use the stated (N-1) interpolation without inventing zero latency for missing samples."""
        observed = load.quantiles([4.0, 1.0, 3.0, 2.0])
        self.assertAlmostEqual(observed["p50"], 2.5)
        self.assertAlmostEqual(observed["p95"], 3.85)
        self.assertAlmostEqual(observed["p99"], 3.97)
        self.assertEqual(load.quantiles([]), {"p50": None, "p95": None, "p99": None})
        self.assertEqual(load.quantiles([7.0]), {"p50": 7.0, "p95": 7.0, "p99": 7.0})

    def test_latency_record_is_immutable_and_millisecond_based(self) -> None:
        """Freeze each observation so later aggregation cannot mutate the timing trace."""
        record = load.RequestRecord(1, "authored", 0.1, 0.3, 200, "")
        self.assertAlmostEqual(record.latency_ms, 200)
        with self.assertRaises(FrozenInstanceError):
            record.status = 429

    def test_drain_included_in_latency_and_error_cohort_but_not_qps_window(self) -> None:
        """Do not inflate sixty-second throughput with requests completed during the drain."""
        records = [
            load.RequestRecord(0, "ham", 0.0, 0.05, 200, ""),
            load.RequestRecord(1, "spam", 0.1, 0.3, 429, "capacity_busy"),
            load.RequestRecord(0, "ham", 0.8, 1.0, 200, ""),
            load.RequestRecord(2, "spam", 0.9, 1.2, 200, ""),
            load.RequestRecord(3, "ham", 0.95, 1.25, 0, "timeout"),
        ]
        result = load.summarize_records(records, 1.0)
        self.assertEqual(result["issued_requests"], 5)
        self.assertEqual(result["completed_in_window"], 3)
        self.assertEqual(result["successful_in_window"], 2)
        self.assertEqual(result["successful_requests"], 3)
        self.assertEqual(result["error_requests"], 2)
        self.assertEqual(result["qps"], 3.0)
        self.assertEqual(result["successful_qps"], 2.0)
        self.assertEqual(result["drained_requests"], 2)
        self.assertEqual(result["error_rate"], 0.4)
        self.assertEqual(result["status_counts"], {"200": 3, "429": 1, "0": 1})
        self.assertEqual(
            {key: count for key, count in result["error_counts"].items() if count},
            {"capacity_busy": 1, "timeout": 1},
        )
        for key in ["minute_rate_limit", "other_429", "prediction_mismatch", "transport_error"]:
            self.assertEqual(result["error_counts"][key], 0)
        self.assertEqual(result["fixture_counts"], {"ham": 3, "spam": 2})
        self.assertAlmostEqual(result["latency_ms"]["successful"]["p50"], 200)
        self.assertAlmostEqual(result["latency_ms"]["successful"]["p95"], 290)
        self.assertAlmostEqual(result["latency_ms"]["successful"]["p99"], 298)
        self.assertAlmostEqual(result["latency_ms"]["all"]["p95"], 300)
        empty = load.summarize_records([], 60.0)
        self.assertEqual(empty["qps"], 0)
        self.assertEqual(empty["successful_qps"], 0)
        self.assertIsNone(empty["error_rate"])
        self.assertIsNone(empty["latency_ms"]["successful"]["p99"])

    def test_error_classification_keeps_429_reasons_and_prediction_mismatch_separate(self) -> None:
        """Count capacity rejections without confusing them with rate limits or incorrect scores."""
        expected = {"label": "ham", "spam_score": 0.25, "threshold": 0.5}
        examples = [
            (200, dict(expected), ""),
            (200, {**expected, "spam_score": 0.5}, "prediction_mismatch"),
            (200, None, "prediction_mismatch"),
            (503, {}, "http_503"),
            (429, {"detail": "Prediction capacity is busy"}, "capacity_busy"),
            (429, {"detail": "Please wait before predicting again"}, "minute_rate_limit"),
            (429, {"error": "Prediction capacity is busy"}, "other_429"),
            (429, None, "other_429"),
        ]
        for status, payload, wanted in examples:
            with self.subTest(status=status, payload=payload):
                self.assertEqual(load.response_error(status, payload, expected), wanted)


class ReadinessTests(unittest.IsolatedAsyncioTestCase):
    """Reject another service on the temporary port before sending any prediction workload."""

    async def test_pid_or_model_mismatch_is_not_treated_as_owned_readiness(self) -> None:
        """Require both the owned child PID and exact model metadata before issuing load."""
        selected = {"kind": "mlp16", "run_id": "authored-run"}
        request = httpx.Request("GET", "http://127.0.0.1:12345/api/health")
        for health, metrics in [
            ({"loaded": selected}, {"pid": 222}),
            ({"loaded": {**selected, "run_id": "other-run"}}, {"pid": 111}),
        ]:
            client = Mock()
            client.get = AsyncMock(
                side_effect=[
                    httpx.Response(200, json=health, request=request),
                    httpx.Response(200, json=metrics, request=request),
                ]
            )
            process = Mock(pid=111)
            process.poll.return_value = None
            with self.subTest(health=health, metrics=metrics), self.assertRaises(RuntimeError):
                await load.wait_ready(client, process, selected)
            self.assertEqual(client.get.await_count, 2)


if __name__ == "__main__":
    unittest.main()
