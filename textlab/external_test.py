"""Freeze and run one external SMS test without opening sealed test texts or changing models."""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import unicodedata
import zipfile
from typing import Any

import httpx

from textlab import llm_provider
from textlab.comparison import (
    canonical,
    dispatch,
    fingerprints,
    local_results,
    online_model_check,
    protect_formal_files,
    read_key,
    write_json,
)
from textlab.comparison_analysis import summarize
from textlab.comparison_budget import Ledger, estimate_request, PEAK_RATES, OFFPEAK_RATES
from textlab.data import normalize
from textlab.external_data import (
    load_external_csv,
    exclude_uci_overlap,
    exclude_reserved_identities,
)
from textlab.safe_model import SafeModel

PROJECT = Path(__file__).resolve().parents[1]
FORMAL = PROJECT / "var"
PUBLIC = PROJECT / "artifacts/external-test"
PLAN_DOCUMENT = PROJECT / "docs/EXTERNAL_TEST_PLAN.md"
REPORT = PROJECT / "docs/EXTERNAL_TEST.md"
FROZEN = PROJECT / "artifacts/llm-comparison/frozen-config.json"
VALIDATION_SUMMARY = PROJECT / "artifacts/llm-comparison/summary.json"
STUDY_ID = "mendeley-f45bkkt8pr-v1-external-20261101"
DOWNLOAD_URL = (
    "https://data.mendeley.com/public-files/datasets/f45bkkt8pr/files/"
    "edb361de-918d-469f-9106-e84823830665/file_downloaded"
)
ARCHIVE_SHA256 = "9bbf3188fdad81495d8e82825648b9b63b53fc86841a3d26c02629990b233cc3"
CSV_SHA256 = "649844f1c62a6b27e145eaf17a65f7010c56c390e11a794ea8329993a05ba71e"
LABEL_MAPPING = {
    "ham": "ham",
    "spam": "spam",
    "Spam": "spam",
    "smishing": "spam",
    "Smishing": "spam",
}
CAP_YUAN = Decimal("4")
CONCURRENCY = 4
COHORT_EXPECTED_COUNT = 733


def sha(content: bytes) -> str:
    """Bind artifacts to exact bytes without publishing message contents."""
    return hashlib.sha256(content).hexdigest()


def immutable_json(path: Path, value: Any) -> None:
    """Create a frozen artifact once and refuse any later replacement."""
    content = json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False).encode() + b"\n"
    immutable_bytes(path, content)


