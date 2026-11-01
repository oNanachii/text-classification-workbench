# Text Model Lab · 项目协作入口

2026-11-01：Python 源码整理到 `textlab/` 与 `legacy/`。先读 `PROJECT_STATUS.md`、`README.md`，只执行用户授权的增量任务。原前端设计已接回并部署，`docs/process/` 中的设计交接与启动提示保留历史依据，不覆盖当前任务授权。

涉及前端或 Jev 时，再读 `docs/process/CLAUDE_FRONTEND_HANDOFF.md`、`docs/JEV_API_CONTRACT.md` 与 `docs/JEV_EVALUATION_PLAN.md`。保持现有功能与证据边界。Claude Code 由用户本人操作。

## 2026-11-01 本机接口压测

已按仓库所有者要求完成本地回环压测。工具为 `textlab.load_test`，唯一发压目标是其自行启动、PID 与模型核对通过的 `127.0.0.1` 子进程。测试专用入口为 `textlab.load_test_server:create_app`，必须显式启用并提供独立临时公开数值模型包；不能用作线上入口。

只在子进程内将每 IP 的 30 次/60 秒上限放宽为 1e9，保留生产 2 个推断槽、公开只读、禁用外部模型及单 Uvicorn worker；生产源码、部署文件和线上配置不改。输入只有 `examples/load_test_sms.json` 中 6 条自编短信，不读取真实短信、验证/测试语料、数据库、快照或旧最终报告，也不发 Jev/DeepSeek 请求。仅允许只读取得选定数值模型的服务清单和四个模型/报告文件，用于临时包。

结果在 `docs/LOAD_TEST.md`、`artifacts/load-test/`，默认可离线重建：`python -m textlab.load_test --replay artifacts/load-test/results.json`。新的本地测量使用全新输出目录；禁止把工具指向任意远端服务或线上压力目标。该结果只属于本机本工作负载，不作为 VPS 或线上容量结论。

## 2026-11-01 冻结外部公开数据测试

本次仓库所有者另行授权使用外部公开短信、费用上限人民币 4 元，沿用既有提示、`mlp16 / 0.5` 与包含端点的 `[0.2, 0.8]`。已在独立 `var/external-test-20261101/` 完成唯一一轮；该授权不扩展至任何重跑或新批次。

`docs/EXTERNAL_TEST_PLAN.md` 与对应 plan/audit/cohort 已于任何预测前冻结在 `ba8e68f589b35eeda22f8977b635f934d3adfff3` 并先推送 GitHub，之后不得修改。原始 ZIP、CSV、正文及私有规范化副本不提交。来源与 CC BY 4.0 条件见 `data/external/README.md`。去重只读 UCI 训练／验证文本及额外 4 条自编训练样例；为隔离来源中的旧测试重复，仅使用 `var/split-index.json` 的 ID／模板摘要元数据，不打开封存测试原文或排除原文库，不重评旧 757 条。

结果见 `docs/EXTERNAL_TEST.md` 和 `artifacts/external-test/`，仅说明这份按规则过滤的外部批次；不声称 DeepSeek 预训练未见过公开语料，不推广到线上或中文。不得根据此结果调整冻结提示、区间、阈值或模型。默认仅可用 `python -m textlab.external_test --root artifacts/external-test --replay` 离线重放；全局尝试标记会拒绝该计划的第二次真实评估。

## 2026-11-01 验证集开发比较

本次仓库所有者另行授权真实 DeepSeek 对比：只用 v2 的 783 条验证集，以当前线上 `mlp16 / 0.5` 为本地对照；在独立 `var/llm-comparison-20261101/` 运行，整轮费用上限人民币 1 元，先 dry-run、逐次持久化预留、无自动重试。此授权覆盖该次已经完成的请求，不扩展到任何新批次或其他付费接口。不得读取 757 条测试或 heldout/exclusion 文件，正式数据、权重、阈值和历史报告保持不变。

本轮禁止运行历史 `scripts/verify_refactor.py`：它会遍历正式 `var/` 的二进制文件，不适用于本任务的留出集拒读规则。

结果见 `docs/LLM_VS_LOCAL.md` 与 `artifacts/llm-comparison/`。后续默认只执行 `python -m textlab.comparison --root artifacts/llm-comparison --replay`，从已保存的逐 ID 输出重新核算，不联网、不加载真实短信。提示词、参数和推荐区间以报告“冻结配置”为准，外部数据测试原样使用，不依据该测试结果继续调参；新的真实调用需新的明确授权。

## 目标与入口

