# Text Model Lab · Claude 前端入口

2026-10-30：本轮五分区设计已接回、修复并部署。后续先读PROJECT_STATUS.md，只执行用户新要求的增量任务；原设计交接与启动提示是历史任务依据。

请先读 `docs/process/CLAUDE_FRONTEND_HANDOFF.md`、`docs/JEV_API_CONTRACT.md`、`docs/JEV_EVALUATION_PLAN.md` 和 `PROJECT_STATUS.md`。这轮工作是把已经可运行的 Vue 工作台设计成清楚、易演示的产品；保持现有功能与证据边界。Claude Code 由用户本人操作。

## 目标与入口

- 真实任务仍是英文短信 `ham/spam` 判断与人工复核。按“判断体验、批量导入、人工审核、效果对照、模型与规则”整理信息；这些是功能职责，可以合理合并、拆分或安排导航。
- 判断体验保留 `local / jev / compare` 三模式。Jev 接入是单条外部判断，不是中文留言产品、批量外发、自动审核或训练系统。
- 真正入口：`frontend/src/main.js` → `frontend/src/App.vue`；当前判断组件为 `components/DecisionPanel.vue`，曲线使用 `components/LossChart.vue`。`PredictionPanel.vue`、`SamplesPanel.vue`、`ExperimentsPanel.vue` 和旧 `api.js` 不是当前主流程入口，不能只改这些旧面板后声称交付。
- 当前后端是 `app_v2:create_app` 与 `decision.py`，主页面路径 `/workbench/`，同源 API `/api/*`。旧 `app.py` 是 v1 兼容入口。
- 视觉、版式、颜色、组件和动效由你根据产品目标决定。没有确认的独立模板目录；如用户提供了模板，先查明再适配，否则直接设计，不虚构模板。

## 必须保留

1. 公开模式只读，即使携带操作者令牌也不能导入、审核、生成快照或切换模型；public 不借用服务器 Jev Key。
2. BYOK 仅页内内存，经同源 HTTPS 的 `X-TypeSafe-Key` 发送；本地模式不发送 Key。外发前明确同意；不自动重试；超时、取消与未收到结果都不证明没有计费。
3. 输入、引擎、Key、同意项变化后旧响应失效；清除、身份变化、离开面板后不恢复旧文本、凭据或结果。保留 `decisionSession.js`、`importGuard.js`、`samplePage.js` 的竞态保护。
4. 导入预览与确认分开；新样本先待审。审核提交带准确 `expected_revision`、标签和依据；409 不覆盖。保存未知时重新读取核对，同标签审核不伪造纠错。
5. 已选择候选与实际加载版本分开展示，切版/回退需要重启后核对。模型建议不能写成人工真值或直接进入快照。
6. 本地分数未经校准；Jev confidence 是分布集中程度，不是正确率。双方一致也不证明正确，失败/unknown 不能显示为有效对照。

## 本轮边界

- **不可改动五个冻结文件**：`evaluation.py`、`safe_model.py`、`data.py`、`experiments.py`、`dataflow.py`。不得改历史权重、阈值、冻结清单、报告或旧实验。
- **禁止重新执行旧757条最终测试**。不运行 `freeze-final`、`evaluate-final`、`train` 或数据准备/下载；新 Jev 质量评估是另一个有独立授权、数据和预算的任务。
- 不读真实 `.env`、`var/`、工作数据库、原始短信、旧数据快照或留出文本，不拿它们截图。设计默认用临时目录、自编数据、假令牌与 `httpx.MockTransport`。
- 不发付费模型请求，不运行 `scripts/verify_jev.py --live`，不部署、不重启线上服务。`scripts/verify_v2_http.py` 会复制真实 `var/`，本轮不要运行。
- 不弱化 CSP、鉴权、TLS、限流或数据边界；不添加外部脚本、广告 SDK、追踪或密钥持久化。若预留未来推广位置，只能默认隐藏且不接触工作数据。

## 交付检查

使用现有依赖，不为视觉工作随意重装环境：

```sh
.venv/bin/python -m unittest discover -s tests -v
npm test --prefix frontend
npm run build --prefix frontend
.venv/bin/python scripts/verify_jev.py
```

最后一项默认 dry-run，仅准备自编请求；不能附加 `--live`。这些软件测试使用自编/临时 fixture，不运行真实757条评估。

必须实际打开隔离演示的生产构建，检查桌面和窄屏、键盘、焦点、空/加载/错误/保存未知/修订冲突/清除身份状态，并核对 CSP。保留截图与检查结果，注明全部为模拟数据；构建通过不等于视觉验收完成。具体状态清单、API和冻结文件摘要见交接文档。
