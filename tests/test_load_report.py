"""Synthetic renderer checks preserve load-test denominators and measured-resource boundaries."""

from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
import unittest
from xml.etree import ElementTree

from textlab.load_report import CHART_NAME, render_report


def fixture() -> dict[str, Any]:
    """Build distinct synthetic success, rejection and drain measurements."""
    return {
        "schema_version": 1,
        "recorded_at_utc": "2026-11-01T22:00:00+00:00",
        "source_commit": "a" * 40,
        "machine": {
            "os": "Synthetic OS",
            "architecture": "arm64",
            "cpu_model": "Authored CPU",
            "hardware_model": "Authored hardware",
            "physical_cores": 10,
            "logical_cores": 10,
            "memory_bytes": 16 * 1024**3,
            "hostname": "private-hostname-must-not-render",
        },
        "versions": {"python": "3.13", "httpx": "0.28.1"},
        "model": {"run_id": "authored-run", "kind": "mlp16", "threshold": 0.5},
        "server_config": {
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
            "production_resource_limits": {
                "cpu_quota": "50%",
                "memory_high_mib": 192,
                "memory_max_mib": 256,
                "tasks_max": 64,
                "limit_nofile": 2048,
            },
        },
        "workload": {
            "fixture_file": "examples/authored-load.json",
            "fixture_sha256": "b" * 64,
            "fixture_count": 6,
            "text_min_chars": 45,
            "text_max_chars": 160,
            "pattern": "closed-loop keep-alive round-robin authored SMS",
            "warmup_requests": 100,
        },
        "scenarios": [
            {
                "concurrency": 1,
                "duration_seconds": 60,
                "wall_seconds": 60.1,
                "drain_seconds": 0.1,
                "issued_requests": 121,
                "completed_in_window": 120,
                "successful_in_window": 120,
                "completed_requests": 121,
                "successful_requests": 121,
                "error_requests": 0,
                "status_counts": {"200": 121},
                "error_counts": {},
                "qps": 2.0,
                "successful_qps": 2.0,
                "error_rate": 0,
                "latency_ms": {
                    "successful": {"p50": 1, "p95": 2, "p99": 3},
                    "all": {"p50": 1, "p95": 2, "p99": 3},
                },
                "memory": {"server_peak_rss_mib": 48, "server_warmup_peak_rss_mib": 47},
                "cpu": {
                    "server_cpu_seconds": 30,
                    "average_server_cores": 0.5,
                    "client_cpu_seconds": 12,
                    "average_client_cores": 0.2,
                },
                "server_metrics_before": {"thread_count": 4, "threadpool_info": []},
                "server_metrics_after": {"thread_count": 5},
                "trace_file": "requests-c1.csv.gz",
                "trace_sha256": "c" * 64,
            },
            {
                "concurrency": 10,
                "duration_seconds": 60,
                "wall_seconds": 60.2,
                "drain_seconds": 0.2,
                "issued_requests": 610,
                "completed_in_window": 600,
                "successful_in_window": 400,
                "completed_requests": 610,
                "successful_requests": 405,
                "error_requests": 205,
                "status_counts": {"200": 405, "429": 205},
                "error_counts": {"capacity_busy": 205, "minute_rate_limit": 0},
                "qps": 10.0,
                "successful_qps": 400 / 60,
                "error_rate": 205 / 610,
                "latency_ms": {
                    "successful": {"p50": 2, "p95": 12, "p99": 20},
                    "all": {"p50": 0.5, "p95": 9, "p99": 15},
                },
                "memory": {"server_peak_rss_mib": 62.5, "server_warmup_peak_rss_mib": 47.5},
                "cpu": {
                    "server_cpu_seconds": 50,
                    "average_server_cores": 0.8,
                    "client_cpu_seconds": 20,
                    "average_client_cores": 0.3,
                },
                "server_metrics_before": {"thread_count": 4, "threadpool_info": []},
                "server_metrics_after": {"thread_count": 14},
                "trace_file": "requests-c10.csv.gz",
                "trace_sha256": "d" * 64,
            },
        ],
        "load_generator": {"peak_rss_mib": 85.25},
        "integrity": {"source_model_files_unchanged": True, "online_load_requests": 0},
        "ignored_secret": "private-value-must-not-render",
    }


