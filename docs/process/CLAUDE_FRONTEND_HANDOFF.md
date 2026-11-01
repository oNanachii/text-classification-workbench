# Text Model Lab 前端设计交接

2026-10-29。本文是用户操作 Claude Code 的实现入口，不是新视觉设计或部署已完成的声明。已有 v2 工作台和 Jev 单条判断接口可供设计接入；本轮以源码与模拟检查为依据，没有发起真实付费 Jev 请求。

## 产品应该让人完成什么

用户应能看懂当前判断针对什么任务，输入一条英文短信，选择本地模型、Jev 或并排对照，理解结果与限制；私有操作者还能导入同格式数据、复核标签、查看实验、管理服务候选。不要把这些能力包装成尚未建立的中文留言分类产品。

任务固定为 `english-sms-ham-spam-v1`：`ham` 是正常个人、事务或明确订阅的英文短信，`spam` 是未经请求的推广、奖品诱导或诈骗。模型结果只是建议。新中文任务、其他标签、批量 Jev 判断和独立质量评测不在这次前端设计范围。

建议按下列职责组织。导航名称、组件拆分与视觉形式可以调整，前后关联必须清楚：

| 功能区 | 用户要完成的动作 | 应呈现的状态和边界 |
| --- | --- | --- |
| 判断体验 | 输入 → 选择引擎 → 外发同意（需要时）→ 查看判断 | 默认本地；Jev未启用、无本地模型、缺凭据、TLS阻断、单边失败、双方分歧、unknown |
| 批量导入 | 选择CSV → 来源说明 → 预览 → 明确确认 | `text,label`；无效、重复、冲突、留出重叠各有计数；确认后进待审池，不自动调用Jev |
| 人工审核 | 筛选待审 → 查看原文/历史 → 标签与依据 → 保存 → 下一条 | 同标签确认与真正纠错分清；保存未知、409、列表刷新失败、分页回退都能恢复 |
| 效果对照 | 查看同版本实验与即时对照的不同证据 | 历史训练/验证表与最终测试分开；Jev没有新质量分数时不造排名、不把单条示例叫评测 |
| 模型与规则 | 查看当前加载模型、已选候选、任务/标签规则、能力开关 | 候选不等于已生效；本地权限才有选择/回退；真实重启不在设计演示中执行 |

可自由选择版式、字体层级、颜色、间距与交互反馈。现未确认独立设计模板目录；若发现用户明确提供的模板，先检查许可、资源与适配情况，否则直接基于现有 Vue 设计，不虚构模板来源。避免堆叠技术字段影响主要流程；版本、快照、来源、限制应仍可查阅。

## 当前实现地图

| 位置 | 当前职责 |
| --- | --- |
| `frontend/src/main.js` | 加载App与统一样式 |
| `frontend/src/App.vue` | 实际主入口；当前四区导航、操作者凭据、导入审核、历史实验、候选/加载状态 |
| `frontend/src/components/DecisionPanel.vue` | 新的三模式单条判断、外发同意、凭据输入与结果展示 |
| `frontend/src/decisionSession.js` | 页内Key、发送前TLS检查、一次请求、过期响应拒绝、清除/取消边界 |
| `frontend/src/importGuard.js` | CSV草稿、文件读取、预览请求的身份与内容绑定 |
| `frontend/src/samplePage.js` | 审核/导入后的列表刷新、空末页回退、迟到响应拒绝 |
| `frontend/src/components/LossChart.vue` | 当前使用的真实历史曲线组件 |
| `frontend/src/style.css` | 当前样式；可整体整理但保留可访问状态 |
| `frontend/src/components/PredictionPanel.vue` 等旧三个面板、`frontend/src/api.js` | 历史组件，不是当前主流程；不能仅改旧文件 |
| `app_v2.py` | 当前API、权限、CSP、静态生产构建挂载 |
| `decision.py` | 固定TypeSafe端点、Jev契约校验、TLS/权限/额度、临时判断结果 |

Vite的生产base是 `/workbench/`，后端挂载 `frontend/dist`。前端API使用同源 `/api/*`。不要把v1 `app.py` 与旧接口当作新页面的后端。

## API与状态保留

完整新增判断契约见 [JEV_API_CONTRACT.md](../JEV_API_CONTRACT.md)。下面是设计需要知道的最小映射，字段与错误以当前实现为准。

