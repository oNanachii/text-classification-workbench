"""Render a validation-only comparison from audited summaries without model calls or SMS access."""

from collections.abc import Mapping, Sequence
from decimal import Decimal
from html import escape
import json
import math
import os
from pathlib import Path
from typing import Any

Record = Mapping[str, Any]
CHART_NAME = "escalation-frontier.svg"
CHART_WIDTH = 1280
CHART_HEIGHT = 680
PLOT_LEFT = 92
PLOT_TOP = 102
PLOT_WIDTH = 738
PLOT_HEIGHT = 464
LEGEND_LEFT = 878
TICK_COUNT = 5
MIN_Y_SPAN = 0.06
Y_PADDING = 0.01
LOCAL_COLOR = "#176c66"
DEEPSEEK_COLOR = "#8150ac"
CASCADE_COLOR = "#2169b0"
INCOMPLETE_COLOR = "#7c838d"
TEXT_COLOR = "#243044"
GRID_COLOR = "#dce3ed"


def _number(value: Any, digits: int = 4) -> str:
    if value is None:
        return "—"
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("Report metrics must be finite")
    return f"{number:.{digits}f}"


def _money(value: Any) -> str:
    if value is None:
        return "—"
    number = Decimal(str(value))
    if not number.is_finite() or number < 0:
        raise ValueError("Report money must be finite and nonnegative")
    return format(number, ".8f").rstrip("0").rstrip(".") or "0"


def _percent(value: Any) -> str:
    return "—" if value is None else f"{float(value) * 100:.2f}%"


def _band(value: Sequence[float] | None) -> str:
    if value is None:
        return "无升级区间"
    return f"[{value[0]:g}, {value[1]:g}]"


def _relative(path: Path, docs_path: Path) -> str:
    return os.path.relpath(path, docs_path.parent).replace(os.sep, "/")


def _metrics(strategy: Record) -> Record:
    return strategy["full_denominator_conservative_metrics"]


def _latency(strategy: Record, scope: str = "completed") -> str:
    values = strategy["latency_ms"][scope]
    return f"{_number(values['p50'], 3)} / {_number(values['p95'], 3)}"


def _strategy_rows(summary: Record) -> list[tuple[str, Record]]:
    rows = [("本地 MLP16", summary["local"]), ("纯 DeepSeek", summary["deepseek"])]
    rows.extend((f"级联 {_band(item['band'])}", item) for item in summary["cascades"])
    return rows


