"""Report one preregistered external cohort without selecting a model or opening SMS data."""

from collections.abc import Mapping
from html import escape
import json
from pathlib import Path
from typing import Any

from textlab.comparison_report import (
    _budget_section,
    _latency,
    _metrics,
    _money,
    _number,
    _percent,
    _quality_table,
    _relative,
)

Record = Mapping[str, Any]
FROZEN_BAND = [0.2, 0.8]
FROZEN_PROMPT_SHA256 = "cc22556e0d9f8d46f80c4bc73232662ea7a77a04788067d5650231c362fe82fe"
CHART_NAME = "external-vs-validation.svg"
STRATEGY_NAMES = ("本地 MLP16", "纯 DeepSeek", "固定级联 [0.2, 0.8]")


def _fixed_strategies(summary: Record, *, external: bool) -> list[Record]:
    cascades = summary["cascades"]
    matching = [item for item in cascades if item.get("band") == FROZEN_BAND]
    if len(matching) != 1 or (external and len(cascades) != 1):
        raise ValueError("External reporting requires exactly the frozen [0.2, 0.8] cascade")
    if external and (
        summary.get("selection_performed") is not False
        or "recommendation" in summary
        or "best_observed_nonzero_band" in summary
        or summary.get("fixed_cascade_band") != FROZEN_BAND
    ):
        raise ValueError("External outcomes must not contain a new recommendation or band search")
    return [summary["local"], summary["deepseek"], matching[0]]


def _validate(summary: Record, plan: Record, validation_summary: Record) -> None:
    _fixed_strategies(summary, external=True)
    _fixed_strategies(validation_summary, external=False)
    if (
        plan["cascade_band"] != FROZEN_BAND
        or summary["requested_rows"] != plan["external_count"]
        or sum(plan["class_counts"].values()) != plan["external_count"]
        or plan["deepseek"]["prompt_sha256"] != FROZEN_PROMPT_SHA256
        or plan["local_model"]["kind"] != "mlp16"
        or plan["local_model"]["threshold"] != 0.5
        or summary["plan_sha256"] != plan["plan_sha256"]
        or summary["external_sha256"] != plan["external_sha256"]
    ):
        raise ValueError("External summary differs from the preregistered cohort or fixed protocol")


def _delta(current: Any, reference: Any, *, scale: float = 1.0, digits: int = 5) -> str:
    if current is None or reference is None:
        return "—"
    return f"{(float(current) - float(reference)) * scale:+.{digits}f}"


def _ham_count(strategy: Record) -> int:
    metric = _metrics(strategy)
    confusion = metric["confusion"]
    return (
        confusion["true_negative"]
        + confusion["false_positive"]
        + metric["abstention_count_by_truth"]["ham"]
    )


def _ham_false_positive_rate(strategy: Record) -> float | None:
    count = _ham_count(strategy)
    return _metrics(strategy)["false_positive_ham_count"] / count if count else None