- 真实任务仍是英文短信 `ham/spam` 判断与人工复核。按“判断体验、批量导入、人工审核、效果对照、模型与规则”整理信息；这些是功能职责，可以合理合并、拆分或安排导航。
- 判断体验保留 `local / jev / compare` 三模式。Jev 接入是单条外部判断，不是中文留言产品、批量外发、自动审核或训练系统。
- 真正入口：`frontend/src/main.js` → `frontend/src/App.vue`；当前判断组件为 `components/DecisionPanel.vue`，曲线使用 `components/LossChart.vue`。`PredictionPanel.vue`、`SamplesPanel.vue`、`ExperimentsPanel.vue` 和旧 `api.js` 不是当前主流程入口，不能只改这些旧面板后声称交付。
- 当前后端是 `textlab.app_v2:create_app` 与 `textlab/decision.py`，主页面路径 `/workbench/`，同源 API `/api/*`。旧 `legacy/app.py` 是 v1 兼容入口。
- 视觉、版式、颜色、组件和动效由你根据产品目标决定。没有确认的独立模板目录；如用户提供了模板，先查明再适配，否则直接设计，不虚构模板。

## 必须保留

1. 公开模式只读，即使携带操作者令牌也不能导入、审核、生成快照或切换模型；public 不借用服务器 Jev Key。
2. BYOK 仅页内内存，经同源 HTTPS 的 `X-TypeSafe-Key` 发送；本地模式不发送 Key。外发前明确同意；不自动重试；超时、取消与未收到结果都不证明没有计费。
3. 输入、引擎、Key、同意项变化后旧响应失效；清除、身份变化、离开面板后不恢复旧文本、凭据或结果。保留 `decisionSession.js`、`importGuard.js`、`samplePage.js` 的竞态保护。
4. 导入预览与确认分开；新样本先待审。审核提交带准确 `expected_revision`、标签和依据；409 不覆盖。保存未知时重新读取核对，同标签审核不伪造纠错。
5. 已选择候选与实际加载版本分开展示，切版/回退需要重启后核对。模型建议不能写成人工真值或直接进入快照。
6. 本地分数未经校准；Jev confidence 是分布集中程度，不是正确率。双方一致也不证明正确，失败/unknown 不能显示为有效对照。

## 证据冻结与任务边界

- **最终评估证据冻结在 tag `v2.0-final-eval`；之后的改动必须通过等价校验。** 757 条最终测试对应这个 tag 的代码（`81feb90c5f55cb5706826c73e6950952d83af45c`）。原五个依赖的当前路径为 `textlab/evaluation.py`、`textlab/safe_model.py`、`textlab/data.py`、`textlab/experiments.py`、`textlab/dataflow.py`。不得改历史权重、阈值、冻结计划、尝试记录、报告或旧实验。
- **禁止重新执行旧 757 条最终测试**，不得用它做推断、调参或再次评估；不运行正式工作空间的 `freeze-final`、`evaluate-final`。新 Jev 质量评估需要独立授权、数据和预算。
- 常规软件检查用临时目录、自编数据、假令牌与 `httpx.MockTransport`，不读真实 `.env`、工作数据库、原始短信或正式快照。2026-11-01 本次重构已由仓库所有者特别授权：只读访问正式数据，在 783 条验证集比较分数，并从原始数据在临时目录重建划分、比较样本 ID 归属；不得修改 `var/` 中任何文件或权限。脚本为 `scripts/verify_refactor.py`，结果为 `artifacts/refactor-equivalence.json`。
- 不发付费模型请求，不运行 `scripts/verify_jev.py --live`。本次任务已授权等价校验通过后部署，并用 3 条自编英文短信比较部署前后线上预测分数；这不授权新训练、改权重或改阈值。常规前端演示仍不使用会复制真实工作库的 `scripts/verify_v2_http.py`。
- 不弱化 CSP、鉴权、TLS、限流或数据边界；不添加外部脚本、广告 SDK、追踪或密钥持久化。若预留未来推广位置，只能默认隐藏且不接触工作数据。

## 交付检查

使用现有依赖，不为视觉工作随意重装环境：

```sh
ruff check .
ruff format --check .
.venv/bin/python -B scripts/verify_refactor.py
.venv/bin/python -m unittest discover -s tests -v
npm test --prefix frontend
npm run build --prefix frontend
.venv/bin/python scripts/verify_jev.py
```

最后一项默认 dry-run，仅准备自编请求；不能附加 `--live`。`unittest`、前端测试与 Jev dry-run 使用自编/临时 fixture；等价校验按本次授权只读正式验证集和划分来源。两类检查都不运行真实 757 条评估。

涉及前端交互或视觉变化时，必须实际打开隔离演示的生产构建，检查桌面和窄屏、键盘、焦点、空/加载/错误/保存未知/修订冲突/清除身份状态，并核对 CSP。保留截图与检查结果，注明全部为模拟数据；构建通过不等于视觉验收完成。具体状态清单、API和冻结文件摘要见交接文档。