class LoadReportTests(unittest.TestCase):
    def render(self, result: dict[str, Any]) -> tuple[str, str, set[str]]:
        """Render only into a temporary directory and return inspectable text and output names."""
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            document = root / "docs/LOAD_TEST.md"
            artifacts = root / "artifacts/load-test"
            render_report(result, docs_path=document, artifact_dir=artifacts)
            return (
                document.read_text(),
                (artifacts / CHART_NAME).read_text(),
                {path.name for path in artifacts.iterdir()},
            )

    def test_successful_latency_and_window_rates_do_not_include_fast_rejections_or_drain(
        self,
    ) -> None:
        """Keep success quantiles, final errors and window rates separately visible."""
        document, _chart, _files = self.render(fixture())
        self.assertIn("10.00 | 6.67 | 2.000 / 12.000 / 20.000 | 33.61%", document)
        self.assertNotIn("0.500 / 9.000 / 15.000", document)
        self.assertIn("600 / 400 | 610 / 405 / 205", document)
        self.assertIn("completed_in_window / duration_seconds", document)
        self.assertIn("error_requests / issued_requests", document)
        self.assertIn("不把排空期间才完成的请求加进窗口吞吐", document)
        self.assertIn("预热请求不进入吞吐、时延和错误率", document)
        self.assertIn("205 / 205 / 0", document)

    def test_resource_limits_and_causal_claims_are_scoped(self) -> None:
        """Separate process peaks, client contention, gates and unreplicated VPS caps."""
        document, _chart, _files = self.render(fixture())
        for phrase in (
            "不是线上或 VPS 容量结论",
            "本机没有等价复刻其 CPU 配额与内存限制",
            "CPUQuota=50%",
            "192 / 256 MiB",
            "85.25 MiB",
            "操作系统生命周期高水位",
            "不是定时采样得到的近似峰值",
            "不能单凭 429 推断",
            "压测器与应用同机竞争",
            "没有独立采集队列长度",
            "线程上限配置（非实测）",
            "不能据此声称实际 BLAS 已验证为单线程",
            "尚不能把 RSS 增长判为模型泄漏",
        ):
            self.assertIn(phrase, document)
        self.assertNotIn("private-hostname-must-not-render", document)
        self.assertNotIn("private-value-must-not-render", document)

    def test_outputs_are_one_report_one_standalone_svg_and_do_not_mutate_input(self) -> None:
        """Produce an accessible standalone plot without creating an unsolicited README artifact."""
        result = fixture()
        before = deepcopy(result)
        document, chart, files = self.render(result)
        self.assertEqual(result, before)
        self.assertEqual(files, {CHART_NAME})
        xml = ElementTree.fromstring(chart)
        self.assertEqual(xml.attrib["role"], "img")
        self.assertIn("Axis starts at zero", chart)
        self.assertIn("including errors", chart)
        self.assertIn("Valid successful responses", chart)
        self.assertIn("../artifacts/load-test/results.json", document)
        self.assertIn("requests-c10.csv.gz", document)
        self.assertIn(
            "-m textlab.load_test --output artifacts/load-test-new --duration 60 --warmup 100",
            document,
        )
        self.assertIn("-m textlab.load_test --replay", document)

    def test_nonmonotonic_drop_is_not_explained_away_by_a_tiny_rejection_count(self) -> None:
        """Describe lower throughput without inventing a CPU or thread-pool cause."""
        result = fixture()
        high = deepcopy(result["scenarios"][1])
        high.update(
            concurrency=50,
            qps=150 / 60,
            successful_qps=149 / 60,
            issued_requests=151,
            completed_in_window=150,
            successful_in_window=149,
            completed_requests=151,
            successful_requests=150,
            error_requests=1,
            error_rate=1 / 151,
            status_counts={"200": 150, "429": 1},
            error_counts={"capacity_busy": 1, "minute_rate_limit": 0},
        )
        high["latency_ms"]["successful"] = {"p50": 50, "p95": 100, "p99": 200}
        high["cpu"].update(average_server_cores=0.2, average_client_cores=0.7)
        high["server_metrics_after"]["thread_count"] = 10
        result["scenarios"].append(high)
        document, _chart, _files = self.render(result)
        self.assertIn("成功吞吐呈非单调变化", document)
        self.assertIn("低 62.75%", document)
        self.assertIn("这些拒绝计数不足以", document)
        self.assertIn("没有证明 CPU 饱和", document)
        self.assertIn("不能据此断言线程池已经饱和", document)
        self.assertIn("主瓶颈仍未定位", document)

    def test_resource_alias_and_numpy_build_identity_do_not_prove_thread_count(self) -> None:
        """Accept descriptor limits while separating build identity from runtime proof."""
        result = fixture()
        limits = result["server_config"]["production_resource_limits"]
        limits.pop("limit_nofile")
        limits["file_descriptors"] = 2048
        result["scenarios"][0]["server_metrics_before"]["numpy_backend"] = {
            "available": True,
            "identity_scope": "build identity only",
            "runtime_threads_verified": False,
            "numpy_version": "2.5.4",
            "blas": {"name": "accelerate", "version": "unknown", "found": True},
            "lapack": {"name": "accelerate", "version": "unknown", "found": True},
        }
        document, _chart, _files = self.render(result)
        self.assertIn("64 / 2048", document)
        self.assertIn("NumPy 构建 BLAS 身份 | accelerate / unknown", document)
        self.assertIn("runtime_threads_verified=`False`", document)
        self.assertIn("不证明运行时线程数", document)
        self.assertIn("不能据此声称实际 BLAS 已验证为单线程", document)

    def test_unknown_statistics_stay_unknown_instead_of_turning_into_zero(self) -> None:
        """Show missing measurements as absent instead of reporting successful zeroes."""
        result = fixture()
        result["scenarios"] = [
            {
                "concurrency": 50,
                "duration_seconds": 60,
                "qps": None,
                "successful_qps": None,
                "error_rate": None,
                "latency_ms": {"successful": {"p50": None, "p95": None, "p99": None}},
                "status_counts": None,
                "error_counts": None,
            }
        ]
        document, chart, _files = self.render(result)
        self.assertIn("50 | — | — | — / — / — | — | — / — / — | —", document)
        self.assertIn("缺测当成零拒绝", document)
        self.assertIn("错误分类未记录", document)
        self.assertIn("N/A", chart)

    def test_no_busy_breakdown_does_not_claim_a_capacity_cause(self) -> None:
        """An unclassified 429 cannot be promoted to a proven CPU or admission bottleneck."""
        result = fixture()
        result["scenarios"] = [result["scenarios"][1]]
        result["scenarios"][0]["error_counts"] = {"other_429": 205}
        document, _chart, _files = self.render(result)
        self.assertIn("现有分类未明确给出 busy 数", document)
        self.assertNotIn("明确的 busy 拒绝", document)


if __name__ == "__main__":
    unittest.main()
