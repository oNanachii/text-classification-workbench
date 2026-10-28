# Text Model Lab

英文短信分类与数据纠错工作台。操作者导入同格式数据并复核标签，生成不可变训练快照，在本地CPU训练逻辑回归和小型MLP，再根据同一验证集选择服务版本。公开部署只提供明确版本的预测与只读结果。

项目使用AI辅助开发。源码、软件检查、真实训练、公开部署与本人独立讲解分别记录。原v1实验与文件保留，v2结果来自另一份预先固定的分组划分，不能直接把两组指标作为提分对比。

## 一次完整流程

1. 准备v2数据版本：按预先确定的模板/近重复候选分组规则划分，原始文件和v1划分不改写。
2. 在私有工作台预览CSV，确认后进入待审核池。列名为`text,label`，标签只接受`ham`或`spam`。
3. 复核并保存标签及依据。标签改变与历史同事务提交；待审确认或标签改变会推进记录版本，旧版本请求返回409。同标签重复审核不制造纠错历史。
4. 导出内容寻址快照。待审核行不进入快照，留出集重复/模板重叠的导入被拒。
5. 本地CLI显式读取快照，生成独立run目录、实际训练曲线、固定验证指标及安全数值产物。
6. 选择候选，校验后重启服务。网页和预测返回实际加载版本；注册清单选择与已加载版本分别显示，可以回退到上一选择。

## 本地启动

Python 3.13，macOS/Linux。完整训练环境：

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

真实v2数据为3619条训练、783条验证、757条封存测试。向量器只拟合训练快照，两个模型使用同一份向量器；MLP默认20轮，按验证损失选择检查点。训练和实验页只读取验证集结果；原v1的774条验证和0.9531保持原义。

最终测试由操作者在模型、阈值与实现冻结后明确执行一次：

```sh
.venv/bin/python workbench.py freeze-final --run '<chosen_run>' --kind lr --threshold 0.5 --reason '根据已完成验证记录作出的选择'
.venv/bin/python workbench.py evaluate-final
```

`freeze-final`只封存选择、评估/推断/归一化/指标等依赖源码摘要及运行时版本，不评价模型；`evaluate-final`在读测试内容和推断前创建不可覆盖的尝试记录。重复执行拒绝，失败或未知结果必须先检查原记录，不自动重试。报告不输出测试文本或误判原文，测试结果不能再用于选择该版本。

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
SMS_LAB_ROOT=/绝对路径/public-bundle SMS_LAB_PUBLIC=true .venv/bin/python -m uvicorn app_v2:create_app --factory --host 127.0.0.1 --port 3111 --workers 1 --no-access-log
```

`SMS_LAB_PUBLIC=true`时即使携带操作令牌也不能导入、审核、生成快照或切换模型。预测长度、频率和并发有上限，不保存访客文本。部署前实测目标服务器资源、端口和既有服务，不把本地内存观测当作服务器容量保证。

## 复现与证据

```sh
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python scripts/verify_v2_http.py
npm ci --prefix frontend
npm test --prefix frontend
npm run build --prefix frontend
```

软件检查使用一次性数据库或明确标注的故障注入；实际HTTP检查复制v2工作空间与已完成数值模型，在回环端口验证导入审核、重启生效、回退和公开只读，不改原工作库。真实训练记录保存在`var/runs/`，公开摘要和验收记录位于`artifacts/v2-verification/`。尚未完成的设备/浏览器与本人验收应继续明确记录。

数据来源：Almeida与Hidalgo的[UCI SMS Spam Collection](https://archive.ics.uci.edu/dataset/228/sms+spam+collection)，DOI `10.24432/C5CC84`；原始压缩包、readme与来源hash保留在`data/`。项目保留归属说明，数据许可与源码许可分开。