| 接口 | 作用与权限 |
| --- | --- |
| `GET /api/config`、`/api/health` | public/操作者状态、Jev能力与服务器Key可用性、实际加载模型 |
| `POST /api/decision` | `text`、`engine=local/jev/compare`、`credential_source=byok/server`、`external_consent`；最多2000字符 |
| `POST /api/predict` | 旧本地预测兼容接口，不要删除或随意改变契约 |
| `GET /api/service` | `loaded`、`selected`、`revision`、`restart_required`；历史受权限限制 |
| `GET /api/runs`、`/api/final-report` | 已完成实验与单独最终测试报告；本地需认证，public仅发布包中的结果 |
| `GET /api/dataset`、`/api/samples`、`/api/samples/{id}` | 非public已认证操作者；样本分页、筛选、版本与历史 |
| `POST /api/imports/preview` | `{csv, source}`；只预览，不写成已审核样本 |
| `POST /api/imports/confirm` | `{preview_id}`；重复确认沿原ID核对，不另造确认记录 |
| `PATCH /api/samples/{id}` | `{label, note, expected_revision}`；修改与历史同事务，冲突409 |
| `POST /api/snapshots` | 当前已审核数据的内容寻址快照；不会自动训练 |
| `POST /api/service/select` | `{run_id, kind, threshold, expected_revision}`；只选择候选 |
| `POST /api/service/rollback` | `{expected_revision}`；回退选择，仍需重启核对loaded |

导入上限5MiB、10000行，单条2000字符，审核池累计20000行；仍只接受 `ham/spam`。网页不提供任意模型文件、Python执行或自动依赖安装。

外部请求最多一次上游调用，无隐式重试。HTTP 200 也可能含 `jev.status=error/unknown`；比较时保留已成功的本地结果。只有双方 `status=ok` 才展示有效分歧/一致结论。错误文案用已有常量，不展示任意响应体。`usage=null` 表示服务未返回，不显示为零消耗。

## 凭据与错误边界

- BYOK仅在当前页面内存；`X-TypeSafe-Key`只传给同源后端。不得进入JSON正文、URL、localStorage、sessionStorage、IndexedDB、数据库、截图、分析埋点或日志。
- 外发前说明文本会由工作台转交TypeSafe，使用所选Key额度。默认本地模式不传任何Key或操作者令牌给判断接口，也不外发文本。
- 浏览器在发请求之前检查安全来源。公网必须HTTPS；只有非public本机回环开发允许HTTP。后端再检查scheme与Origin，不直接相信任意客户端的`X-Forwarded-Proto`。不要为开发方便取消这些检查。
- public不能使用服务器Key；BYOK缺失或失败不能回退到服务器Key。服务器Key选项仅在非public、已认证且后端明确启用时出现。
- 输入、引擎、凭据来源、Key、同意项改变时清除旧结果并使旧响应无效。清除按钮、离开判断面板和操作者身份改变时清空Key、文本、同意项与结果。换身份后迟到响应不能恢复旧状态。
- 中止请求只代表前端停止等待，不能保证TypeSafe没有处理或计费。不能把“取消/超时/连接失败”写成“免费、没有调用、没有保存”。
- 本地写入结果未知时，保留必要草稿并引导重新读取同一记录、预览或服务版本；已经确认成功但列表刷新失败时，不能要求用户重复提交。401清除私有状态；409保持草稿并要求读取新版本后再决定。
- 保留CSP、自源资源、鉴权、只读部署、请求上限与现有限流。不要添加远程字体/脚本、广告SDK、追踪或弱化`script-src/connect-src`。若以后需要推广布局，这次最多预留默认隐藏且与工作数据隔离的位置。

## 结果如何解释

本地返回的是未校准`spam_score`及其补数，需标出实际阈值、模型和数据版本，不能称为该短信的正确率。Jev返回的概率分布没有经过本地校准；`confidence_kind=choice_distribution_concentration`是分布集中程度，不是准确率。双方标签一致也不能替代人工标签。

旧英文短信最终测试是历史证据，当前代码/文档已记录757条的一次固定评估；它不能再用来为Jev挑提示、调阈值或反复比较。新Jev没有正式质量评测时，应显示“尚未评测”及已知工程状态。新的评测与中文任务要求见 [JEV_EVALUATION_PLAN.md](../JEV_EVALUATION_PLAN.md)，本轮不执行。

## 不可变范围与隐私

