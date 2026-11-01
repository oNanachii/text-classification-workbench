"""Render a bounded local HTTP benchmark report without opening model or workload data."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from html import escape
import json
import math
import os
from pathlib import Path
from typing import Any

PROJECT = Path(__file__).resolve().parents[1]
CHART_NAME = "load-test-throughput.svg"
MIB = 1024**2
GIB = 1024**3
PERCENT = 100
CHART_WIDTH = 1080
CHART_HEIGHT = 510
PLOT_LEFT = 100
PLOT_RIGHT = 1010
PLOT_TOP = 100
PLOT_BOTTOM = 405
GRID_STEPS = 4
ALL_COLOR = "#8091aa"
SUCCESS_COLOR = "#21777a"
TEXT_COLOR = "#243044"
GRID_COLOR = "#e6ebf2"

Record = Mapping[str, Any]


def _number(value: Any, digits: int = 3) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return "—"
    return f"{value:.{digits}f}"


def _count(value: Any) -> str:
    if type(value) is int and value >= 0:
        return str(value)
    return "—"


def _percentage(value: Any) -> str:
    return "—" if _number(value) == "—" else f"{value * PERCENT:.2f}%"


def _text(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, (dict, list, tuple)):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value).replace("|", "\\|").replace("\n", " ")


def _mapping(value: Any) -> Record:
    return value if isinstance(value, Mapping) else {}


def _scenarios(result: Record) -> Sequence[Record]:
    value = result.get("scenarios", [])
    if not isinstance(value, (list, tuple)) or any(not isinstance(item, Mapping) for item in value):
        raise ValueError("Load-test scenarios must be a list of objects")
    return value


def _relative(target: Path, document: Path) -> str:
    return Path(os.path.relpath(target, document.parent)).as_posix()


def _status_counts(scenario: Record) -> Record:
    return _mapping(scenario.get("status_counts"))


def _error_counts(scenario: Record) -> Record:
    return _mapping(scenario.get("error_counts"))


def _status_count(scenario: Record, status: int) -> Any:
    if scenario.get("status_counts") is None:
        return None
    counts = _status_counts(scenario)
    return counts.get(str(status), counts.get(status, 0))


def _busy_count(scenario: Record) -> Any:
    counts = _error_counts(scenario)
    # The harness supplies typed categories; aliases support descriptive JSON field spellings.
    for name in ("capacity_busy", "prediction_capacity_busy", "429_capacity_busy", "busy_429"):
        if name in counts:
            return counts[name]
    return None


def _rate_count(scenario: Record) -> Any:
    counts = _error_counts(scenario)
    for name in (
        "minute_rate_limit",
        "rate_limited",
        "prediction_rate_limited",
        "429_rate_limited",
        "rate_429",
    ):
        if name in counts:
            return counts[name]
    return None


def _metrics(scenario: Record, phase: str) -> Record:
    return _mapping(scenario.get(f"server_metrics_{phase}"))


def _table(headers: Sequence[str], rows: Sequence[Sequence[str]], *, numeric: bool = False) -> str:
    lines = ["| " + " | ".join(headers) + " |"]
    lines.append("| " + " | ".join("---:" if numeric else "---" for _ in headers) + " |")
    lines.extend("| " + " | ".join(row) + " |" for row in rows)
    return "\n".join(lines)


def _numpy_backend(result: Record) -> Record:
    for scenario in _scenarios(result):
        for phase in ("before", "after"):
            backend = _mapping(_metrics(scenario, phase).get("numpy_backend"))
            if backend:
                return backend
    return {}


def _machine_table(result: Record) -> str:
    machine = _mapping(result.get("machine"))
    server = _mapping(result.get("server_config"))
    limits = _mapping(server.get("production_resource_limits"))
    workload = _mapping(result.get("workload"))
    memory = machine.get("memory_bytes")
    memory_gib = memory / GIB if type(memory) in (int, float) else None
    architecture = _text(machine.get("architecture"))
    hardware = _text(machine.get("hardware_model"))
    cores = _count(machine.get("physical_cores")), _count(machine.get("logical_cores"))
    runtime = [_text(server.get(name)) for name in ("host", "loop", "http")]
    lengths = [_count(workload.get(name)) for name in ("text_min_chars", "text_max_chars")]
    rate_window = _number(server.get("rate_window_seconds"), 0)
    soft = _number(limits.get("memory_high_mib"), 0)
    hard = _number(limits.get("memory_max_mib"), 0)
    tasks = _count(limits.get("tasks_max"))
    descriptors = _count(limits.get("limit_nofile", limits.get("file_descriptors")))
    backend = _numpy_backend(result)
    blas = _mapping(backend.get("blas"))
    lapack = _mapping(backend.get("lapack"))
    rows = [
        ("操作系统", _text(machine.get("os"))),
        ("架构 / 硬件型号", f"{architecture} / {hardware}"),
        ("CPU", _text(machine.get("cpu_model"))),
        ("物理核 / 逻辑核", " / ".join(cores)),
        ("机器内存", f"{_number(memory_gib, 2)} GiB"),
        ("Uvicorn workers", _count(server.get("workers"))),
        ("监听 / 事件循环 / HTTP", " / ".join(runtime)),
        ("数值库线程上限配置（非实测）", _count(server.get("numeric_threads"))),
        ("NumPy 构建 BLAS 身份", f"{_text(blas.get('name'))} / {_text(blas.get('version'))}"),
        ("NumPy 构建 LAPACK 身份", f"{_text(lapack.get('name'))} / {_text(lapack.get('version'))}"),
        ("推断槽位", _count(server.get("prediction_slots"))),
        ("公开只读 / 外部模型", "公开只读数值推断；本轮不调用 Jev 或 DeepSeek"),
        ("客户端模式", _text(workload.get("pattern"))),
        ("自编 fixture 条数", _count(workload.get("fixture_count"))),
        ("自编 fixture 字符长度", "–".join(lengths)),
        ("每轮预热请求数", _count(workload.get("warmup_requests"))),
        ("生产每 IP 窗口限制", f"{_count(server.get('production_rate_limit'))} / {rate_window} 秒"),
        ("隔离压测进程窗口限制", f"{_count(server.get('test_rate_limit'))} / {rate_window} 秒"),
        ("仓库 VPS 模板 CPU 配额", _text(limits.get("cpu_quota"))),
        ("仓库 VPS 模板 MemoryHigh / MemoryMax", f"{soft} / {hard} MiB"),
        ("仓库 VPS 模板任务数 / 文件描述符", f"{tasks} / {descriptors}"),
    ]
    content = _table(("配置", "本次记录 / 比较参照"), rows)
    versions = _mapping(result.get("versions"))
    if versions:
        pairs = [f"{_text(name)} `{_text(value)}`" for name, value in sorted(versions.items())]
        content += "\n\n依赖版本：" + "，".join(pairs) + "。"
    return content


def _throughput_table(result: Record) -> str:
    headers = (
        "客户端并发",
        "窗口完成尝试 QPS（含错误）",
        "窗口成功 QPS",
        "成功 p50 / p95 / p99，ms",
        "全请求错误率",
        "HTTP 429 / 其中 busy / 窗口限流",
        "应用生命周期峰值 RSS，MiB",
    )
    rows = []
    for scenario in _scenarios(result):
        latency = _mapping(_mapping(scenario.get("latency_ms")).get("successful"))
        memory = _mapping(scenario.get("memory"))
        quantiles = " / ".join(_number(latency.get(name)) for name in ("p50", "p95", "p99"))
        rejection = " / ".join(
            _count(value)
            for value in (
                _status_count(scenario, 429),
                _busy_count(scenario),
                _rate_count(scenario),
            )
        )
        rows.append(
            [
                _count(scenario.get("concurrency")),
                _number(scenario.get("qps"), 2),
                _number(scenario.get("successful_qps"), 2),
                quantiles,
                _percentage(scenario.get("error_rate")),
                rejection,
                _number(memory.get("server_peak_rss_mib"), 2),
            ]
        )
    return _table(headers, rows, numeric=True)


def _window_table(result: Record) -> str:
    headers = (
        "并发",
        "派发窗口，s",
        "总墙钟，s",
        "停止派发后的排空，s",
        "发起请求",
        "窗口内完成 / 成功",
        "含排空最终完成 / 成功 / 错误",
    )
    rows = []
    for scenario in _scenarios(result):

        def counts(names: Sequence[str]) -> str:
            return " / ".join(_count(scenario.get(name)) for name in names)

        rows.append(
            [
                _count(scenario.get("concurrency")),
                _number(scenario.get("duration_seconds")),
                _number(scenario.get("wall_seconds")),
                _number(scenario.get("drain_seconds")),
                _count(scenario.get("issued_requests")),
                counts(("completed_in_window", "successful_in_window")),
                counts(("completed_requests", "successful_requests", "error_requests")),
            ]
        )
    return _table(headers, rows, numeric=True)


def _cpu_memory_table(result: Record) -> str:
    headers = (
        "并发",
        "应用 CPU 秒 / 平均占用核",
        "压测器 CPU 秒 / 平均占用核",
        "应用预热峰值 RSS，MiB",
        "应用线程数：前 / 后",
    )
    rows = []
    for scenario in _scenarios(result):
        cpu = _mapping(scenario.get("cpu"))
        memory = _mapping(scenario.get("memory"))
        server_cpu = " / ".join(
            _number(cpu.get(name)) for name in ("server_cpu_seconds", "average_server_cores")
        )
        client_cpu = " / ".join(
            _number(cpu.get(name)) for name in ("client_cpu_seconds", "average_client_cores")
        )
        threads = " / ".join(
            _count(_metrics(scenario, phase).get("thread_count")) for phase in ("before", "after")
        )
        rows.append(
            [
                _count(scenario.get("concurrency")),
                server_cpu,
                client_cpu,
                _number(memory.get("server_warmup_peak_rss_mib"), 2),
                threads,
            ]
        )
    peak = _mapping(result.get("load_generator")).get("peak_rss_mib")
    sentence = f"压测器自身进程峰值 RSS：{_number(peak, 2)} MiB；与应用进程分开记录。"
    return _table(headers, rows, numeric=True) + "\n\n" + sentence


def _errors(result: Record) -> str:
    lines = []
    for scenario in _scenarios(result):
        statuses = (
            ", ".join(
                f"{_text(key)}={_count(value)}"
                for key, value in sorted(
                    _status_counts(scenario).items(), key=lambda item: str(item[0])
                )
            )
            or "—"
        )
        errors = ", ".join(
            f"{_text(key)}={_count(value)}"
            for key, value in sorted(_error_counts(scenario).items())
        ) or ("错误分类未记录" if scenario.get("error_counts") is None else "无分类错误")
        lines.append(
            f"- 并发 {_count(scenario.get('concurrency'))}："
            f"HTTP 状态 {statuses}；错误分类 {errors}。"
        )
    return "\n".join(lines)


def _observed_findings(result: Record) -> str:
    scenarios = {item.get("concurrency"): item for item in _scenarios(result)}
    middle, high = scenarios.get(10), scenarios.get(50)
    if middle is None or high is None:
        return ""
    q10, q50 = middle.get("successful_qps"), high.get("successful_qps")
    if any(type(value) not in (int, float) or not math.isfinite(value) for value in (q10, q50)):
        return ""
    lines = []
    q1 = _mapping(scenarios.get(1)).get("successful_qps")
    if type(q1) in (int, float) and math.isfinite(q1) and q1 < q10 and q50 < q10:
        lines.append(
            "成功吞吐呈非单调变化：并发 1、10、50 的成功 QPS 分别为 "
            f"{_number(q1, 2)}、{_number(q10, 2)}、{_number(q50, 2)}。"
            "这只是本轮三档观测，不能将最高一档数值视为已定位的容量上限。"
        )
    if q10 > 0 and q50 < q10:
        drop = (q10 - q50) / q10
        p95_10 = _mapping(_mapping(middle.get("latency_ms")).get("successful")).get("p95")
        p95_50 = _mapping(_mapping(high.get("latency_ms")).get("successful")).get("p95")
        lines.append(
            f"并发 50 的成功 QPS 比并发 10 低 {drop * PERCENT:.2f}%；"
            f"成功 p95 从 {_number(p95_10)} ms 变为 {_number(p95_50)} ms。"
            "低错误率不能替代等待时间指标。"
        )
        issued, errors = high.get("issued_requests"), high.get("error_requests")
        if type(issued) is int and issued > 0 and type(errors) is int and errors / issued < drop:
            lines.append(
                f"并发 50 共 {_count(issued)} 次请求，仅 {_count(errors)} 次错误，"
                f"错误率 {errors / issued * PERCENT:.5f}%；"
                f"busy 拒绝 {_count(_busy_count(high))} 次。"
                f"该档全部完成尝试 QPS {_number(high.get('qps'), 2)}，"
                f"与成功 QPS {_number(q50, 2)} 接近。"
                "这些拒绝计数不足以把档间的大幅吞吐回落直接解释为观察到的 429；"
                "仍需区分客户端、服务处理及其间等待。"
            )
    cpu10, cpu50 = _mapping(middle.get("cpu")), _mapping(high.get("cpu"))
    values = [
        cpu10.get("average_server_cores"),
        cpu50.get("average_server_cores"),
        cpu10.get("average_client_cores"),
        cpu50.get("average_client_cores"),
    ]
    if all(type(value) in (int, float) and math.isfinite(value) for value in values):
        lines.append(
            "从并发 10 到 50，应用平均占用核为 "
            f"{_number(values[0])} → {_number(values[1])}，压测器为 "
            f"{_number(values[2])} → {_number(values[3])}。"
            "这是两个进程的平均 CPU 时间，既不是整机利用率，也不是瞬时热点或等待剖析。"
            "本记录没有证明 CPU 饱和；同机其他进程和短时资源争用也未受控。"
        )
    threads10 = _metrics(middle, "after").get("thread_count")
    threads50 = _metrics(high, "after").get("thread_count")
    if type(threads10) is int and type(threads50) is int:
        lines.append(
            f"两个场景结束后的应用线程数快照为 {_count(threads10)} 和 {_count(threads50)}。"
            "它们不是活动线程峰值、线程池上限或排队长度，不能据此断言线程池已经饱和。"
        )
    lines.append(
        "本轮确认了端到端吞吐与尾延迟的变化，主瓶颈仍未定位。客户端调度、服务事件循环、"
        "连接或线程等待、HTTP 处理是待区分的环节；没有分阶段等待与活动时间，"
        "不能将下降归因于其中一个环节，也不据此更改生产槽位。"
    )
    return "\n\n".join(lines)


def _bottlenecks(result: Record) -> str:
    scenarios = _scenarios(result)
    busy = sum(value for scenario in scenarios if type(value := _busy_count(scenario)) is int)
    limited = sum(value for scenario in scenarios if type(value := _rate_count(scenario)) is int)
    statuses = sum(
        value for scenario in scenarios if type(value := _status_count(scenario, 429)) is int
    )
    lines = []
    if any(scenario.get("status_counts") is None for scenario in scenarios):
        lines.append("部分场景缺少 HTTP 状态计数，不能把缺测当成零拒绝或零错误。")
    if busy:
        lines.append(
            f"记录到 {busy} 次明确的 busy 拒绝。保留的两槽推断闸门确实拒绝了这些并发尝试；"
            "这证明该闸门生效，不能单凭 429 推断 CPU、数值模型或公网吞吐已经饱和。"
        )
    elif statuses:
        lines.append(
            f"记录到 {statuses} 次 HTTP 429，但现有分类未明确给出 busy 数；"
            "应按错误详情区分推断槽位与窗口限流，不能直接解释为 CPU 饱和。"
        )
    else:
        lines.append(
            "本轮没有记录到 HTTP 429；这只覆盖这些请求和时段，不能证明更高负载下不会触发准入限制。"
        )
    if limited:
        lines.append(
            f"另有 {limited} 次窗口限流拒绝。放宽每 IP 配额并不等于移除应用限流机制，"
            "应把这部分与数值推断吞吐分开。"
        )
    lines.append(
        "应用 CPU 秒与压测器 CPU 秒在上表分开。平均占用核表示进程 CPU 时间与对应墙钟的比值，"
        "1.0 约等于占用一个逻辑核，不是整台机器 100%。压测器与应用同机竞争调度资源；"
        "单 worker 的 HTTP 处理、线程调度、校验和序列化也消耗 CPU。没有独立进程剖析或分阶段计时，"
        "不能把观测曲线因果归为某一个阶段，也没有证明服务容量上限。"
    )
    if any(_metrics(scenario, "before").get("threadpool_info") == [] for scenario in scenarios):
        lines.append(
            "本轮 threadpool_info 记录为空列表，未提供可识别的数值库线程池信息。"
            "numeric_threads=1 是子进程启动时的环境线程上限配置；"
            "不能据此声称实际 BLAS 已验证为单线程。NumPy 后端摘要与环境配置保留在原始记录中。"
        )
    backend = _numpy_backend(result)
    if backend:
        blas = _mapping(backend.get("blas"))
        lapack = _mapping(backend.get("lapack"))
        lines.append(
            f"NumPy 构建身份摘要：BLAS `{_text(blas.get('name'))}`，"
            f"LAPACK `{_text(lapack.get('name'))}`；"
            f"runtime_threads_verified=`{_text(backend.get('runtime_threads_verified'))}`。"
            "这些名称只标识构建配置，不证明运行时线程数、具体调用路径或核心占用。"
            "构建版本 unknown 也不能自动补成系统或供应商版本。"
        )
    inference = _mapping(result.get("inference_only_ms"))
    if inference:
        lines.append(
            "另记数值推断计时："
            f"p50 / p95 / p99 = {_number(inference.get('p50'))} / "
            f"{_number(inference.get('p95'))} / "
            f"{_number(inference.get('p99'))} ms。它与完整 HTTP 请求的测量边界不同，"
            "不能把二者的差额精确归给网络、排队或某个 Python 函数。"
        )
    lines.append(
        "原每 IP 滑动窗口仍在保存时间戳，而且在两槽闸门判断之前追加。放宽配额的本机测试可能"
        "产生比生产默认 30 次/分钟更大的队列；RSS 包含模型、HTTP 服务和这些请求簿记。"
        "本轮没有独立采集队列长度，因此这只是源码提示的可能因素；"
        "尚不能把 RSS 增长判为模型泄漏或唯一瓶颈。"
    )
    return "\n\n".join(lines)


def _chart(result: Record) -> str:
    scenarios = _scenarios(result)
    maximum = max(
        (
            float(value)
            for scenario in scenarios
            for key in ("qps", "successful_qps")
            if type(value := scenario.get(key)) in (int, float)
            and math.isfinite(value)
            and value >= 0
        ),
        default=0.0,
    )
    top = maximum * 1.15 if maximum else 1.0
    width = PLOT_RIGHT - PLOT_LEFT
    height = PLOT_BOTTOM - PLOT_TOP
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{CHART_WIDTH}" height="{CHART_HEIGHT}" '
        f'viewBox="0 0 {CHART_WIDTH} {CHART_HEIGHT}" role="img" aria-labelledby="title desc">',
        '<title id="title">Local HTTP load test: all attempts and valid successes</title>',
        '<desc id="desc">Window completion rates including errors are separate from valid-success '
        "rates. Each bar is a local loopback scenario, not deployed VPS capacity. "
        "Axis starts at zero.</desc>",
        '<rect width="1080" height="510" fill="white"/>',
        f'<g font-family="Arial, sans-serif" fill="{TEXT_COLOR}">',
        '<text x="44" y="38" font-size="23" font-weight="700">Local HTTP benchmark</text>',
        '<text x="44" y="66" font-size="14">Single worker | same-host load generator | '
        "local loopback</text>",
    ]
    for tick in range(GRID_STEPS + 1):
        value = top * tick / GRID_STEPS
        y = PLOT_BOTTOM - height * tick / GRID_STEPS
        parts.extend(
            [
                f'<path d="M {PLOT_LEFT} {y:.2f} H {PLOT_RIGHT}" stroke="{GRID_COLOR}"/>',
                f'<text x="{PLOT_LEFT - 12}" y="{y + 4:.2f}" text-anchor="end" '
                f'font-size="12">{value:.0f}</text>',
            ]
        )
    count = max(len(scenarios), 1)
    group = width / count
    bar_width = min(75, group / 4)
    for index, scenario in enumerate(scenarios):
        center = PLOT_LEFT + group * (index + 0.5)
        for key, offset, color in (
            ("qps", -bar_width, ALL_COLOR),
            ("successful_qps", 5, SUCCESS_COLOR),
        ):
            value = scenario.get(key)
            x = center + offset
            if type(value) in (int, float) and math.isfinite(value) and value >= 0:
                bar_height = float(value) / top * height
                y = PLOT_BOTTOM - bar_height
                parts.extend(
                    [
                        f'<rect x="{x:.2f}" y="{y:.2f}" width="{bar_width - 5:.2f}" '
                        f'height="{bar_height:.2f}" fill="{color}"/>',
                        f'<text x="{x + (bar_width - 5) / 2:.2f}" y="{y - 7:.2f}" '
                        f'text-anchor="middle" font-size="13">{float(value):.2f}</text>',
                    ]
                )
            else:
                parts.append(f'<text x="{x:.2f}" y="{PLOT_BOTTOM - 10}" font-size="13">N/A</text>')
        parts.append(
            f'<text x="{center:.2f}" y="{PLOT_BOTTOM + 25}" text-anchor="middle" '
            f'font-size="14">Concurrency {escape(_count(scenario.get("concurrency")))}</text>'
        )
    parts.extend(
        [
            '<text x="26" y="255" transform="rotate(-90 26 255)" '
            'text-anchor="middle" font-size="14">Completed attempts / second</text>',
            f'<rect x="100" y="459" width="16" height="16" fill="{ALL_COLOR}"/>',
            '<text x="125" y="472" font-size="13">All completed attempts, including errors</text>',
            f'<rect x="485" y="459" width="16" height="16" fill="{SUCCESS_COLOR}"/>',
            '<text x="510" y="472" font-size="13">Valid successful responses</text>',
            "</g></svg>\n",
        ]
    )
    return "\n".join(parts)


def render_report(result: dict[str, Any], *, docs_path: Path, artifact_dir: Path) -> None:
    """Write one local benchmark Markdown report and SVG with explicit measurement boundaries."""
    scenarios = _scenarios(result)
    docs_path = Path(docs_path)
    artifact_dir = Path(artifact_dir)
    docs_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    chart = artifact_dir / CHART_NAME
    chart.write_text(_chart(result), encoding="utf-8")
    model = _mapping(result.get("model"))
    workload = _mapping(result.get("workload"))
    integrity = _mapping(result.get("integrity"))
    result_file = artifact_dir / "results.json"
    record = _relative(result_file, docs_path)
    try:
        replay_file = result_file.resolve().relative_to(PROJECT).as_posix()
    except ValueError:
        replay_file = result_file.as_posix()
    fixture = _text(workload.get("fixture_file"))
    scope = "、".join(_count(scenario.get("concurrency")) for scenario in scenarios) or "—"
    preflight_counts = {
        scenario["fixture_preflight_requests"]
        for scenario in scenarios
        if type(scenario.get("fixture_preflight_requests")) is int
    }
    preflight_note = ""
    if len(preflight_counts) == 1:
        count = next(iter(preflight_counts))
        preflight_note = (
            f"每轮另有 {count} 次自编 fixture 响应一致性预检，也不进入测量批次。"
            "CPU 差值的基线在预检和预热之后取得，生命周期 RSS 仍包括这些阶段。"
        )
    sections = [
        "# 本机 HTTP 负载测试",
        f"本轮在同一台机器的回环地址上，以单 Uvicorn worker、客户端并发 {scope} 测量公开只读的"
        "本地数值预测接口。它是本机软件与当前工作负载的观察记录，不是线上或 VPS 容量结论。"
        "压测进程保留生产相同的两槽推断闸门，仅在隔离子进程中放宽每 IP 窗口限流；"
        "生产服务、权重、阈值和线上限流配置不因此改变。",
        f"记录时间：`{_text(result.get('recorded_at_utc'))}`；源码提交：`{_text(result.get('source_commit'))}`。"
        f"模型：`{_text(model.get('run_id'))} / {_text(model.get('kind'))}`，"
        f"阈值 `{_text(model.get('threshold'))}`。"
        f"[机器可读原始汇总]({record})。",
        "## 机器、配置与 VPS 参照",
        _machine_table(result),
        "VPS 一栏是仓库部署模板参照；本机没有等价复刻其 CPU 配额与内存限制。"
        "CPUQuota=50% 表示约半个 CPU 核的调度配额，不是本机一半的总核数。"
        "本轮没有远端并发请求，也没有覆盖 HTTPS 反向代理、跨网时延、其他服务争用、"
        "长期稳定性或生产默认每 IP 30 次/分钟限制下的真实访问分布。",
        "## 请求与计时口径",
        f"输入来自 `{fixture}` 的 {_count(workload.get('fixture_count'))} 条自编短信，"
        "使用 keep-alive、闭环并发和轮转输入；并发数表示最多同时等待的请求，不是固定到达率。"
        f"每轮先完成 {_count(workload.get('warmup_requests'))} 次预热，"
        "预热请求不进入吞吐、时延和错误率。" + preflight_note,
        "每轮按记录的 duration_seconds 设定派发截止，窗口结束后停止新请求并等待已发出的请求结束。"
        "窗口 QPS 分别是 completed_in_window / duration_seconds 和 "
        "successful_in_window / duration_seconds；不把排空期间才完成的请求加进窗口吞吐。"
        "成功 p50/p95/p99 和错误率则覆盖窗口内发起的完整请求批次，包括排空阶段的完成或失败。"
        "错误率 = error_requests / issued_requests；HTTP 429、其他非成功状态、超时、连接错误和"
        "响应校验失败均应计入，不能只看快速拒绝得到的高 QPS。未知统计以 — 标记。",
        _window_table(result),
        "## 吞吐、成功时延与错误",
        _throughput_table(result),
        f"![本机窗口完成尝试与有效成功吞吐]({_relative(chart, docs_path)})",
        _errors(result),
        "成功时延仅使用有效成功响应；所有请求时延（含错误）另保存在原始汇总中，"
        "避免把快速 429 混入成功延迟后误报性能改善。"
        "一次、每级并发约 60 秒的观测不构成容量上限或 SLA。",
        "## CPU、内存和条件性瓶颈分析",
        _cpu_memory_table(result),
        "应用峰值 RSS 来自新启动后端进程的操作系统生命周期高水位，包含启动、预热、测量和排空；"
        "不是定时采样得到的近似峰值，也不是仅测量窗口内的峰值。预热峰值单列，"
        "它已经包含模型加载和预热分配，不是空进程基线。压测器的进程峰值另列，不能直接"
        "相加当作精确物理内存总量，因为共享页可能重复计入。RSS 不等于模型权重大小或可用容量。",
        _bottlenecks(result),
        "## 复现与证据边界",
        "复现使用独立公开数值模型包；包内只有服务清单、数值模型、词表、已有来源报告和"
        "压测用途标记，不包含工作数据库、快照或留出短信。自编 fixture 的摘要为 "
        f"`{_text(workload.get('fixture_sha256'))}`。"
        "本轮无付费模型调用，也不使用 757 条最终测试集。",
        "```sh\n"
        ".venv/bin/python -B -m textlab.load_test --help\n"
        "# 新的一轮本机测量，目标目录必须是新的独立目录。\n"
        ".venv/bin/python -B -m textlab.load_test --output artifacts/load-test-new "
        "--duration 60 --warmup 100\n"
        "# 仅按已有记录重建报告，不发压测请求。\n"
        f".venv/bin/python -B -m textlab.load_test --replay {replay_file}\n"
        "```",
        "隔离服务入口为 `textlab.load_test_server:create_app`，需要 "
        "`SMS_LAB_LOAD_TEST=true`、`SMS_LAB_LOAD_TEST_HOST=127.0.0.1` 和显式独立包路径。"
        "这些标记只用于独立回环子进程；不把测试入口作为公开部署入口，也不修改生产源码常量。",
        f"完整性记录：来源模型文件未变 `{_text(integrity.get('source_model_files_unchanged'))}`；"
        f"线上压测请求数 `{_count(integrity.get('online_load_requests'))}`。"
        "若附带 production_reference，它仅是只读元数据快照，不是对线上吞吐或资源容量的复核。",
    ]
    findings = _observed_findings(result)
    if findings:
        sections[3:3] = ["## 本轮观测到的变化", findings]
    traces = []
    for scenario in scenarios:
        name = scenario.get("trace_file")
        if not name:
            continue
        relative = _relative(artifact_dir / str(name), docs_path)
        traces.append(
            f"- 并发 {_count(scenario.get('concurrency'))}："
            f"[{_text(name)}]({relative})，SHA256 `{_text(scenario.get('trace_sha256'))}`。"
        )
    if traces:
        sections.extend(["逐请求数值记录（不含短信内容）：", "\n".join(traces)])
    sections.extend(
        [
            "独立复算可读取压缩轨迹、检查计数与分位数，不启动服务或发送请求：",
            "```sh\n.venv/bin/python -B scripts/verify_load_test.py\n```",
        ]
    )
    receipt = artifact_dir / "verification.json"
    if receipt.is_file():
        sections.append(f"[独立复算记录]({_relative(receipt, docs_path)})。")
    docs_path.write_text("\n\n".join(sections) + "\n", encoding="utf-8")