def _delta_table(summary: Record, validation_summary: Record) -> str:
    external = _fixed_strategies(summary, external=True)
    validation = _fixed_strategies(validation_summary, external=False)
    lines = [
        "| 固定方案 | 验证 macro-F1 | 外部 macro-F1 | Δ macro-F1 | "
        "Δ spam 精确率 / 召回率 | 验证 ham 误杀 / ham 总数 | 外部 ham 误杀 / ham 总数 | "
        "Δ ham 误杀率，百分点 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for name, current, reference in zip(STRATEGY_NAMES, external, validation, strict=True):
        now = _metrics(current)
        old = _metrics(reference)
        fp_change = _delta(
            _ham_false_positive_rate(current), _ham_false_positive_rate(reference), scale=100
        )
        lines.append(
            f"| {name} | {_number(old['macro_f1'], 5)} | {_number(now['macro_f1'], 5)} | "
            f"{_delta(now['macro_f1'], old['macro_f1'])} | "
            f"{_delta(now['spam_precision'], old['spam_precision'])} / "
            f"{_delta(now['spam_recall'], old['spam_recall'])} | "
            f"{old['false_positive_ham_count']} / {_ham_count(reference)} | "
            f"{now['false_positive_ham_count']} / {_ham_count(current)} | "
            f"{fp_change} |"
        )
    return "\n".join(lines)


def _runtime_table(summary: Record) -> str:
    strategies = _fixed_strategies(summary, external=True)
    lines = [
        "| 固定方案 | 外部派发数 | 有效预测 p50 / p95，ms | 全方案已尝试 p50 / p95，ms | "
        "已知 API 元 / 千条请求 | 外部失败 / unknown / 未派发 | 未知费用预留，元 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for index, (name, strategy) in enumerate(zip(STRATEGY_NAMES, strategies, strict=True)):
        if index == 0:
            attempts = failures = unknown = not_attempted = 0
        elif index == 1:
            attempts = strategy["attempted_count"]
            failures = strategy["failure_count"]
            unknown = strategy["unknown_count"]
            not_attempted = strategy["not_attempted_count"]
        else:
            attempts = strategy["escalation_attempted_count"]
            failures = strategy["escalation_failure_count"]
            unknown = strategy["escalation_unknown_count"]
            not_attempted = strategy["escalation_not_attempted_count"]
        cost = strategy["cost"]
        lines.append(
            f"| {name} | {attempts} | {_latency(strategy)} | "
            f"{_latency(strategy, 'attempted')} | "
            f"{_money(cost['known_cost_yuan_per_1000_requested_rows'])} | "
            f"{failures} / {unknown} / {not_attempted} | "
            f"{_money(cost['unknown_reserved_yuan'])} |"
        )
    return "\n".join(lines)


def _findings(summary: Record, validation_summary: Record) -> str:
    current = _fixed_strategies(summary, external=True)
    previous = _fixed_strategies(validation_summary, external=False)
    values = [
        f"{name} macro-F1 {_number(_metrics(now)['macro_f1'], 5)}"
        f"（较验证集 {_delta(_metrics(now)['macro_f1'], _metrics(old)['macro_f1'])}）"
        for name, now, old in zip(STRATEGY_NAMES, current, previous, strict=True)
    ]
    cascade = current[2]
    return (
        "；".join(values)
        + "。固定级联计划升级 "
        + f"{cascade['planned_escalation_count']} / {summary['requested_rows']} 条"
        + f"（{_percent(cascade['planned_escalation_fraction'])}），"
        + f"回退本地 {cascade['fallback_count']} 条。"
        + "这些是原样冻结方案在本次外部批次的结果，无论提高或下降都不据此更换区间、"
        + "提示、阈值或权重，也不新增另一轮选择。"
    )


def _source_section(plan: Record, validation_summary: Record) -> str:
    source = plan["dataset"]
    audit = plan["data_audit"]
    ingestion = audit.get("ingestion", {})
    dedup = audit.get("dedup", {})
    reserved = audit.get("reserved_identity_exclusion", {})
    validation = validation_summary["local"]
    validation_spam = validation["requested_count"] - _ham_count(validation)
    external_spam = plan["class_counts"].get("spam", 0)
    lines = [
        f"来源：[{source['title']}]({source['source_url']})，版本 {source['version']}，"
        f"DOI `{source['doi']}`；贡献者 {', '.join(source['contributors'])}。"
        f"提供方标注 [{source['license']}]({source['license_url']})。"
        "本次处理包括二分类标签合并、归一化、输入过滤、去重和重叠隔离；"
        "保留署名与许可链接，不暗示提供方为本项目结果背书。",
        "",
        "| 预预测数据处理 | 条数 |",
        "| --- | ---: |",
        f"| 原始 CSV 行 | {ingestion.get('original_rows', '—')} |",
        f"| ASCII 字母规则排除 | {ingestion.get('ascii_letter_guard_rows_excluded', '—')} |",
        "| 内部归一化完全重复折叠 | "
        f"{ingestion.get('internal_exact_duplicate_rows_collapsed', '—')} |",
        f"| 标签冲突整组隔离 | {ingestion.get('conflicting_label_rows_quarantined', '—')} |",
        f"| 训练／验证规则重叠排除 | {dedup.get('uci_overlap_rows_excluded', '—')} |",
        f"| 历史测试 ID／模板元数据隔离 | {reserved.get('reserved_overlap_rows_excluded', '—')} |",
        f"| 固定外部样本总数 | {plan['external_count']} |",
        f"| ham / spam | {plan['class_counts'].get('ham', 0)} / {external_spam} |",
    ]
    lines.append(
        "\n原来源含有 UCI 成分，因此不能把整个原始来源称为独立新语料。"
        "本批次是在预测前按既定规则排除训练／验证重叠，并用旧测试 ID 与模板摘要隔离后剩余的"
        "固定样本；规则未检出重叠，不等于语义新颖性、全量近重复或完全无泄漏的证明。"
        "没有打开保存 757 条测试短信的封存文件；"
        "外部 CSV 中命中的 ID／模板行已在预测前隔离。"
        "因此不声称完成了对旧测试文本的字符近重复或语义审计。"
    )
    lines.append(
        f"\n外部 spam 占比 {_percent(external_spam / plan['external_count'])}；"
        f"原验证集为 {_percent(validation_spam / validation['requested_count'])}。"
        "样本量和类别比例不同，原始误杀数量不能单独用作增减结论；表中同时报告 ham 分母与误杀率。"
    )
    language = audit.get("language_audit", {})
    if isinstance(language, Mapping):
        lines.append(
            f"\n字符审计含非拉丁字母的样本数：{language.get('non_latin_letter_rows', '—')}。"
            "ASCII 字母规则和文字类别计数都不是语义语言识别，不保证剩余样本是纯英文；"
            "这限制了将其当作标准英文短信测试的解释。"
        )
    return "\n".join(lines)


def _fixed_route_diagnostic(artifact_dir: Path, summary: Record, validation_summary: Record) -> str:
    """Count errors on the one frozen route from published ID-only predictions, never SMS."""
    path = artifact_dir / "results.jsonl"
    if not path.is_file():
        return ""
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    if len(rows) != summary["requested_rows"] or len({row["sample_id"] for row in rows}) != len(
        rows
    ):
        raise ValueError("Published route diagnostics require the complete unique fixed cohort")
    low, high = FROZEN_BAND
    inside = inside_errors = outside_errors = fixed_inside = introduced_inside = 0
    outside_low_spam_errors = cascade_errors = local_errors = 0
    remote_errors = remote_unknown = remote_not_attempted = spam_count = local_spam_misses = 0
    for row in rows:
        truth = row["truth"]
        local = row["local"]
        remote = row["deepseek"]
        if truth not in {"ham", "spam"} or local["status"] != "ok":
            raise ValueError(
                "Published diagnostics require fixed labels and completed local outputs"
            )
        if local["label"] not in {"ham", "spam"} or not 0 <= local["spam_score"] <= 1:
            raise ValueError("Published diagnostics require validated local scores and labels")
        eligible = low <= local["spam_score"] <= high
        wrong = local["label"] != truth
        remote_ok = remote["status"] == "ok" and remote["label"] in {"ham", "spam"}
        remote_wrong = remote_ok and remote["label"] != truth
        remote_errors += int(remote_wrong)
        remote_unknown += int(remote["status"] == "unknown")
        remote_not_attempted += int(remote["status"] == "not_attempted")
        local_errors += int(wrong)
        spam_count += int(truth == "spam")
        local_spam_misses += int(truth == "spam" and wrong)
        if eligible:
            inside += 1
            inside_errors += int(wrong)
            fixed_inside += int(wrong and remote_ok and not remote_wrong)
            introduced_inside += int(not wrong and remote_wrong)
        else:
            outside_errors += int(wrong)
            outside_low_spam_errors += int(wrong and truth == "spam" and local["spam_score"] < low)
        prediction = remote["label"] if eligible and remote_ok else local["label"]
        cascade_errors += int(prediction != truth)
    old_local = _metrics(validation_summary["local"])
    old_spam_count = (
        old_local["confusion"]["true_positive"]
        + old_local["confusion"]["false_negative"]
        + old_local["abstention_count_by_truth"]["spam"]
    )
    net = local_errors - cascade_errors
    change = f"净减少 {net}" if net >= 0 else f"净增加 {-net}"
    outside = len(rows) - inside
    lines = [
        "### 冻结路径的实际错误分布（观察）",
        f"本地共 {local_errors} 条标签错误。冻结区间 [{low:g}, {high:g}] 内 "
        f"{inside} 条，其中本地错误 {inside_errors} 条；区间外 {outside} 条，"
        f"其中本地错误 {outside_errors} 条。区间内 DeepSeek 修正 {fixed_inside} 条既有错误，"
        f"同时引入 {introduced_inside} 条新错误，级联最终 {cascade_errors} 条错误，"
        f"相对本地{change} 条。",
        f"纯 DeepSeek 有效返回中观察到 {remote_errors} 条标签错误；"
        f"unknown {remote_unknown} 条、未派发 {remote_not_attempted} 条另列。",
    ]
    if outside_errors and outside_low_spam_errors == outside_errors:
        lines.append(
            f"区间外 {outside_errors} 条本地错误全部是真实 spam，且本地 spam_score < {low:g}。"
            "这些漏判没有触发冻结的升级条件，所以外部模型即便能纠正，也不会改变该级联的输出。"
        )
    else:
        lines.append(
            f"区间外本地错误中，{outside_low_spam_errors} 条是真实 spam "
            f"且本地 spam_score < {low:g}；它们没有触发冻结的升级条件。"
        )
    lines.append(
        f"本地 spam 漏判由验证集的 {old_local['confusion']['false_negative']} / {old_spam_count} "
        f"变为外部的 {local_spam_misses} / {spam_count}。"
        "这直接说明召回率和 F1 变化中的漏判计数组成，不证明变化的因果来源。"
        "这里的低分是未校准数值，不是可靠置信度或校准结论；"
        "仅观察冻结路由遗漏的错误类型，不据此扩大区间或重跑本批测试。"
    )
    return "\n\n".join(lines)


def _provenance(plan: Record, budget: Record, artifact_dir: Path) -> str:
    attempt_path = artifact_dir / "evaluation-attempt.json"
    attempt = json.loads(attempt_path.read_text()) if attempt_path.is_file() else {}
    evidence = {
        "study_id": plan.get("study_id"),
        "plan_sha256": plan["plan_sha256"],
        "document_sha256": plan.get("document_sha256"),
        "external_sha256": plan["external_sha256"],
        "frozen_config_sha256": plan["frozen_config_sha256"],
        "validation_summary_sha256": plan.get("validation_summary_sha256"),
        "source_archive_sha256": plan["dataset"]["archive_sha256"],
        "source_csv_sha256": plan["dataset"]["csv_sha256"],
        "execution_commit": attempt.get("execution_commit"),
        "started_at_utc": attempt.get("started_at_utc"),
        "local_model": plan["local_model"],
        "deepseek": plan["deepseek"],
        "cascade_band": plan["cascade_band"],
        "text_preprocessing": plan["text_preprocessing"],
        "execution": plan["execution"],
        "returned_models": budget.get("returned_models", []),
        "system_fingerprints": budget.get("system_fingerprints", []),
    }
    return (
        "计划在任何本地或付费预测前提交，样本 ID、来源摘要和配置预先固定。"
        "计划 JSON 的 plan_sha256 不含自身与文档摘要，Markdown 文档摘要另列，避免循环自哈希。"
        "逐次调用可由样本 ID、请求摘要、供应商响应 ID、用量、时刻和费用账本核对。"
        "发布文件仅含 ID、标签、数值与受限元数据，不发布短信原文、完整上游响应或凭据。\n\n"
        "```json\n" + json.dumps(evidence, ensure_ascii=False, indent=2) + "\n```"
    )


def _chart(summary: Record, validation_summary: Record) -> str:
    groups = list(
        zip(
            ("Local", "DeepSeek", "Cascade [0.2, 0.8]"),
            _fixed_strategies(validation_summary, external=False),
            _fixed_strategies(summary, external=True),
            strict=True,
        )
    )
    parts = [
        '<svg xmlns="http://www.w3.org/2000/svg" width="1040" height="500" '
        'viewBox="0 0 1040 500" role="img" aria-labelledby="title desc">',
        '<title id="title">Frozen strategies: validation vs external macro-F1</title>',
        '<desc id="desc">Three preregistered strategies only, with no new band search. '
        "Full-denominator conservative macro-F1. Y axis starts at zero.</desc>",
        '<rect width="1040" height="500" fill="white"/>',
        '<g font-family="Arial, sans-serif" fill="#243044">',
        '<text x="42" y="38" font-size="23" font-weight="700">'
        "Frozen strategies: validation vs external</text>",
        '<text x="42" y="64" font-size="14">'
        "Fixed local weights / prompt / threshold / band; "
        "remote model is an alias; cohorts differ</text>",
    ]
    for tick in range(6):
        value = tick / 5
        yy = 388 - value * 280
        parts.append(
            f'<path d="M 82 {yy:.2f} H 995" stroke="#dce3ed"/>'
            f'<text x="68" y="{yy + 5:.2f}" font-size="13" '
            f'text-anchor="end">{value:.1f}</text>'
        )
    for index, (name, old, now) in enumerate(groups):
        center = 230 + index * 300
        for offset, strategy, color in ((-65, old, "#176c66"), (5, now, "#2169b0")):
            score = _metrics(strategy)["macro_f1"]
            if score is None:
                continue
            height = float(score) * 280
            xx, yy = center + offset, 388 - height
            parts.append(
                f'<rect x="{xx}" y="{yy:.2f}" width="60" height="{height:.2f}" '
                f'fill="{color}" rx="3"/>'
                f'<text x="{xx + 30}" y="{yy - 9:.2f}" text-anchor="middle" '
                f'font-size="14">{_number(score, 5)}</text>'
            )
        parts.append(
            f'<text x="{center}" y="417" text-anchor="middle" font-size="15">{escape(name)}</text>'
        )
    parts.extend(
        [
            '<text x="24" y="246" transform="rotate(-90 24 246)" '
            'font-size="14" text-anchor="middle">Macro-F1 (all requested rows)</text>',
            '<rect x="680" y="437" width="14" height="14" fill="#176c66"/>'
            '<text x="701" y="449" font-size="13">Validation</text>',
            '<rect x="820" y="437" width="14" height="14" fill="#2169b0"/>'
            '<text x="841" y="449" font-size="13">External</text>',
            '<text x="42" y="467" font-size="12" fill="#606d80">'
            "Observed cohort differences only; "
            "not causal effects or a new selection exercise.</text>",
            "</g></svg>\n",
        ]
    )
    return "\n".join(parts)


def render_report(
    summary: Record,
    plan: Record,
    budget: Record,
    validation_summary: Record,
    *,
    docs_path: Path,
    artifact_dir: Path,
) -> None:
    """Render only the fixed external protocol and signed differences without reading raw data."""
    _validate(summary, plan, validation_summary)
    docs_path = Path(docs_path)
    artifact_dir = Path(artifact_dir)
    docs_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    chart = artifact_dir / CHART_NAME
    chart.write_text(_chart(summary, validation_summary), encoding="utf-8")
    plan_path = docs_path.parent.parent / plan["execution"]["freeze_plan_path"]
    links = " · ".join(
        f"[{name}]({_relative(artifact_dir / name, docs_path)})"
        for name in (
            "plan.json",
            "audit.json",
            "cohort.json",
            "results.jsonl",
            "ledger.json",
            "budget.json",
            "summary.json",
            "evaluation-attempt.json",
            "verification.json",
        )
    )
    integrity = budget.get("data_integrity") or {}
    guard = (
        "本轮访问审计记录：封存测试文件打开次数为 0；正式受保护输入完整性核对一致。"
        if integrity.get("heldout_test_files_opened") == 0
        and integrity.get("protected_files_unchanged") is True
        else "完整性与禁止访问检查以 verification.json 和账本中的 data_integrity 为准。"
    )
    content = [
        f"# 冻结方案外部测试：{plan['external_count']} 条固定短信样本",
        _findings(summary, validation_summary),
        "## 固定协议与解释范围",
        f"预注册计划：[EXTERNAL_TEST_PLAN.md]({_relative(plan_path, docs_path)})。"
        "本轮只报告原本地模型、原 DeepSeek 提示与固定 [0.2, 0.8] 级联，"
        "没有重新扫描区间、选阈值、选提示或训练。"
        "两种模型均使用 `textlab.data.normalize` 的 casefold、折叠空白、去首尾空白结果；"
        "外部 user 消息只有该条归一化短信，不带真实标签或样本 ID。"
        "本地权重固定；DeepSeek 使用原服务型号别名，保存返回型号和 fingerprint "
        "不等于冻结供应商权重。",
        "这些样本仅在列明去重规则和可核对参照下未检出本地训练／验证重叠，"
        "不能证明 DeepSeek 预训练未见过公开语料，也不能称为对大模型完全未见的新测试。"
        "结果仅属于此来源、此过滤批次；不代表线上流量、其他国家／时间或中文性能。",
        "## 来源、许可与预预测筛选",
        _source_section(plan, validation_summary),
        "## 三个固定方案的外部结果",
        "有效子集与全量保守指标分开。全量保留全部固定样本，将 unknown 或未派发作为真实类别"
        "的漏判，不虚构外部标签；固定级联的外部结果缺失时保留本地预测。"
        "正常误杀仅计真实 ham 被预测为 spam，失败与 unknown 可重叠，不能相加。",
        _quality_table(summary),
        "## 与原验证集的有符号变化",
        "Δ = 外部 − 原验证，所有差异按同一方案逐项计算；正负都如实报告。"
        "原验证集曾用于挑选本地模型，对本地模型略有利；外部类别比例、来源与样本量不同，"
        "这里不是同一人群的前后对照，也不进行显著性或因果推断。",
        _delta_table(summary, validation_summary),
        f"![冻结方案的验证与外部 macro-F1]({_relative(chart, docs_path)})",
        "## 时延、费用与失败",
        _runtime_table(summary),
        "本地耗时只含已加载数值模型的逐条计算；DeepSeek 含网络与远端处理。"
        "级联复用同一批外部输出与逐条耗时作反事实回放，不是新调用或实测串行流程，"
        "缺少外部请求时其回放耗时仍未知。"
        "级联时延分位数包括未升级的本地样本，外部派发数不是时延分母。"
        "级联表内失败／unknown 指计划升级子集的外部状态，最终标签可能已回退本地。"
        "千条费用均按全体固定请求数换算；本地外部 API 费用为 0，未计硬件或电力。",
        _budget_section(budget, plan),
        "## 错误分布与可能的域变化解释",
        _fixed_route_diagnostic(artifact_dir, summary, validation_summary),
        "### 可能原因（未验证假设）",
        "来源时期、地区、发送习惯和促销／诈骗写法变化，去重后类别比例变化，"
        "本地词表覆盖变化，以及截图 OCR 与来源标签噪声，都可能影响结果。"
        "这些仅是待验证假设；当前汇总没有单独检验各因素，不能从升降归因。"
        "该公开来源有 UCI 复用、筛选与标签映射限制，不是随机抽取的线上短信；"
        "未做独立人工复标、预训练语料溯源或真实用户验收。"
        "无论此处升降，冻结配置保持原样；进一步研究需要另立新数据和新计划，"
        "不得继续用本批外部结果调参后仍把它称为未见测试。",
        "## 冻结身份与调用证据",
        guard,
        _provenance(plan, budget, artifact_dir),
        links,
        "只允许离线重放已有逐 ID 结果，不重新联网评估同一计划：\n\n"
        "```sh\n"
        ".venv/bin/python -m textlab.external_test --root artifacts/external-test --replay\n"
        "```",
    ]
    docs_path.write_text("\n\n".join(content) + "\n", encoding="utf-8")