五个冻结依赖必须保持字节不变：

| 文件 | 交接时SHA-256 |
| --- | --- |
| `evaluation.py` | `394ce1081ecc772c3213e3144c8ee15f09ccab80612bd7a19b417a6967cfa4ef` |
| `safe_model.py` | `babee416eab7a9c8c14c1545bf7964e7f9e8f42f8d4cfe4fc0640c605010abec` |
| `data.py` | `ba44ca677ae7b287faf8366057465f982e1ca8b0f721ddbc66c7b86fd35ac2a1` |
| `experiments.py` | `91f61ed718b6e4315bf659528f8f83544721b017ea7205efc95a36b45fbee65c` |
| `dataflow.py` | `6e0849d45c29ed0dfa472e60d972d7b8ab9b908b96b2d869d0c7b47aec3e3dcf` |

不读取真实`.env`、`var/`、工作库、历史CSV、原始短信、正式快照或留出文本；不改权重、阈值、冻结计划、尝试记录和历史报告。禁止重跑757条最终测试。不要运行`train`、`prepare`、`download`、`freeze-final`、`evaluate-final`或`verify_jev.py --live`。不要部署或重启线上服务。

## 默认设计演示与验收

默认使用自编英文句子、假令牌与临时数据目录。可复用`tests/test_workbench_v2.py`中的`seeded`和`numeric_fixture`，它们写入临时目录并生成自编数值模型，不读取真实项目数据；Jev使用`tests/test_decision.py`同类`httpx.MockTransport`。临时服务须显式传入root、假的admin_token、public与decision_service，避免落回默认`var/`或读取环境Key。

当前没有声称交付独立的设计服务器脚本。如果需要，新建仅使用上述fixture的本地设计入口，并明确标记“模拟数据/无真实模型调用”。可先用浏览器路由模拟API制作状态，再用真实FastAPI + 临时fixture验证权限与生产构建。不要运行现有`verify_v2_http.py`：它会复制真实`var/`，不属于默认设计验收。

软件检查：

```sh
.venv/bin/python -m unittest discover -s tests -v
npm test --prefix frontend
npm run build --prefix frontend
.venv/bin/python scripts/verify_jev.py
shasum -a 256 evaluation.py safe_model.py data.py experiments.py dataflow.py
```

优先使用已安装环境，不默认重装依赖。`verify_jev.py`最后不带`--live`，只输出自编样例的请求准备摘要，零网络调用。现有软件测试里的最终评估保护用例使用两条合成fixture，不是旧757条测试。

必须实际浏览生产构建，而不只看Vite开发页或静态截图：

1. 桌面约1280×800与窄屏约390×844；无全页横向溢出，表格自身可滚动，主要操作可触达。
2. 键盘可完成输入、引擎选择、同意、提交、清除、审核及冲突恢复；标签、焦点、错误/状态播报与对比度可辨。
3. 判断空白、加载、local成功、Jev未启用、缺Key、TLS阻断、单边失败、分歧、unknown、429与无自动重试都可检查。
4. 发出模拟慢请求后清除/换Key/改文本/换引擎/换身份/离开面板，迟到响应不能回填；检查浏览器存储没有凭据或短信。
5. 导入空结果/部分有效/冲突/留出重叠，修改CSV后旧预览失效；异步文件读取和预览不能覆盖新草稿。
6. 待审为空、末页审核后回上一有效页、同标签确认、保存后刷新失败、409、响应未知分别展示准确状态，不制造成功提示或重复提交。
7. public即使带假操作者令牌仍拒绝写入/服务器Key；非public才展示相应管理操作。比较页面不能把selected画成loaded。
8. 用后端提供生产静态页实际核对CSP与浏览器控制台；不要为消除错误增加unsafe-inline或外部资源域名。

保存必要截图、操作结果、浏览器/视口、软件检查结果与冻结文件核对，注明自编数据和模拟上游。代码通过、mock链路通过、真实上游质量、实际视觉验收、部署和本人讲解是不同结论。

## 交接时的证据

本次独立复核实际重跑14项Jev Python检查与19项Node检查，全部通过；冻结五文件无Git差异且摘要如上。接入实现方另已报告完整36项Python、19项Node和生产构建通过。本交接没有改应用实现、构建产物或部署，没有运行真实Jev或旧最终测试，也没有完成新的浏览器视觉设计验收。后续修改以当时实际运行结果为准。
