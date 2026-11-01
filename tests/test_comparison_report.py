"""Synthetic report checks keep cached development comparisons distinct from new evaluations."""

from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
import xml.etree.ElementTree as ET

from tests.test_comparison_analysis import authored_row
from textlab.comparison_analysis import summarize
from textlab.comparison_report import CHART_NAME, render_report


def authored_plan(count: int) -> dict:
    """Describe an authored experiment without accessing a workspace or environment key."""
    return {
        "validation_count": count,
        "dataset_id": "synthetic-validation-dataset",
        "validation_sha256": "a" * 64,
        "local_model": {
            "kind": "mlp16",
            "threshold": 0.5,
            "run_id": "synthetic-model",
            "weights_sha256": "b" * 64,
        },
        "online_model_check": {"status": "passed", "loaded": {"run_id": "synthetic-model"}},
        "deepseek": {
            "model": "deepseek-flash",
            "endpoint": "https://api.deepseek.com/chat/completions",
            "system_prompt": 'Synthetic only: return JSON {"label":"ham"} or {"label":"spam"}.',
            "prompt_sha256": "c" * 64,
            "payload_parameters": {
                "model": "deepseek-flash",
                "temperature": 0,
                "thinking": {"type": "disabled"},
                "max_tokens": 32,
                "response_format": {"type": "json_object"},
                "stream": False,
            },
        },
        "bands": [[0.45, 0.55], [0.4, 0.6]],
        "price_schedule": {
            "peak": {"cache_hit": "0.04", "cache_miss": "2", "output": "8"},
            "offpeak": {"cache_hit": "0.02", "cache_miss": "1", "output": "4"},
            "source_url": "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/",
            "verified_date": "2026-11-01",
        },
        "dry_run": {
            "heuristic_peak_cost_yuan": "0.009",
            "heuristic_offpeak_cost_yuan": "0.0045",
        },
        "identity_sha256": "d" * 64,
        "source_sha256": {"textlab/llm_provider.py": "e" * 64},
        "concurrency": 4,
        "timeout_seconds": 15,
        "jev_status": "skipped_no_paid_jev_requests",
    }


def authored_budget() -> dict:
    """Separate confirmed charges and unknown reservations in the report fixture."""
    return {
        "known_cost_yuan": "0.004",
        "conservative_peak_yuan": "0.008",
        "unknown_reserved_yuan": "0",
        "cap_exposure_yuan": "0.008",
        "cap_yuan": "1",
        "attempt_count": 4,
        "known_usage_count": 4,
        "unknown_usage_count": 0,
        "stopped": False,
        "stop_reason": None,
        "usage_totals": {
            "prompt_tokens": 400,
            "completion_tokens": 24,
            "total_tokens": 424,
            "prompt_cache_hit_tokens": 320,
            "prompt_cache_miss_tokens": 80,
        },
        "returned_models": ["deepseek-flash"],
        "system_fingerprints": ["synthetic-fingerprint"],
        "data_integrity": {"protected_files_unchanged": True, "heldout_test_files_opened": 0},
    }