def immutable_bytes(path: Path, content: bytes) -> None:
    """Keep frozen plans stable across reruns rather than overwriting evidence."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as stream:
            stream.write(content)
    except FileExistsError:
        if path.is_symlink() or path.read_bytes() != content:
            raise ValueError(
                "Frozen external-test artifact differs; it may not be replaced"
            ) from None


def download_source(workspace: Path) -> Path:
    """Download only the pinned public archive and extract its one CSV without running code."""
    raw = workspace / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    archive = raw / "Dataset_5971.zip"
    if not archive.exists():
        with httpx.Client(trust_env=False, timeout=60, follow_redirects=True) as client:
            response = client.get(DOWNLOAD_URL)
            response.raise_for_status()
        immutable_bytes(archive, response.content)
    content = archive.read_bytes()
    if sha(content) != ARCHIVE_SHA256:
        raise ValueError("External archive differs from the pinned official file checksum")
    with zipfile.ZipFile(io.BytesIO(content)) as bundle:
        if bundle.namelist() != ["Dataset_5971.csv"]:
            raise ValueError("External archive contents differ from the pinned dataset")
        csv_content = bundle.read("Dataset_5971.csv")
    if sha(csv_content) != CSV_SHA256:
        raise ValueError("External CSV checksum differs")
    target = raw / "Dataset_5971.csv"
    immutable_bytes(target, csv_content)
    return target


def input_paths() -> set[Path]:
    """Allow training/validation data and ID-only split metadata, never held-out SMS files."""
    return {
        (FORMAL / name).resolve() for name in ("dataset.json", "service.json", "split-index.json")
    }


def configure_guard(workspace: Path) -> tuple[dict, set[Path]]:
    """Install the formal-data denylist before reading even the permitted metadata."""
    allowed = input_paths()
    evidence = protect_formal_files(workspace, allowed)
    selected = json.loads((FORMAL / "service.json").read_text())["selected"]
    directory = FORMAL / "runs" / selected["run_id"]
    if directory.resolve().parent != (FORMAL / "runs").resolve():
        raise ValueError("Invalid model directory")
    allowed.update(
        (directory / name).resolve() for name in ("model.json", "weights.npz", "vocabulary.json")
    )
    return evidence, allowed


def prepare_cohort(workspace: Path) -> tuple[list[dict], dict]:
    """Apply all input-only exclusions before any local or external prediction is made."""
    import csv

    ingestion = load_external_csv(download_source(workspace), "TEXT", "LABEL", LABEL_MAPPING)
    dataset = json.loads((FORMAL / "dataset.json").read_text())
    references = [
        {**row, "split": split} for split in ("train", "validation") for row in dataset[split]
    ]
    with (PROJECT / "examples/authored_sms.csv").open() as stream:
        authored = []
        for row in csv.DictReader(stream):
            text = normalize(row["text"])
            authored.append({"id": sha(text.encode()), "text": text, "split": "train"})
    dedup = exclude_uci_overlap(ingestion["rows"], references + authored)
    index = json.loads((FORMAL / "split-index.json").read_text())["index"]
    if any({"text", "raw_text", "label"} & set(row) for row in index):
        raise ValueError("Split metadata must contain no message text or labels")
    reserved = [row for row in index if row["split"] == "test"]
    quarantined = exclude_reserved_identities(
        dedup["rows"],
        {row["id"] for row in reserved},
        {row["template_hash"] for row in reserved},
    )
    rows = quarantined["rows"]
    if len(rows) != COHORT_EXPECTED_COUNT or {row["label"] for row in rows} != {"ham", "spam"}:
        raise ValueError("Pinned external cohort count/classes differ from the preflight audit")
    non_latin = sum(
        any(
            unicodedata.category(c).startswith("L") and "LATIN" not in unicodedata.name(c, "")
            for c in row["text"]
        )
        for row in rows
    )
    audit = {
        "ingestion": ingestion["audit"],
        "dedup": dedup["audit"],
        "reserved_identity_exclusion": quarantined["audit"],
        "uci_train_validation_reference_count": len(references),
        "additional_authored_training_reference_count": len(authored),
        "reserved_test_metadata_records": len(reserved),
        "final_count": len(rows),
        "final_class_counts": dict(Counter(row["label"] for row in rows)),
        "language_audit": {
            "non_latin_letter_rows": non_latin,
            "scope": "Script audit only, not a semantic language detector",
        },
        "exclusions": [
            {key: value for key, value in item.items() if key != "source_rows"}
            for item in ingestion["exclusions"] + dedup["exclusions"] + quarantined["exclusions"]
        ],
    }
    return rows, audit


def validate_frozen(frozen: dict) -> None:
    """Reject any drift in the previously selected prompt, model, threshold or cascade band."""
    if frozen["cascade"]["recommended_band"] != [0.2, 0.8]:
        raise ValueError("Frozen cascade differs")
    if frozen["local"]["kind"] != "mlp16" or frozen["local"]["threshold"] != 0.5:
        raise ValueError("Frozen local model differs")
    if frozen["deepseek"]["system_prompt"] != llm_provider.SYSTEM_PROMPT:
        raise ValueError("Frozen system prompt differs")
    if frozen["deepseek"]["prompt_sha256"] != llm_provider.PROMPT_SHA256:
        raise ValueError("Frozen prompt digest differs")
    parameters = llm_provider.build_payload("Authored configuration check.")
    parameters.pop("messages")
    if parameters != frozen["deepseek"]["payload_parameters"]:
        raise ValueError("Frozen request parameters differ")


def plan_document(plan: dict) -> bytes:
    """Describe the complete preregistered protocol without predictions or SMS examples."""
    audit = plan["data_audit"]
    text = f"""# 外部公开短信测试：冻结计划

本文件在任何本地或付费模型预测之前冻结并提交 Git。之后不修改；结果只写入
`docs/EXTERNAL_TEST.md`。计划摘要：`{plan["plan_sha256"]}`。

## 来源、许可与标签

Mishra 与 Soni 的 Mendeley Data V1（2022-06-20），DOI `10.17632/f45bkkt8pr.1`。
来源与下载见 `data/external/README.md`。提供方标注 CC BY 4.0，遵守署名、许可链接、
变更说明和不暗示作者背书的条件。本次仅发布 ID、标签、数值、排除计数与汇总，不发布短信。
CSV 标签精确映射固定为 `{json.dumps(LABEL_MAPPING, ensure_ascii=False)}`。
它将 spam、Spam、smishing、Smishing 合并为 spam，ham 保持 ham。

