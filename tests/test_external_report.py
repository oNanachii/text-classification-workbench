"""Text-free fixtures verify frozen external reporting without touching a real study or API."""

from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
import xml.etree.ElementTree as ET

from tests.test_comparison_analysis import authored_row
from tests.test_comparison_report import authored_budget, authored_plan
from textlab.comparison_analysis import summarize
from textlab.external_report import CHART_NAME, FROZEN_PROMPT_SHA256, render_report


def external_plan(count: int) -> dict:
    """Declare synthetic cohort and source metadata before producing synthetic outcomes."""
    original = authored_plan(count)
    original["deepseek"]["prompt_sha256"] = FROZEN_PROMPT_SHA256
    return {
        "study_id": "synthetic-external-study",
        "external_count": count,
        "external_sha256": "a" * 64,
        "class_counts": {"ham": count // 2, "spam": count - count // 2},
        "dataset": {
            "id": "synthetic-source",
            "version": 1,
            "title": "Authored fixture metadata",
            "doi": "synthetic-doi",
            "contributors": ["Synthetic contributor"],
            "source_url": "https://example.invalid/synthetic-data",
            "license": "Synthetic license fixture",
            "license_url": "https://example.invalid/license",
            "archive_sha256": "b" * 64,
            "csv_sha256": "c" * 64,
        },
        "data_audit": {
            "ingestion": {
                "original_rows": 12,
                "ascii_letter_guard_rows_excluded": 1,
                "internal_exact_duplicate_rows_collapsed": 2,
                "conflicting_label_rows_quarantined": 0,
            },
            "dedup": {"uci_overlap_rows_excluded": 3},
            "reserved_identity_exclusion": {"reserved_overlap_rows_excluded": 2},
            "language_audit": {"non_latin_letter_rows": 1},
        },
        "frozen_config_sha256": "d" * 64,
        "validation_summary_sha256": "e" * 64,
        "plan_sha256": "f" * 64,
        "document_sha256": "1" * 64,
        "local_model": original["local_model"],
        "deepseek": original["deepseek"],
        "cascade_band": [0.2, 0.8],
        "text_preprocessing": {"function": "textlab.data.normalize"},
        "execution": {
            "concurrency": 4,
            "timeout_seconds": 15,
            "authorized_max_cap_yuan": "4",
            "freeze_plan_path": "docs/EXTERNAL_TEST_PLAN.md",
            "source_sha256": {"textlab/external_test.py": "2" * 64},
        },
        "price_schedule": original["price_schedule"],
        "dry_run": original["dry_run"],
    }


def external_summary(rows: list[dict]) -> dict:
    """Compute the one fixed synthetic band while dropping all selection outputs."""
    summary = summarize(rows, bands=((0.2, 0.8),))
    summary.pop("recommendation")
    summary.pop("best_observed_nonzero_band")
    summary.update(
        selection_performed=False,
        fixed_cascade_band=[0.2, 0.8],
        plan_sha256="f" * 64,
        external_sha256="a" * 64,
    )
    return summary


def external_rows() -> list[dict]:
    """Use four already-scored numeric rows without any SMS content."""
    return [
        authored_row("external-ham-one", "ham", 0.6, "ham"),
        authored_row("external-ham-two", "ham", 0.1, "ham"),
        authored_row("external-spam-one", "spam", 0.8, "spam"),
        authored_row("external-spam-two", "spam", 0.1, "spam"),
    ]


def validation_summary() -> dict:
    """Keep a separate development fixture so signed external changes are hand-checkable."""
    return summarize(
        [
            authored_row("validation-ham-one", "ham", 0.1, "spam"),
            authored_row("validation-ham-two", "ham", 0.2, "ham"),
            authored_row("validation-spam-one", "spam", 0.8, "spam"),
            authored_row("validation-spam-two", "spam", 0.9, "spam"),
        ]
    )


class ExternalReportTests(unittest.TestCase):
    def test_signed_differences_frozen_scope_and_original_plan_are_preserved(self) -> None:
        """Report both improvement and decline without performing a fresh selection."""
        summary = external_summary(external_rows())
        plan = external_plan(4)
        budget = authored_budget()
        budget.update(cap_yuan="4", authorized_max_cap_yuan="4")
        before = deepcopy((summary, plan, budget))
        with TemporaryDirectory() as directory:
            root = Path(directory)
            docs = root / "docs" / "EXTERNAL_TEST.md"
            artifacts = root / "artifacts" / "external-test"
            frozen = root / "docs" / "EXTERNAL_TEST_PLAN.md"
            frozen.parent.mkdir(parents=True)
            frozen.write_text("Immutable authored plan fixture.\n")
            artifacts.mkdir(parents=True)
            (artifacts / "evaluation-attempt.json").write_text(
                json.dumps({"execution_commit": "3" * 40, "started_at_utc": "synthetic-start"})
            )
            render_report(
                summary, plan, budget, validation_summary(), docs_path=docs, artifact_dir=artifacts
            )
            report = docs.read_text()
            chart = (artifacts / CHART_NAME).read_text()
            self.assertEqual(frozen.read_text(), "Immutable authored plan fixture.\n")
        self.assertEqual((summary, plan, budget), before)
        self.assertIn("4 条固定短信样本", report)
        self.assertIn("-0.50000", report)
        self.assertIn("+0.26667", report)
        self.assertIn("没有重新扫描区间", report)
        self.assertIn("不能证明 DeepSeek 预训练未见过公开语料", report)
        self.assertIn("不等于冻结供应商权重", report)
        self.assertIn("原来源含有 UCI 成分", report)
        self.assertIn("背书。\n\n| 预预测数据处理", report)
        self.assertIn("ASCII 字母规则", report)
        self.assertIn("仅是待验证假设", report)
        self.assertIn("正常误杀", report)
        self.assertIn("失败与 unknown 可重叠", report)
        self.assertIn("封存测试文件打开次数为 0", report)
        self.assertIn("EXTERNAL_TEST_PLAN.md", report)
        self.assertIn("[verification.json]", report)
        self.assertIn("--replay", report)
        self.assertNotIn("--live", report)
        self.assertIn('"execution_commit": "' + "3" * 40, report)
        self.assertIn(FROZEN_PROMPT_SHA256, report)
        parsed = ET.fromstring(chart)
        self.assertEqual(parsed.attrib["role"], "img")
        self.assertIn("Y axis starts at zero", chart)
        self.assertNotIn("<script", chart)

    def test_fixed_route_diagnostics_count_corrections_and_new_errors_without_a_search(
        self,
    ) -> None:
        """Use only published label/score rows to distinguish routed and unrouted mistakes."""
        rows = [
            authored_row("ham-outside", "ham", 0.1, "ham"),
            authored_row("ham-corrected", "ham", 0.6, "ham"),
            authored_row("spam-new-error", "spam", 0.7, "ham"),
            authored_row("spam-outside", "spam", 0.1, "spam"),
        ]
        with TemporaryDirectory() as directory:
            root = Path(directory)
            artifacts = root / "artifacts"
            artifacts.mkdir()
            (artifacts / "results.jsonl").write_text(
                "\n".join(json.dumps(row) for row in rows) + "\n"
            )
            docs = root / "EXTERNAL_TEST.md"
            render_report(
                external_summary(rows),
                external_plan(4),
                authored_budget(),
                validation_summary(),
                docs_path=docs,
                artifact_dir=artifacts,
            )
            report = docs.read_text()
        self.assertIn("冻结路径的实际错误分布（观察）", report)
        self.assertIn("本地共 2 条标签错误", report)
        self.assertIn("内 2 条，其中本地错误 1 条", report)
        self.assertIn("区间外 2 条，其中本地错误 1 条", report)
        self.assertIn("修正 1 条既有错误，同时引入 1 条新错误", report)
        self.assertIn("级联最终 2 条错误，相对本地净减少 0 条", report)
        self.assertIn("区间外 1 条本地错误全部是真实 spam", report)
        self.assertIn("spam_score < 0.2", report)
        self.assertIn("验证集的 0 / 2 变为外部的 1 / 2", report)
        self.assertIn("不据此扩大区间或重跑", report)
        self.assertLess(report.index("实际错误分布（观察）"), report.index("未验证假设"))

    def test_new_band_search_or_configuration_drift_is_rejected_before_writing(self) -> None:
        """Fail closed if an outcome table no longer represents the preregistered protocol."""
        base = external_summary(external_rows())
        plans = []
        for field, value in (("cascade_band", [0.1, 0.9]), ("external_count", 5)):
            plan = external_plan(4)
            plan[field] = value
            plans.append((base, plan))
        plan = external_plan(4)
        plan["deepseek"]["prompt_sha256"] = "9" * 64
        plans.append((base, plan))
        for update in (
            {"selection_performed": True},
            {"recommendation": {"band": [0.2, 0.8]}},
            {"best_observed_nonzero_band": {}},
            {"fixed_cascade_band": [0.1, 0.9]},
            {"plan_sha256": "8" * 64},
        ):
            summary = {**base, **update}
            plans.append((summary, external_plan(4)))
        summary = deepcopy(base)
        summary["cascades"].append(deepcopy(summary["cascades"][0]))
        plans.append((summary, external_plan(4)))
        for summary, plan in plans:
            with TemporaryDirectory() as directory:
                root = Path(directory)
                with self.assertRaises(ValueError):
                    render_report(
                        summary,
                        plan,
                        authored_budget(),
                        validation_summary(),
                        docs_path=root / "report.md",
                        artifact_dir=root / "artifacts",
                    )
                self.assertEqual(list(root.iterdir()), [])

    def test_incomplete_external_requests_keep_denominators_and_unknown_reservations(self) -> None:
        """Do not turn a failed or skipped call into a free successful external prediction."""
        rows = [
            authored_row("ham", "ham", 0.5, None, status="unknown", cost=None, failure=True),
            authored_row(
                "spam", "spam", 0.5, None, status="not_attempted", cost=None, remote_ms=None
            ),
        ]
        budget = authored_budget()
        budget.update(
            unknown_reserved_yuan="0.02",
            unknown_usage_count=1,
            stopped=True,
            stop_reason="synthetic-stop",
            cap_yuan="4",
        )
        with TemporaryDirectory() as directory:
            root = Path(directory)
            docs = root / "EXTERNAL_TEST.md"
            render_report(
                external_summary(rows),
                external_plan(2),
                budget,
                validation_summary(),
                docs_path=docs,
                artifact_dir=root / "artifacts",
            )
            report = docs.read_text()
        self.assertIn("0 / 2", report)
        self.assertIn("未知预留不是已发生费用", report)
        self.assertIn("未派发作为真实类别", report)
        self.assertIn("回退本地 2 条", report)
        self.assertIn("synthetic-stop", report)


if __name__ == "__main__":
    unittest.main()