class ComparisonReportTests(unittest.TestCase):
    def render(self, rows: list[dict], budget: dict | None = None) -> tuple[str, str]:
        """Render text-free fixtures to a temporary report and read the generated artifacts."""
        with TemporaryDirectory() as directory:
            root = Path(directory)
            docs = root / "docs" / "LLM_VS_LOCAL.md"
            artifacts = root / "artifacts" / "llm-comparison"
            render_report(
                summarize(rows),
                authored_plan(len(rows)),
                authored_budget() if budget is None else budget,
                docs_path=docs,
                artifact_dir=artifacts,
            )
            return docs.read_text(), (artifacts / CHART_NAME).read_text()

    def test_report_pins_exact_prompt_and_separates_development_from_final_evidence(self) -> None:
        rows = [
            authored_row("one", "ham", 0.1, "ham"),
            authored_row("two", "ham", 0.5, "ham"),
            authored_row("three", "spam", 0.5, "spam"),
            authored_row("four", "spam", 0.1, "spam"),
        ]
        report, chart = self.render(rows)
        self.assertIn("4 条验证集开发比较", report)
        self.assertIn("## 冻结配置\n", report)
        self.assertIn(
            "后续外部数据测试原样使用以下提示词、请求参数和推荐区间，不再依据测试结果调整。",
            report,
        )
        self.assertIn("[frozen-config.json]", report)
        self.assertIn("spam 漏判由 1 变为 1 条", report)
        self.assertIn("<SMS_TEXT_ONLY>", report)
        self.assertIn("并非原始短信字节", report)
        self.assertIn("后续外部数据也先对同一输入调用 `textlab.data.normalize`", report)
        self.assertIn("验证集曾用于挑选本地模型，对本地模型略有利", report)
        self.assertIn("757 条最终测试未读取、未推断、未评估", report)
        self.assertIn("macro-F1 仅完成", report)
        self.assertIn("macro-F1 全量保守", report)
        self.assertIn("不等于冻结供应商权重", report)
        self.assertIn("不为每个区间重新付费调用", report)
        self.assertIn("未知预留不是已发生费用", report)
        self.assertIn("千条费用始终除以全量请求数", report)
        self.assertIn("[0.45, 0.55]", report)
        self.assertIn("[0.1, 0.9]", report)
        self.assertIn("最佳非零升级候选", report)
        blocks = report.split("```json\n")
        config = json.loads(blocks[-1].split("\n```", 1)[0])
        self.assertEqual(
            config["deepseek"]["system_prompt"], authored_plan(4)["deepseek"]["system_prompt"]
        )
        self.assertEqual(config["deepseek"]["prompt_sha256"], "c" * 64)
        self.assertEqual(config["cascade"]["recommended_band"], [0.45, 0.55])
        self.assertEqual(config["text_preprocessing"]["function"], "textlab.data.normalize")
        self.assertEqual(config["text_preprocessing"]["apply_to"], ["local", "deepseek"])
        self.assertEqual(
            config["text_preprocessing"]["steps"], ["casefold", "collapse whitespace", "trim"]
        )
        self.assertFalse(config["cascade"]["auto_retry"])
        self.assertTrue(config["cascade"]["band_endpoints_included"])
        self.assertIn("../artifacts/llm-comparison/escalation-frontier.svg", report)
        self.assertIn("--replay", report)
        self.assertIn("--live --verify-online --cap-yuan 1", report)
        parsed = ET.fromstring(chart)
        self.assertEqual(parsed.attrib["role"], "img")
        self.assertIn("1000 requested rows", chart)
        self.assertNotIn("<script", chart)

    def test_local_only_recommendation_still_names_observed_nonzero_candidate(self) -> None:
        rows = [
            authored_row("ham", "ham", 0.1, "ham"),
            authored_row("spam", "spam", 0.9, "spam"),
        ]
        report, _ = self.render(rows)
        self.assertIn("继续使用本地模型，不启用自动升级", report)
        self.assertIn("最佳非零升级候选为 [0.1, 0.9]", report)
        self.assertIn('"recommended_band": null', report)
        self.assertIn('"band": [', report)

    def test_incomplete_budget_stays_explicit_and_does_not_recommend_partial_band(self) -> None:
        rows = [
            authored_row("ham", "ham", 0.5, None, status="unknown", cost=None, failure=True),
            authored_row(
                "spam", "spam", 0.5, None, status="not_attempted", cost=None, remote_ms=None
            ),
        ]
        budget = authored_budget()
        budget.update(
            known_cost_yuan="0",
            unknown_reserved_yuan="0.1",
            known_usage_count=0,
            unknown_usage_count=1,
            stopped=True,
            stop_reason="synthetic_budget_stop",
        )
        report, chart = self.render(rows, budget)
        self.assertIn("尚无完整观察的非零升级区间", report)
        self.assertIn("预算账本已停止后续派发", report)
        self.assertIn("synthetic_budget_stop", report)
        self.assertIn("未派发样本仍保留在全量分母", report)
        self.assertIn("unknown 数（占全量）", report)
        self.assertIn("incomplete/ineligible", chart)
        self.assertNotIn("nan", chart.lower())

    def test_execution_provenance_and_usage_schema_are_preserved(self) -> None:
        rows = [
            authored_row("ham", "ham", 0.2, "ham"),
            authored_row("spam", "spam", 0.8, "spam"),
        ]
        plan = authored_plan(2)
        plan["execution_commit"] = "f" * 40
        plan["post_execution_repairs"] = [
            {
                "file": "textlab/comparison.py",
                "reason": "Persistence-only ledger schema correction",
                "executed_sha256": "1" * 64,
                "current_sha256": "2" * 64,
                "live_requests_repeated": 0,
            }
        ]
        budget = authored_budget()
        budget["usage"] = budget.pop("usage_totals")
        with TemporaryDirectory() as directory:
            root = Path(directory)
            docs = root / "docs" / "LLM_VS_LOCAL.md"
            render_report(
                summarize(rows), plan, budget, docs_path=docs, artifact_dir=root / "artifacts"
            )
            report = docs.read_text()
        self.assertIn("真实派发代码提交：`" + "f" * 40, report)
        self.assertIn("没有重复联网请求", report)
        self.assertIn("原始 source_sha256", report)
        self.assertIn("缓存命中 320", report)
        self.assertIn('"live_requests_repeated": 0', report)
        self.assertIn('"user_message_template"', report)

    def test_repeated_render_is_deterministic_and_does_not_mutate_inputs(self) -> None:
        rows = [
            authored_row("ham", "ham", 0.2, "ham"),
            authored_row("spam", "spam", 0.8, "spam"),
        ]
        summary = summarize(rows)
        plan = authored_plan(2)
        budget = authored_budget()
        before = deepcopy((summary, plan, budget))
        with TemporaryDirectory() as directory:
            root = Path(directory)
            docs = root / "docs" / "LLM_VS_LOCAL.md"
            artifacts = root / "artifacts" / "llm-comparison"
            render_report(summary, plan, budget, docs_path=docs, artifact_dir=artifacts)
            first = (
                docs.read_bytes(),
                (artifacts / CHART_NAME).read_bytes(),
                (artifacts / "frozen-config.json").read_bytes(),
            )
            config_from_report = json.loads(
                docs.read_text().split("```json\n")[-1].split("\n```", 1)[0]
            )
            self.assertEqual(
                json.loads((artifacts / "frozen-config.json").read_text()), config_from_report
            )
            render_report(summary, plan, budget, docs_path=docs, artifact_dir=artifacts)
            second = (
                docs.read_bytes(),
                (artifacts / CHART_NAME).read_bytes(),
                (artifacts / "frozen-config.json").read_bytes(),
            )
        self.assertEqual(first, second)
        self.assertEqual((summary, plan, budget), before)


if __name__ == "__main__":
    unittest.main()