## 预处理、去重和固定样本

两种模型均接收 `textlab.data.normalize` 归一化文本：casefold、合并空白、去首尾空白。
空白、无效列或标签、超过 2000 字符、没有 `[a-zA-Z]{{2}}` 的行排除；此字符规则不是语言识别。
内部归一化完全重复合并，二分类标签冲突整组隔离。不按模型输出排除任何样本。

去重参照 UCI v2 的 3619 条初始训练、783 条验证，并额外包括 4 条已用于训练的自编烟测短信。
复用原 `template_key` 和字符 5-gram：先归一化完全相同，再非空模板相同；其余仅比较模板
首二／末二 token 共享候选，两条规范化文本均不少于 35 字符，gram 数量比和 Jaccard 均 ≥0.85
则排除。它是既有阻塞词法规则，不保证全量近重复、传递变体或语义改写均被识别。

该外部来源含 UCI 成分，另用旧划分的 ID 与模板 SHA256 元数据隔离历史测试完全重复或
同模板样本。不打开 757 条测试原文、排除原文库、旧快照或最终测试数据，不进行旧测试推断。
没有测试原文，故不声称完成对旧测试的字符近重复或语义审计。

预预测审计：原始 {audit["ingestion"]["original_rows"]} 条；输入规则排除
{audit["ingestion"]["ascii_letter_guard_rows_excluded"]} 条，内部完全重复折叠
{audit["ingestion"]["internal_exact_duplicate_rows_collapsed"]} 条，训练／验证规则重叠排除
{audit["dedup"]["uci_overlap_rows_excluded"]} 条，历史测试 ID／模板隔离
{audit["reserved_identity_exclusion"]["reserved_overlap_rows_excluded"]} 条。
固定全部剩余 {plan["external_count"]} 条：{json.dumps(plan["class_counts"], ensure_ascii=False)}。
不抽样、不平衡重采样、不根据结果更换数据集。公开 `cohort.json` 固定样本 ID 与标签；
规范化内容摘要 `{plan["external_sha256"]}`。

## 比较方案和指标

1. 本地：既有 `v2-import-reviewed-seed42 / mlp16`，阈值 0.5；权重、词表不改。
2. DeepSeek：原 `docs/LLM_VS_LOCAL.md` 冻结提示和参数，`deepseek-flash`，temperature 0、
   thinking disabled、JSON 单标签、max_tokens 32，单次整段超时 15 秒；无真实标签或 ID 入提示。
3. 级联：只使用冻结的包含端点 `[0.2, 0.8]`，其余保留本地；未知或失败回退本地。
   不扫区间，不重新选阈值或提示。级联复用同一批 DeepSeek 输出反事实回放，不能当实测串行延迟。

主指标为 macro-F1（固定 ham/spam 两类）、spam precision/recall、正常短信误杀数及其分母、
交给大模型的比例、总 token 用量与费用。附混淆计数、有效覆盖、失败／未知／未派发、
p50/p95。失败与未知可能重叠，不相加。有效子集指标单列，全量保守指标将弃权作为真实类别
漏判，不编造预测标签；级联未知回退本地。和原验证集的相同三个方案比较有符号变化，
不假设一定下降；样本量、类别比例和来源不同，原始误杀数量不能单独比较。

## 一次性与费用

总授权上限人民币 4 元；默认先 dry-run，峰价启发估算
`{plan["dry_run"]["heuristic_peak_cost_yuan"]}` 元。
每条发送前持久化峰价保守预留，已确认峰价费用与所有在途／未知预留合计不超过 4 元才派发。
真实费用按 API 返回输入命中／未命中和输出 token、北京时间派发时段官方费率核算；
不是独立账单。未知用量保留完整预留，超时／失败不重试。并发 4，一条 ID 只派发一次；
全局尝试记录拒绝第二轮。异常中断也保留逐请求账本与部分分母，不能用另一个目录重跑该计划。

## 解释范围

仅说明本来源、此固定过滤批次的结果；不推广到线上流量、其他国家／时间或中文短信。
去重后是按已列规则未检出训练／验证重叠，不是语义新颖性证明；公开数据是否进入 DeepSeek
预训练未知，别名也不是不可变远端权重。截图 OCR、来源标注噪声、去重后类别比例及
短信写法变化可作为下降的可能原因，不根据结果声称因果。没有独立人工复标或线上验收。

## 机器可读冻结身份

