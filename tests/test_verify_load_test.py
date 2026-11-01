"""Reject altered load-test evidence using authored two-request traces and synthetic metrics."""

import copy
import csv
import gzip
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from scripts import verify_load_test as verify


class TraceVerificationTests(unittest.TestCase):
    """Exercise independent evidence checks without application imports or real load requests."""

    def setUp(self) -> None:
        """Allocate a tiny numeric trace with one in-window success and one drained rejection."""
        self.temporary = TemporaryDirectory(prefix="textlab-load-verifier-test-")
        self.directory = Path(self.temporary.name)
        self.rows = [
            [0, "authored-a", 0.0, 1.0, 200, ""],
            [0, "authored-b", 59.5, 60.5, 429, "capacity_busy"],
        ]
        self.case = {
            "concurrency": 1,
            "duration_seconds": 60.0,
            "trace_file": "requests-c1.csv.gz",
            "issued_requests": 2,
            "completed_requests": 2,
            "completed_in_window": 1,
            "successful_in_window": 1,
            "successful_requests": 1,
            "error_requests": 1,
            "drained_requests": 1,
            "qps": 1 / 60,
            "successful_qps": 1 / 60,
            "error_rate": 0.5,
            "status_counts": {"0": 0, "200": 1, "429": 1},
            "error_counts": dict.fromkeys(verify.ERROR_KEYS, 0) | {"capacity_busy": 1},
            "fixture_counts": {"authored-a": 1, "authored-b": 1},
            "latency_ms": {
                "all": {"p50": 1000.0, "p95": 1000.0, "p99": 1000.0},
                "successful": {"p50": 1000.0, "p95": 1000.0, "p99": 1000.0},
            },
            "wall_seconds": 60.6,
            "drain_seconds": 0.6,
        }
        self.write_trace()

    def tearDown(self) -> None:
        """Remove only the temporary numeric fixture after each independent rejection case."""
        self.temporary.cleanup()

    def write_trace(self) -> None:
        """Rehash authored mutations so semantic tests do not fail only at the checksum gate."""
        path = self.directory / self.case["trace_file"]
        with gzip.open(path, "wt", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(verify.TRACE_COLUMNS)
            writer.writerows(self.rows)
        self.case["trace_sha256"] = verify.digest(path.read_bytes())

    def verify(self) -> dict:
        """Run the standalone trace checks without invoking the benchmark or resource collector."""
        return verify.verify_trace(
            self.case, self.directory, {"authored-a", "authored-b"}, verify.Checks()
        )

    def test_valid_trace_separates_drain_from_window_and_counts_rejection(self) -> None:
        """Keep drained failures in cohort denominators while excluding them from window QPS."""
        receipt = self.verify()
        self.assertEqual(receipt["completed_in_window"], 1)
        self.assertEqual(receipt["drained_requests"], 1)
        self.assertEqual(receipt["qps"], 1 / 60)
        self.assertEqual(receipt["successful_qps"], 1 / 60)
        self.assertEqual(receipt["error_rate"], 0.5)
        self.assertEqual(receipt["maximum_inflight"], 1)

    def test_trace_checksum_and_reported_qps_cannot_be_changed(self) -> None:
        """Reject both changed bytes and a report that divides by a more favorable duration."""
        original = self.case["trace_sha256"]
        self.case["trace_sha256"] = "0" * 64
        with self.assertRaisesRegex(verify.EvidenceError, "SHA256 differs"):
            self.verify()
        self.case["trace_sha256"] = original
        self.case["qps"] = 2 / 60
        with self.assertRaisesRegex(verify.EvidenceError, "QPS differs"):
            self.verify()

    def test_overlap_out_of_range_worker_and_late_issue_are_rejected(self) -> None:
        """Reject impossible closed-loop traces even when their compressed checksums match."""
        originals = copy.deepcopy(self.rows)
        mutations = [(0, 0.5), (1, 59.5), (0, 60.0)]
        for worker, begin in mutations:
            self.rows = copy.deepcopy(originals)
            self.rows[1][0] = worker
            self.rows[1][2] = begin
            self.write_trace()
            with self.subTest(worker=worker, begin=begin), self.assertRaises(verify.EvidenceError):
                self.verify()

    def test_http_rejections_cannot_be_marked_successful(self) -> None:
        """Require status/error consistency before trusting a harness success flag."""
        self.rows[1][-1] = ""
        self.write_trace()
        with self.assertRaisesRegex(verify.EvidenceError, "status/error"):
            self.verify()

    def test_independent_percentiles_use_linear_interpolation(self) -> None:
        """Check interpolation separately so a shared production implementation is unnecessary."""
        values = verify.percentiles([1.0, 4.0, 2.0, 3.0])
        self.assertEqual(values["p50"], 2.5)
        self.assertAlmostEqual(values["p95"], 3.85)
        self.assertAlmostEqual(values["p99"], 3.97)
        self.assertEqual(verify.percentiles([]), {"p50": None, "p95": None, "p99": None})


class ResourceVerificationTests(unittest.TestCase):
    """Check saved metric arithmetic without collecting process or kernel measurements."""

    def test_resource_pid_rss_and_cpu_arithmetic_are_checked(self) -> None:
        """Reject a foreign process identity or altered average-core calculation."""
        limits = {
            "test_only": True,
            "host": "127.0.0.1",
            "public_read_only": True,
            "external_decision_enabled": False,
            "requests_per_minute": 1_000_000_000,
            "production_requests_per_minute": 30,
            "prediction_slots": 2,
            "run_id": "authored-run",
        }
        before = {
            "pid": 111,
            "limits": limits,
            "thread_count": 1,
            "thread_count_scope": "authored-snapshot",
            "threadpool_info_available": False,
            "threadpool_info": [],
            "numpy_backend": {
                "available": True,
                "numpy_version": "authored-version",
                "runtime_threads_verified": False,
            },
            "peak_rss_bytes": 1_048_576,
            "user_cpu_seconds": 1.0,
            "system_cpu_seconds": 2.0,
        }
        after = copy.deepcopy(before)
        after.update(peak_rss_bytes=2_097_152, user_cpu_seconds=3.0, system_cpu_seconds=4.0)
        case = {
            "local_pid": 111,
            "server_metrics_before": before,
            "server_metrics_after": after,
            "wall_seconds": 60.0,
            "memory": {
                "server_warmup_peak_rss_mib": 1.0,
                "server_peak_rss_mib": 2.0,
            },
            "cpu": {
                "server_cpu_seconds": 4.0,
                "average_server_cores": 4.0 / 60,
                "client_cpu_seconds": 8.0,
                "average_client_cores": 8.0 / 60,
            },
        }
        arguments = ({"run_id": "authored-run"}, {"numpy": "authored-version"})
        receipt = verify.verify_resources(case, *arguments, verify.Checks())
        self.assertEqual(receipt["server_peak_rss_mib"], 2.0)
        self.assertEqual(receipt["server_cpu_seconds"], 4.0)
        after["pid"] = 222
        with self.assertRaisesRegex(verify.EvidenceError, "PID differs"):
            verify.verify_resources(case, *arguments, verify.Checks())
        after["pid"] = 111
        case["cpu"]["average_server_cores"] = 1.0
        with self.assertRaisesRegex(verify.EvidenceError, "average-core"):
            verify.verify_resources(case, *arguments, verify.Checks())


if __name__ == "__main__":
    unittest.main()