def _quality_table(summary: Record) -> str:
    lines = [
        "| 方案 | 有效结果 / 全量 | macro-F1 仅完成 | macro-F1 全量保守 | "
        "spam 精确率 / 召回率 | ham 精确率 / 召回率 | 正常短信误杀 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for name, strategy in _strategy_rows(summary):
        metric = _metrics(strategy)
        completed = strategy["completed_only_metrics"]
        lines.append(
            f"| {name} | {strategy['completed_count']} / {strategy['requested_count']} | "
            f"{_number(completed['macro_f1'])} | {_number(metric['macro_f1'])} | "
            f"{_number(metric['spam_precision'])} / {_number(metric['spam_recall'])} | "
            f"{_number(metric['ham_precision'])} / {_number(metric['ham_recall'])} | "
            f"{metric['false_positive_ham_count']} |"
        )
    return "\n".join(lines)


def _runtime_table(summary: Record) -> str:
    lines = [
        "| 方案 | 已派发 / 全量 | 完成 p50 / p95，ms | 已派发 p50 / p95，ms | "
        "已知 API 元 / 千条请求 | 失败数（占全量） | unknown 数（占全量） | 未派发数 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for name, strategy in _strategy_rows(summary)[:2]:
        cost = strategy["cost"]["known_cost_yuan_per_1000_requested_rows"]
        lines.append(
            f"| {name} | {strategy['attempted_count']} / {strategy['requested_count']} | "
            f"{_latency(strategy)} | {_latency(strategy, 'attempted')} | {_money(cost)} | "
            f"{strategy['failure_count']}（{_percent(strategy['failure_fraction_requested'])}）| "
            f"{strategy['unknown_count']}（{_percent(strategy['unknown_fraction_requested'])}）| "
            f"{strategy['not_attempted_count']} |"
        )
    return "\n".join(lines)


def _cascade_table(summary: Record) -> str:
    lines = [
        "| 区间（含端点） | 计划升级数（占全量） | 外部派发 / 完成 | 全量 macro-F1 | "
        "已知元 / 千条请求 | 未知费用预留，元 | 回退本地数 | 可参与推荐 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for strategy in summary["cascades"]:
        cost = strategy["cost"]
        eligible = "是" if strategy["eligible_for_recommendation"] else "否"
        lines.append(
            f"| {_band(strategy['band'])} | {strategy['planned_escalation_count']}"
            f"（{_percent(strategy['planned_escalation_fraction'])}）| "
            f"{strategy['escalation_attempted_count']} / "
            f"{strategy['escalation_completed_count']} | "
            f"{_number(_metrics(strategy)['macro_f1'])} | "
            f"{_money(cost['known_cost_yuan_per_1000_requested_rows'])} | "
            f"{_money(cost['unknown_reserved_yuan'])} | {strategy['fallback_count']} | {eligible} |"
        )
    return "\n".join(lines)


def _finding(summary: Record) -> str:
    recommendation = summary["recommendation"]
    if recommendation["band"] is None:
        finding = (
            "按预先固定的筛选规则，没有完整观察且已计价的级联方案严格提高本地 macro-F1，"
            "本轮建议继续使用本地模型，不启用自动升级。"
        )
    else:
        finding = (
            f"按预先固定的筛选规则，本轮开发比较推荐区间 {_band(recommendation['band'])}："
            "它在完整观察且已计价的候选中，满足 macro-F1 严格高于本地、已知 API 费用最低。"
        )
    observed = summary.get("best_observed_nonzero_band")
    if observed:
        finding += (
            f" 已观察到的最佳非零升级候选为 {_band(observed['band'])}，"
            f"macro-F1 {_number(observed['macro_f1'])}；"
            "该候选按 F1 优先排列，与按费用筛选的最终建议是不同口径。"
        )
    else:
        finding += " 尚无完整观察的非零升级区间可冻结为候选，不能用部分返回结果补足证据。"
    return finding + "这些是验证集上的观察差异，未检验统计显著性，也不表示独立测试或线上收益。"


def _budget_section(budget: Record, plan: Record) -> str:
    rows = [
        ("已确认 token 用量按实际时段费率估算", "known_cost_yuan"),
        ("同一已确认用量全部按峰时费率计价", "conservative_peak_yuan"),
        ("未知用量保留的预算预留", "unknown_reserved_yuan"),
        ("峰时已知费用 + 未知预留（派发预算占用）", "cap_exposure_yuan"),
        ("本轮硬派发预算上限", "cap_yuan"),
    ]
    lines = ["| 账本口径 | 人民币元 |", "| --- | ---: |"]
    lines.extend(f"| {label} | {_money(budget.get(key))} |" for label, key in rows)
    lines.append(
        f"\n已派发 {budget.get('attempt_count', 0)} 次；"
        f"有可确认用量 {budget.get('known_usage_count', 0)} 次，"
        f"用量未知 {budget.get('unknown_usage_count', 0)} 次。"
        "费用来自 API usage 与公开费率换算，不是独立账单；未知预留不是已发生费用，"
        "不能把未知请求记为免费。派发前先持久化预留，无结果也不自动重试。"
    )
    if budget.get("stopped"):
        lines.append(
            "\n预算账本已停止后续派发；停止标记为 "
            f"`{budget.get('stop_reason') or 'unspecified'}`。未派发样本仍保留在全量分母中。"
        )
    totals = budget.get("usage_totals")
    if isinstance(totals, Mapping):
        fields = (
            ("prompt_tokens", "输入"),
            ("completion_tokens", "输出"),
            ("total_tokens", "合计"),
            ("prompt_cache_hit_tokens", "缓存命中"),
            ("prompt_cache_miss_tokens", "缓存未命中"),
        )
        lines.append(
            "\n已确认用量 token："
            + "，".join(f"{label} {totals.get(key, '—')}" for key, label in fields)
            + "。这些合计不包含用量未知的请求。"
        )
    schedule = plan.get("price_schedule", {})
    if isinstance(schedule, Mapping):
        peak = schedule.get("peak", {})
        offpeak = schedule.get("offpeak", {})
        lines.extend(
            [
                "\n| 元 / 百万 token | 缓存命中输入 | 未命中输入 | 输出 |",
                "| --- | ---: | ---: | ---: |",
                f"| 峰时 | {_money(peak.get('cache_hit'))} | "
                f"{_money(peak.get('cache_miss'))} | {_money(peak.get('output'))} |",
                f"| 非峰时 | {_money(offpeak.get('cache_hit'))} | "
                f"{_money(offpeak.get('cache_miss'))} | {_money(offpeak.get('output'))} |",
            ]
        )
        source = schedule.get("source_url")
        if source:
            lines.append(
                f"\n费率来源：[DeepSeek 官方人民币费率]({source})，"
                f"核对日期 {schedule.get('verified_date', '见计划')}。"
                "按北京时间的请求派发时刻确定时段；工作日节假日按峰时保守处理。"
            )
    dry = plan.get("dry_run", {})
    if isinstance(dry, Mapping):
        peak_estimate = dry.get("heuristic_peak_cost_yuan")
        offpeak_estimate = dry.get("heuristic_offpeak_cost_yuan")
        lines.append(
            "\n派发前的 dry-run 启发式估算：峰时 "
            f"{_money(peak_estimate)} 元、非峰时 {_money(offpeak_estimate)} 元。"
            "该值用请求字节估算 token，并按最大输出计入余量，不是 tokenizer 结果或账单保证；"
            "实际派发仍由逐请求保守预留限制，不能用估算替代账本。"
        )
    return "\n".join(lines)


def _frozen_config(summary: Record, plan: Record) -> dict[str, Any]:
    deepseek = plan["deepseek"]
    recommendation = summary["recommendation"]
    return {
        "cohort": {
            "split": "validation-development-comparison",
            "count": plan["validation_count"],
            "dataset_id": plan["dataset_id"],
            "validation_sha256": plan["validation_sha256"],
        },
        "local": plan["local_model"],
        "deepseek": {
            "endpoint": deepseek["endpoint"],
            "requested_model_alias": deepseek["model"],
            "system_prompt": deepseek["system_prompt"],
            "prompt_sha256": deepseek["prompt_sha256"],
            "payload_parameters": deepseek["payload_parameters"],
        },
        "cascade": {
            "recommended_strategy_id": recommendation["strategy_id"],
            "recommended_band": recommendation["band"],
            "band_endpoints_included": True,
            "selection_score": "local uncalibrated spam_score",
            "selection_rule": "low <= spam_score <= high",
            "fallback": "Keep the local label when external output is missing or unknown",
            "auto_retry": False,
            "timeout_seconds": plan["timeout_seconds"],
            "best_observed_nonzero_band": summary.get("best_observed_nonzero_band"),
            "recommendation_criterion": recommendation["selection_rule"],
        },
        "execution": {
            "concurrency": plan["concurrency"],
            "jev_status": plan["jev_status"],
            "identity_sha256": plan["identity_sha256"],
            "source_sha256": plan["source_sha256"],
        },
    }


def _provenance(budget: Record, plan: Record) -> str:
    models = budget.get("returned_models", [])
    fingerprints = budget.get("system_fingerprints", [])
    actual = ", ".join(f"`{model}`" for model in models) or "没有可确认返回型号"
    fingerprint = ", ".join(f"`{item}`" for item in fingerprints) or "未返回可确认 fingerprint"
    lines = [
        f"实际返回型号：{actual}。服务端 fingerprint：{fingerprint}。",
        "`deepseek-flash` 是请求时使用的服务型号别名；保存别名、提示和返回型号，"
        "不等于冻结供应商权重。即使 temperature=0，未来重新联网请求也不保证逐条完全相同。",
    ]
    online = plan.get("online_model_check")
    if isinstance(online, Mapping):
        lines.append(
            "\n线上本地模型核对摘要（版本/权重等只读元数据，完整字段见 plan.json）：\n\n"
            "```json\n" + json.dumps(online, ensure_ascii=False, indent=2) + "\n```"
        )
    integrity = budget.get("data_integrity", {})
    if isinstance(integrity, Mapping):
        opened = integrity.get("heldout_test_files_opened")
        unchanged = integrity.get("protected_files_unchanged")
        if opened == 0:
            lines.append(
                "\n本轮正式留出测试文件读取次数为 0；757 条最终测试未读取、未推断、未评估。"
            )
        if unchanged is True:
            lines.append("正式受保护文件的完整性检查一致；没有修改原始数据、模型权重或历史报告。")
    return "\n".join(lines)


def _chart(summary: Record) -> str:
    entries = [
        ("L", "Local", 0.0, summary["local"], LOCAL_COLOR),
        ("D", "DeepSeek", 1.0, summary["deepseek"], DEEPSEEK_COLOR),
    ]
    entries.extend(
        (
            f"C{index}",
            _band(strategy["band"]),
            strategy["planned_escalation_fraction"] or 0.0,
            strategy,
            CASCADE_COLOR if strategy["eligible_for_recommendation"] else INCOMPLETE_COLOR,
        )
        for index, strategy in enumerate(summary["cascades"], 1)
    )
    values = [
        float(_metrics(strategy)["macro_f1"])
        for _, _, _, strategy, _ in entries
        if _metrics(strategy)["macro_f1"] is not None
    ]
    minimum = min(values, default=0.0)
    maximum = max(values, default=1.0)
    span = max(MIN_Y_SPAN, maximum - minimum + Y_PADDING * 2)
    lower = max(0.0, minimum - (span - maximum + minimum) / 2)
    upper = min(1.0, lower + span)
    lower = max(0.0, upper - span)

    def x(value: float) -> float:
        return PLOT_LEFT + value * PLOT_WIDTH

    def y(value: float) -> float:
        return PLOT_TOP + (upper - value) / (upper - lower) * PLOT_HEIGHT

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{CHART_WIDTH}" '
        f'height="{CHART_HEIGHT}" viewBox="0 0 {CHART_WIDTH} {CHART_HEIGHT}" '
        'role="img" aria-labelledby="title desc">',
        '<title id="title">Escalation fraction, macro-F1 and API cost</title>',
        '<desc id="desc">Cached validation replay, not new calls per band. '
        "All-row conservative macro-F1 includes abstentions. "
        "Costs are confirmed token-priced CNY per 1000 requested rows. "
        "Gray points are ineligible incomplete observations. Y axis may be truncated.</desc>",
        '<rect width="1280" height="680" rx="14" fill="#ffffff"/>',
        '<g font-family="Arial, sans-serif" fill="#243044">',
        '<text x="48" y="44" font-size="24" font-weight="700">'
        "Escalation fraction vs macro-F1</text>",
        '<text x="48" y="73" font-size="14">'
        "Fixed validation cohort | cached replay | full-denominator conservative F1</text>",
    ]
    for tick in range(TICK_COUNT + 1):
        fraction = tick / TICK_COUNT
        xx = x(fraction)
        value = lower + fraction * (upper - lower)
        yy = y(value)
        parts.extend(
            [
                f'<path d="M {xx:.2f} {PLOT_TOP} V {PLOT_TOP + PLOT_HEIGHT}" '
                f'stroke="{GRID_COLOR}"/>',
                f'<path d="M {PLOT_LEFT} {yy:.2f} H {PLOT_LEFT + PLOT_WIDTH}" '
                f'stroke="{GRID_COLOR}"/>',
                f'<text x="{xx:.2f}" y="{PLOT_TOP + PLOT_HEIGHT + 25}" '
                f'text-anchor="middle" font-size="13">{fraction * 100:.0f}%</text>',
                f'<text x="{PLOT_LEFT - 12}" y="{yy + 4:.2f}" '
                f'text-anchor="end" font-size="13">{value:.3f}</text>',
            ]
        )
    coordinates = [
        f"{x(float(fraction)):.2f},{y(float(_metrics(strategy)['macro_f1'])):.2f}"
        for _, _, fraction, strategy, _ in entries[2:]
        if _metrics(strategy)["macro_f1"] is not None
    ]
    if coordinates:
        parts.append(
            f'<polyline points="{" ".join(coordinates)}" fill="none" '
            'stroke="#2169b0" stroke-width="2" opacity="0.5"/>'
        )
    parts.extend(
        [
            f'<text x="{PLOT_LEFT + PLOT_WIDTH / 2:.2f}" y="620" '
            'text-anchor="middle" font-size="16">Planned external escalation fraction</text>',
            '<text x="25" y="330" transform="rotate(-90 25 330)" '
            'text-anchor="middle" font-size="16">Macro-F1 (all requested rows)</text>',
            f'<text x="{LEGEND_LEFT}" y="114" font-size="15" font-weight="700">'
            "Rule / known CNY per 1000</text>",
        ]
    )
    occupied_labels: list[tuple[float, float, float, float]] = []

    def place_label(xx: float, yy: float) -> tuple[float, float]:
        for offset_y in (-13, 22, -35, 44, -57, 66, -79, 88):
            for offset_x in (9, -30, 40, -61):
                left, baseline = xx + offset_x, yy + offset_y
                box = left, baseline - 14, left + 26, baseline + 4
                if (
                    box[0] < PLOT_LEFT + 3
                    or box[2] > PLOT_LEFT + PLOT_WIDTH + 14
                    or box[1] < PLOT_TOP + 3
                    or box[3] > PLOT_TOP + PLOT_HEIGHT - 3
                ):
                    continue
                overlaps = any(
                    box[0] < other[2]
                    and box[2] > other[0]
                    and box[1] < other[3]
                    and box[3] > other[1]
                    for other in occupied_labels
                )
                if not overlaps:
                    occupied_labels.append(box)
                    return left, baseline
        return xx + 9, yy - 13

    for index, (identifier, label, fraction, strategy, color) in enumerate(entries):
        cost = _money(strategy["cost"]["known_cost_yuan_per_1000_requested_rows"])
        legend_y = 150 + index * 48
        parts.extend(
            [
                f'<circle cx="{LEGEND_LEFT + 7}" cy="{legend_y - 5}" r="6" fill="{color}"/>',
                f'<text x="{LEGEND_LEFT + 22}" y="{legend_y}" font-size="14">'
                f"{escape(identifier + '  ' + label + '  /  ' + cost)}</text>",
                f'<text x="{LEGEND_LEFT + 22}" y="{legend_y + 18}" '
                'font-size="12" fill="#606d80">'
                f"{escape(_percent(fraction))} planned; F1 "
                f"{_number(_metrics(strategy)['macro_f1'])}</text>",
            ]
        )
        score = _metrics(strategy)["macro_f1"]
        if score is None:
            continue
        xx, yy = x(float(fraction)), y(float(score))
        label_x, label_y = place_label(xx, yy)
        parts.extend(
            [
                f'<path d="M {xx:.2f} {yy:.2f} L {label_x + 8:.2f} {label_y - 5:.2f}" '
                f'stroke="{color}" stroke-width="0.8" opacity="0.65"/>',
                f'<circle cx="{xx:.2f}" cy="{yy:.2f}" r="6" '
                f'fill="{color}" stroke="white" stroke-width="2"/>',
                f'<text x="{label_x:.2f}" y="{label_y:.2f}" font-size="13" '
                f'font-weight="700" fill="{color}">{identifier}</text>',
            ]
        )
    parts.extend(
        [
            '<text x="48" y="653" font-size="12" fill="#606d80">'
            "Y-axis bounds are explicit and may exclude zero. "
            "Gray = incomplete/ineligible. "
            "Costs exclude unknown charges and local hardware.</text>",
            "</g></svg>\n",
        ]
    )
    return "\n".join(parts)


def render_report(
    summary: Record,
    plan: Record,
    budget: Record,
    *,
    docs_path: Path,
    artifact_dir: Path,
) -> None:
    """Write a source-backed Markdown report and deterministic SVG without reading any SMS."""
    docs_path = Path(docs_path)
    artifact_dir = Path(artifact_dir)
    docs_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    chart_path = artifact_dir / CHART_NAME
    chart_path.write_text(_chart(summary), encoding="utf-8")
    count = summary["requested_rows"]
    local_threshold = summary["local"].get("threshold", 0.5)
    artifacts = " · ".join(
        f"[{name}]({_relative(artifact_dir / name, docs_path)})"
        for name in ("plan.json", "summary.json", "budget.json", "results.jsonl", "ledger.json")
    )
    frozen = json.dumps(_frozen_config(summary, plan), ensure_ascii=False, indent=2)
    content = [
        f"# 本地模型与 DeepSeek：{count} 条验证集开发比较",
        _finding(summary),
        "## 数据与比较口径",
        f"同一批 {count} 条验证样本、同一组样本 ID 与标签，比较固定阈值 "
        f"{local_threshold:g} 的本地 MLP16、纯 DeepSeek 与按本地分数升级的级联。"
        f"本地模型曾用这批 {count} 条验证集选择模型和检查点，具有开发集选择优势；"
        "级联区间也在本批数据上比较。这不是未见数据上的公平泛化排名，"
        "不以它改写既有最终测试或声称线上提分。757 条最终测试不得读取、推断或评估。",
        "“仅完成”指标只看有效返回；“全量保守”保持所有请求样本为分母，"
        "将 unknown 与未派发视作其真实类别的漏判，不虚构预测类别。"
        "未定义的精确率/召回率显示为 —。正常短信误杀只统计真实 ham 被预测为 spam。",
        _quality_table(summary),
        "## 时延、费用与失败",
        _runtime_table(summary),
        "本地时延是本机内存中的数值计算；DeepSeek 时延含网络与远端处理，"
        "两者测量边界不同。p50/p95 是逐条耗时的线性插值分位数，"
        f"并发 {plan['concurrency']} 不等于单请求时延除以该数，也不是容量测试。"
        "失败标记可能与 unknown 重叠，二者不能相加；未派发单列。"
        "千条费用始终除以全量请求数，不只除以成功数。"
        "本地外部 API 费为 0，未计硬件、电力或运维。",
        _budget_section(budget, plan),
        "## 级联区间回放",
        "区间使用未校准的本地 spam_score，包含两个端点。"
        "落入区间才升级；外部结果缺失、超时或未知时保留本地标签，不自动重试。"
        "下表复用同一份已缓存外部输出，不为每个区间重新付费调用。"
        "级联时延仅按逐条本地耗时加已观察外部耗时反事实回放；"
        "缺少外部请求的级联耗时仍未知，不能当作实测串行流程。",
        _cascade_table(summary),
        f"![升级比例、macro-F1 与已知费用]({_relative(chart_path, docs_path)})",
        "推荐先要求全部计划升级完成、完整预测、外部调用可计价，"
        "再要求全量 macro-F1 严格高于本地；在合格方案中依次按已知 API 费用最低、"
        "macro-F1 更高、区间更窄、下界更小排序。没有严格提升时继续本地。"
        "“最佳非零候选”另外按最高观察 F1 优先，仍需要独立评估；"
        "它不是已建议在生产启用的承诺。",
        "## 冻结配置与来源",
        _provenance(budget, plan),
        "下面冻结的是本轮数据身份、提示、服务型号别名、请求参数和升级规则。"
        "temperature=0、thinking=disabled、max_tokens=32、JSON 对象输出；"
        f"单次整段调用超时 {plan['timeout_seconds']} 秒，响应不超过 16 KiB。"
        "真实标签和样本 ID 不进入外部请求，user 消息只有该条短信。"
        f"Jev 状态：{plan['jev_status']}。",
        "```json\n" + frozen + "\n```",
        "## 复现与证据文件",
        artifacts,
        "离线重放只使用已保存的逐 ID 输出，不联网：\n\n"
        "```sh\n"
        ".venv/bin/python -m textlab.comparison --root artifacts/llm-comparison --replay\n"
        "```",
        "如获授权开展一次新的联网比较，使用新的独立目录和最多 1 元派发预算；"
        "凭据只从已有 `DEEPSEEK_API_KEY` 环境读取，不写进命令、报告或账本：\n\n"
        "```sh\n"
        ".venv/bin/python -m textlab.comparison --root var/llm-comparison-new "
        "--live --verify-online --cap-yuan 1\n"
        "```",
        "新的联网执行会再次使用服务当时的别名和权重，不能承诺精确复现缓存中的响应；"
        "未知请求保留费用预留，不会因重启或重放自动重试。",
    ]
    docs_path.write_text("\n\n".join(content) + "\n", encoding="utf-8")