完整身份、文件摘要、去重摘要、参数与费用规则见 `artifacts/external-test/plan.json`；
CSV、模型及程序摘要改变即拒绝执行。该 JSON 的 `plan_sha256` 对除文档摘要和自身摘要外的
配置计算；本 Markdown 的独立 SHA256 记录在 JSON 中，避免循环自哈希。
"""
    return text.encode()


def prepare(workspace: Path, verify_online: bool) -> dict:
    """Freeze all selection and execution inputs while deliberately making no predictions."""
    audit_access, allowed = configure_guard(workspace)
    protected = fingerprints(allowed)
    frozen = json.loads(FROZEN.read_text())
    validate_frozen(frozen)
    selected = json.loads((FORMAL / "service.json").read_text())["selected"]
    if selected != frozen["local"]:
        raise ValueError("Selected local artifact differs from the validation-frozen model")
    rows, audit = prepare_cohort(workspace)
    estimates = [estimate_request(llm_provider.build_payload(row["text"])) for row in rows]
    input_estimate = sum(row["estimated_input_tokens"] for row in estimates)
    output_limit = len(rows) * llm_provider.MAX_OUTPUT_TOKENS
    cost = (
        Decimal(input_estimate) * Decimal(PEAK_RATES["cache_miss"])
        + Decimal(output_limit) * Decimal(PEAK_RATES["output"])
    ) / 1_000_000
    sources = [
        "textlab/external_test.py",
        "textlab/external_data.py",
        "textlab/comparison.py",
        "textlab/comparison_budget.py",
        "textlab/comparison_analysis.py",
        "textlab/llm_provider.py",
        "textlab/safe_model.py",
        "textlab/data.py",
        "textlab/dataflow.py",
        "textlab/decision.py",
    ]
    config = {
        "schema_version": 1,
        "study_id": STUDY_ID,
        "external_count": len(rows),
        "external_sha256": sha(canonical(rows)),
        "class_counts": dict(Counter(row["label"] for row in rows)),
        "dataset": {
            "id": "f45bkkt8pr",
            "version": 1,
            "doi": "10.17632/f45bkkt8pr.1",
            "title": "SMS PHISHING DATASET FOR MACHINE LEARNING AND PATTERN RECOGNITION",
            "contributors": ["Sandhya Mishra", "Devpriya Soni"],
            "source_url": "https://data.mendeley.com/datasets/f45bkkt8pr/1",
            "license": "CC BY 4.0",
            "license_url": "https://creativecommons.org/licenses/by/4.0/",
            "archive_sha256": ARCHIVE_SHA256,
            "csv_sha256": CSV_SHA256,
            "download_url": DOWNLOAD_URL,
            "csv_encoding": "utf-8-sig",
        },
        "label_mapping": LABEL_MAPPING,
        "data_audit": {key: value for key, value in audit.items() if key != "exclusions"},
        "audit_sha256": sha(canonical(audit)),
        "frozen_config_sha256": sha(FROZEN.read_bytes()),
        "validation_summary_sha256": sha(VALIDATION_SUMMARY.read_bytes()),
        "local_model": selected,
        "deepseek": frozen["deepseek"],
        "cascade_band": frozen["cascade"]["recommended_band"],
        "text_preprocessing": frozen["text_preprocessing"],
        "execution": {
            "concurrency": CONCURRENCY,
            "timeout_seconds": 15,
            "authorized_max_cap_yuan": "4",
            "cap_yuan": "4",
            "no_retry": True,
            "freeze_plan_path": "docs/EXTERNAL_TEST_PLAN.md",
            "source_sha256": {name: sha((PROJECT / name).read_bytes()) for name in sources},
        },
        "online_model_check": online_model_check(selected)
        if verify_online
        else {"status": "not_checked"},
        "protected_input_fingerprints": protected,
        "data_access_audit": audit_access,
        "price_schedule": {
            "peak": PEAK_RATES,
            "offpeak": OFFPEAK_RATES,
            "source_url": "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/",
            "verified_date": "2026-11-01",
        },
        "dry_run": {
            "heuristic_peak_cost_yuan": str(cost),
            "heuristic_offpeak_cost_yuan": str(cost / 2),
            "input_token_estimate": input_estimate,
            "maximum_output_tokens": output_limit,
            "method": "JSON UTF-8 bytes / 4, all input cache misses, maximum output; "
            "heuristic not a tokenizer guarantee",
        },
    }
    plan = {**config, "plan_sha256": sha(canonical(config))}
    document = plan_document(plan)
    plan["document_sha256"] = sha(document)
    immutable_bytes(PLAN_DOCUMENT, document)
    for destination in (workspace, PUBLIC):
        immutable_json(destination / "plan.json", plan)
        immutable_json(destination / "audit.json", audit)
        immutable_json(
            destination / "cohort.json",
            [{"sample_id": row["id"], "truth": row["label"]} for row in rows],
        )
    immutable_json(workspace / "cohort.private.json", rows)
    if fingerprints(allowed) != protected:
        raise ValueError("Formal inputs changed during preparation")
    return plan


def load_plan(workspace: Path, *, check_committed: bool) -> dict:
    """Verify frozen file identities and optionally require pre-evaluation Git evidence."""
    plan = json.loads((workspace / "plan.json").read_text())
    config = {
        key: value for key, value in plan.items() if key not in {"plan_sha256", "document_sha256"}
    }
    if sha(canonical(config)) != plan["plan_sha256"]:
        raise ValueError("Frozen plan identity differs")
    if sha(PLAN_DOCUMENT.read_bytes()) != plan["document_sha256"]:
        raise ValueError("The preregistered Markdown plan changed")
    if sha(FROZEN.read_bytes()) != plan["frozen_config_sha256"]:
        raise ValueError("Original validation-frozen configuration changed")
    validate_frozen(json.loads(FROZEN.read_text()))
    if sha(VALIDATION_SUMMARY.read_bytes()) != plan["validation_summary_sha256"]:
        raise ValueError("The reference validation report changed")
    for name, expected in plan["execution"]["source_sha256"].items():
        if sha((PROJECT / name).read_bytes()) != expected:
            raise ValueError("A preregistered execution source changed: " + name)
    if check_committed:
        for relative in ("docs/EXTERNAL_TEST_PLAN.md", "artifacts/external-test/plan.json"):
            committed = subprocess.check_output(["git", "show", "HEAD:" + relative], cwd=PROJECT)
            if committed != (PROJECT / relative).read_bytes():
                raise ValueError("Plan must be committed before the one-shot evaluation")
            if (
                relative.endswith("plan.json")
                and committed != (workspace / "plan.json").read_bytes()
            ):
                raise ValueError("Workspace plan differs from the committed preregistration")
    return plan


def publish(
    workspace: Path, plan: dict, ledger: Ledger | None, integrity: dict | None = None
) -> dict:
    """Report only three prespecified strategies, never select again on external outcomes."""
    if ledger is None:
        rows = [json.loads(line) for line in (workspace / "results.jsonl").read_text().splitlines()]
        budget = json.loads((workspace / "budget.json").read_text())
    else:
        local = json.loads((workspace / "local-results.json").read_text())
        rows = [
            {
                **row,
                "deepseek": ledger.records.get(
                    row["sample_id"],
                    {
                        "status": "not_attempted",
                        "label": None,
                        "elapsed_ms": None,
                        "usage": None,
                        "cost_yuan": None,
                        "reserved_yuan": "0",
                        "failure": False,
                    },
                ),
            }
            for row in local
        ]
        budget = ledger.summary()
        usage = sum((Counter(row["deepseek"].get("usage") or {}) for row in rows), Counter())
        budget["usage"] = {key: usage[key] for key in llm_provider.USAGE_FIELDS}
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
    summary = summarize(rows, bands=(tuple(plan["cascade_band"]),))
    summary.pop("recommendation")
    summary.pop("best_observed_nonzero_band")
    summary.update(
        selection_performed=False,
        fixed_cascade_band=plan["cascade_band"],
        budget=budget,
        plan_sha256=plan["plan_sha256"],
        external_sha256=plan["external_sha256"],
    )
    for destination in {workspace, PUBLIC}:
        write_json(destination / "summary.json", summary)
        write_json(destination / "budget.json", budget)
        (destination / "results.jsonl").write_bytes(
            b"\n".join(canonical(row) for row in rows) + b"\n"
        )
        if ledger is not None and destination != workspace:
            write_json(
                destination / "ledger.json",
                {"plan_sha256": plan["plan_sha256"], "records": ledger.records},
            )
    from textlab.external_report import render_report

    render_report(
        summary,
        plan,
        budget,
        json.loads(VALIDATION_SUMMARY.read_text()),
        docs_path=REPORT,
        artifact_dir=PUBLIC,
    )
    return summary


def reserve_attempt(workspace: Path, attempt: dict) -> None:
    """Fsync strict exclusive attempt markers so a crash cannot authorize another evaluation."""
    content = json.dumps(attempt, indent=2, ensure_ascii=False).encode() + b"\n"
    for directory in (workspace, PUBLIC):
        path = directory / "evaluation-attempt.json"
        try:
            with path.open("xb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError:
            raise ValueError("This external study already has an attempt; no live rerun") from None
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def execute(workspace: Path, key_file: Path | None) -> dict:
    """Reserve one study attempt before any prediction and refuse all later live reruns."""
    access, allowed = configure_guard(workspace)
    plan = load_plan(workspace, check_committed=True)
    if plan["online_model_check"]["status"] != "passed":
        raise ValueError("The pre-evaluation online model identity was not verified")
    if fingerprints(allowed) != plan["protected_input_fingerprints"]:
        raise ValueError("Formal inputs differ from the frozen plan")
    rows = json.loads((workspace / "cohort.private.json").read_text())
    if len(rows) != plan["external_count"] or sha(canonical(rows)) != plan["external_sha256"]:
        raise ValueError("External cohort changed after preregistration")
    if Decimal(plan["dry_run"]["heuristic_peak_cost_yuan"]) > CAP_YUAN:
        raise ValueError("Dry-run projected cost exceeds the authorized cap")
    if (PUBLIC / "evaluation-attempt.json").exists():
        raise ValueError("This external study already has an attempt; replay evidence, never retry")
    key = read_key(key_file)
    attempt = {
        "study_id": STUDY_ID,
        "plan_sha256": plan["plan_sha256"],
        "document_sha256": plan["document_sha256"],
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "execution_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=PROJECT, text=True
        ).strip(),
        "policy": "One study attempt; no repeat, retry, new band or model selection",
    }
    reserve_attempt(workspace, attempt)
    selected = plan["local_model"]
    model = SafeModel(FORMAL / "runs" / selected["run_id"], "mlp16")
    write_json(workspace / "local-results.json", local_results(model, rows))
    ledger = Ledger(
        workspace / "ledger.json", CAP_YUAN, plan["plan_sha256"], authorized_max_cap_yuan=CAP_YUAN
    )
    interrupted = False
    try:
        asyncio.run(dispatch(rows, ledger, key))
    except (Exception, KeyboardInterrupt):
        interrupted = True
        ledger.recover_interrupted()
    finally:
        key = ""
    unchanged = fingerprints(allowed) == plan["protected_input_fingerprints"]
    if not unchanged:
        raise ValueError("Protected formal inputs changed during the one-shot evaluation")
    summary = publish(workspace, plan, ledger, {**access, "protected_files_unchanged": unchanged})
    if interrupted:
        raise RuntimeError(
            "External test interrupted; unknown reservations and partial results saved"
        )
    return summary


def main() -> None:
    """Prepare and commit a study plan, execute once, or replay ID-only cached evidence."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--prepare", action="store_true")
    mode.add_argument("--live", action="store_true")
    mode.add_argument("--replay", action="store_true")
    parser.add_argument("--verify-online", action="store_true")
    parser.add_argument("--key-file", type=Path)
    args = parser.parse_args()
    workspace = args.root.resolve()
    if workspace == FORMAL.resolve() or FORMAL.resolve().is_relative_to(workspace):
        parser.error("Use an independent workspace, never the formal data root")
    if workspace.is_relative_to(PROJECT) and not (args.replay and workspace == PUBLIC):
        relative = workspace.relative_to(FORMAL) if workspace.is_relative_to(FORMAL) else None
        if relative is None or not relative.parts[0].startswith("external-test"):
            parser.error(
                "Inside this repository use var/external-test* as an independent workspace"
            )
    workspace.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (workspace / ".external-study.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.prepare:
            result = prepare(workspace, args.verify_online)
            print(
                json.dumps(
                    {
                        "mode": "frozen_preparation",
                        "rows": result["external_count"],
                        "class_counts": result["class_counts"],
                        "plan_sha256": result["plan_sha256"],
                        "dry_run": result["dry_run"],
                    }
                )
            )
        elif args.live:
            summary = execute(workspace, args.key_file)
            print(
                json.dumps(
                    {
                        "status": "completed",
                        "rows": summary["requested_rows"],
                        "budget": summary["budget"],
                    }
                )
            )
        else:
            plan = json.loads((workspace / "plan.json").read_text())
            summary = publish(workspace, plan, None)
            print(
                json.dumps(
                    {
                        "mode": "cached_replay",
                        "network_requests": 0,
                        "rows": summary["requested_rows"],
                    }
                )
            )


if __name__ == "__main__":
    main()
