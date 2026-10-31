# Text Model Lab

[公开HTTPS演示](https://textlab.madokanun.top/workbench/) · [GitHub源码](https://github.com/oNanachii/text-classification-workbench) · [交付与验收](docs/DELIVERY_2026-10-28.md)

2026-10-31启用独立域名，无需输入端口；根网址会进入工作台。旧入口保留，应用、模型与数据未迁移或重训。新入口已核对可信HTTPS、页面资源、本地预测和同源权限限制，本次没有真实Jev调用。

英文短信判断与人工复核工作台。当前v2.1.0（当前前端运行源码`bd7567a`，后端接口与模型实现未改）已上线：公开页面支持本地模型、Jev或同一文本并排对照；Jev使用自己的TypeSafe Key，公开服务不会借用服务器Key。数据导入、审核、训练快照和候选管理仍只在私有工作台进行。

项目使用AI辅助开发。源码、软件检查、真实训练、公开部署与本人独立讲解分别记录。原v1实验与文件保留，v2结果来自另一份预先固定的分组划分，不能直接把两组指标作为提分对比。

五个功能区为判断体验、批量导入、人工审核、效果对照、模型与规则。公开站提供判断与公开证据，私有管理功能明确锁定。

## 现在如何体验

1. 在公开HTTPS页面输入一条英文短信，默认选择本地模型即可判断，不需要外部Key。真实任务仅为`ham/spam`，不是已经验证的中文留言分类器。
2. 如选择Jev或并排对照，输入自己的TypeSafe Key，并明确同意文本由工作台转交TypeSafe、使用该Key额度。公开Jev BYOK已开启，服务器共享Key严格关闭；缺Key或缺同意不会回退到共享凭据。
3. 查看两边的标签、实际版本、耗时及返回用量。单边失败或unknown仍保留另一边有效结果；双方一致不等于正确。清除Key、文本与结果，或离开判断面板后，旧响应不能回填。

Key只在当前页面和单次请求内存，不保存到浏览器存储或数据库；外部判断必须通过HTTPS。取消、超时或连接失败仍可能已经消耗TypeSafe额度，不自动重试。本地分数未经校准，Jev confidence表示概率分布集中程度，均不能当作这条短信的正确率。模型建议不会自动成为人工标签或进入训练快照。

固定Jev版本、错误状态与额度见[Jev接口契约](docs/JEV_API_CONTRACT.md)。本轮未发起真实Jev调用，也没有新增质量、耗时或成本效果结论；未来评测应按[新数据计划](docs/JEV_EVALUATION_PLAN.md)独立开展，不能重用旧757条最终测试。

## 当前设计与验证

用户在Claude完成五分区重构后，已接回独立审查并修正三处异步问题：旧审核响应覆盖另一条草稿、旧导入确认清空新CSV、旧登录响应清除新令牌。新增5条实际Vue组件脚本回归在修复前失败、修复后通过；完整41项Node与构建通过。36项Python检查通过，后端/API及五个冻结文件与改版前字节一致。

独立浏览器使用临时自编数据与模拟上游，验证本地判断、对照分歧、导入期间新草稿保留、纠错后列表刷新、候选与实际加载分离、清除身份。公网另外核对真实本地模型预测、公开私有入口锁定、桌面与390px视口、五份静态文件和TLS/CSP。详细收据与截图保存在操作者本机私有目录，不作为公开上传内容。

本轮未发起真实Jev或付费调用，没有训练或重跑旧757条测试。模拟Jev对照不证明真实模型质量。Safari、Firefox、实体触控设备、屏幕阅读器和用户本人视觉认可仍需另行验收。

[Claude设计交接](docs/CLAUDE_FRONTEND_HANDOFF.md)与[原启动提示](docs/CLAUDE_START_PROMPT.txt)保留为本轮任务依据；设计实现已接回并部署，后续按新的具体任务增量修改，不必重复执行整轮重设计。

## 一次完整流程

1. 准备v2数据版本：按预先确定的模板/近重复候选分组规则划分，原始文件和v1划分不改写。
2. 在私有工作台预览CSV，确认后进入待审核池。列名为`text,label`，标签只接受`ham`或`spam`。
3. 复核并保存标签及依据。标签改变与历史同事务提交；待审确认或标签改变会推进记录版本，旧版本请求返回409。同标签重复审核不制造纠错历史。
4. 导出内容寻址快照。待审核行不进入快照，留出集重复/模板重叠的导入被拒。
5. 本地CLI显式读取快照，生成独立run目录、实际训练曲线、固定验证指标及安全数值产物。
6. 选择候选，校验后重启服务。网页和预测返回实际加载版本；注册清单选择与已加载版本分别显示，可以回退到上一选择。

## 本地启动

Python 3.13，macOS/Linux。以下是独立训练环境的历史工作流，不是体验v2.1或前端设计的必做步骤；已有正式工作库不要重复初始化。创建新数据版本须使用独立工作目录，保留旧冻结文件和评估记录。

```sh
python3.13 -m venv .venv
.venv/bin/python -m pip install -r requirements-backend.txt
.venv/bin/python baseline.py download
.venv/bin/python workbench.py prepare
.venv/bin/python workbench.py create-token
.venv/bin/python workbench.py snapshot
```

快照命令返回`snapshot_id`。复制该ID，使用新的run名训练；已有run拒绝覆盖：

```sh
.venv/bin/python workbench.py train --snapshot '<snapshot_id>' --run my-first-run
.venv/bin/python workbench.py select --run my-first-run --kind lr --threshold 0.5
.venv/bin/python workbench.py serve --port 8765
```

打开`http://127.0.0.1:8765/workbench/`。操作令牌保存在被忽略的`var/operator-token.txt`，权限0600；私下读取到页面的密码输入框，令牌仅保存在当前页面内存，不要粘贴到聊天或Git。服务启动命令会私下读取该文件，无需把令牌写在命令行中。原来的`app.py`和`baseline.py`为v1兼容入口；新完整流程使用`app_v2:create_app`与`workbench.py`。

数据与产物默认保存在被Git忽略的`var/`；可在子命令前加`--root /绝对路径`建立独立工作空间。原`data/working.sqlite3`与`artifacts/`下的v1报告、权重保持不变。

## 数据导入与复核

CSV上限5MiB、10000行、单文本2000字符；审核池累计上限20000行。预览显示空值/非法标签、重复、标签冲突及留出集重叠计数，确认只导入可接受的新行。自带文件必须属于同一英文短信二分类范围，首版不接受任意分类标签或模型文件。

`examples/authored_sms.csv`是AI编写并内部复核的四条链路烟测样本，不是真实业务数据或真实错标修正。它验证导入到重训的工程流程，不证明质量改善。CLI也支持同样的流程：

```sh
.venv/bin/python workbench.py preview examples/authored_sms.csv --source 'AI-authored workflow examples, not field data'
.venv/bin/python workbench.py confirm '<preview_id>'
```

UCI初始训练行按公开源标签纳入基线，不表示已逐条人工复核。新增行确认导入后仍需在数据审核页逐条复核，或用`review`子命令明确传入样本ID、标签、复核依据和当前revision。之后再生成新快照、用新run名训练。正式标签不存在可信错误时，不为提分随意翻转标签；隔离故障注入只用于软件检查。

## 实验与最终评估

v2预处理规则在新模型结果产生前固定：标准化完全重复去除、URL/数字等模板分组、按保守候选阻塞的字符5-gram Jaccard≥0.85连接分组，再按seed42的组哈希做70/15/15划分。该检查覆盖明确模板和词法候选，不声称识别所有语义改写。

真实v2数据为3619条训练、783条验证、757条当时封存的测试；最后一组已完成一次固定评估。向量器只拟合训练快照，两个模型使用同一份向量器；MLP默认20轮，按验证损失选择检查点。训练和实验页只读取验证集结果；原v1的774条验证和0.9531保持原义。

旧757条最终测试已在冻结模型、阈值与实现后执行过一次，当前只保留历史结果，**不得重跑或用于Jev调参**。`freeze-final`/`evaluate-final`是历史实现机制，不是当前体验步骤：前者固定选择与源码/运行时摘要，后者在读取测试内容前建立不可覆盖的尝试记录。未知结果也不能自动重试。

五个冻结依赖`evaluation.py`、`safe_model.py`、`data.py`、`experiments.py`、`dataflow.py`及旧权重、阈值、报告保持不变。新任务或Jev质量比较需要独立的新数据版本和评测授权，不能把旧测试重新称为未见数据。

## 首轮最终测试记录

主线程在验证选择后冻结了`v2-import-reviewed-seed42 / mlp16 / 0.5`，对757条封存测试执行一次最终评估：macro-F1为0.9518524408，误判正常短信FP为0，漏判垃圾短信FN为16。评估后没有调整模型、阈值或训练方案。该结果属于固定英文短信数据版本，不代表线上流量或中文表现，也不把四条AI烟测训练新增对应的验证差异称为真实质量收益。

## 轻量公开部署

训练仍在本地CPU执行。公开推断使用`JSON + NPZ`数字格式，`allow_pickle=False`；不需要Torch、scikit-learn或joblib，不接收上传的模型/可执行脚本。数值格式与原真实训练模型在固定验证集上完成加载一致性核对。

明确选择模型后导出公开包，目标目录必须不存在：

```sh
.venv/bin/python workbench.py export-public deploy/public-bundle
```

公开包只包含选定run的数值权重、词表、来源报告和服务清单；不含数据库、快照、导入CSV或留出集文本。若已有与选定模型一致的独立最终测试报告，也复制该结果。公开模型与词表本身仍是导出的模型工件，操作者须确认所用训练数据允许公开；首轮最终候选依据验证记录选择v2-import-reviewed-seed42 / mlp16 / 0.5，其中含4条明确标注AI编写的烟测新增训练样本。该选择用于辅助审核，不自动删除短信，也不把烟测样本造成的验证变化称为真实业务质量收益。

服务器使用独立Python虚拟环境按`requirements-inference.txt`安装，只监听回环并经HTTPS代理提供服务：

```sh
SMS_LAB_ROOT=/绝对路径/public-bundle SMS_LAB_PUBLIC=true .venv/bin/python -m uvicorn app_v2:create_app --factory --host 127.0.0.1 --port 3111 --workers 1 --no-access-log --proxy-headers --forwarded-allow-ips=127.0.0.1
```

`SMS_LAB_PUBLIC=true`时即使携带操作令牌也不能导入、审核、生成快照或切换模型。当前公网以`SMS_LAB_JEV_ENABLED=true`开启BYOK能力，`SMS_LAB_JEV_SERVER_KEY_ENABLED=false`关闭服务器Key；开关本身不调用模型。仅信任回环代理提供的协议信息，不信任任意客户端转发头。预测长度、频率和并发有上限，不保存访客文本。部署资源观测不构成容量保证。

## 复现与证据

```sh
.venv/bin/python -m unittest discover -s tests -v
npm test --prefix frontend
npm run build --prefix frontend
.venv/bin/python scripts/verify_jev.py
```

上述软件检查使用自编临时数据、数值权重或明确标注的故障注入；Jev脚本默认dry-run、零网络，不要附加`--live`。历史`verify_v2_http.py`会复制真实v2工作库与模型，不能作为前端设计默认命令；新的视觉验收应使用隔离fixture与fake transport。真实训练记录保存在`var/runs/`，公开摘要和验收记录位于`artifacts/v2-verification/`。尚未完成的设备/浏览器与本人验收应继续明确记录。

数据来源：Almeida与Hidalgo的[UCI SMS Spam Collection](https://archive.ics.uci.edu/dataset/228/sms+spam+collection)，DOI `10.24432/C5CC84`；原始压缩包、readme与来源hash保留在`data/`。项目保留归属说明，数据许可与源码许可分开。
