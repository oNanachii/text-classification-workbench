# v1 兼容入口与历史运行说明

2026-11-01 已将原根目录的 v1 代码移到 `legacy/`，数据清洗与归一化仍共享 `textlab/data.py`。以下命令均从仓库根目录执行；完整当前流程请用 [README](../README.md) 的 `textlab.workbench`，公开服务使用 `textlab.app_v2:create_app`。

## 兼容命令

```sh
.venv/bin/python -m legacy.baseline --help
.venv/bin/python -m legacy.train_mlp --help
.venv/bin/python -m uvicorn legacy.app:app --host 127.0.0.1 --port 8765
.venv/bin/python -m legacy.training_snapshot
.venv/bin/python -m legacy.verify_api
.venv/bin/python -m legacy.verify_storage
.venv/bin/python -m legacy.verify_training_snapshot
```

这些是原 v1 学习接口及检查，仍读取原 `data/` 与 `artifacts/` 路径，不能原样作为公开可写服务部署。`legacy.verify_api` 需要另一个终端中已有 v1 本地服务。下载是当前仍使用的共享功能，推荐 `python -m textlab.data download`；`python -m legacy.baseline download` 也保留兼容。

不要为了核对目录迁移重新执行旧训练、初始化工作库或旧评估。新实验需要独立数据版本和新的 run 名。本次重构的验证使用 `scripts/verify_refactor.py` 与现有临时 fixture 测试，不运行下面的历史训练流程。

## 原“运行说明.txt”（历史记录，命令路径已迁移）

以下保留早期 v1 学习阶段的原说明，用于理解已有实验，不代表当前 v2 状态。其“测试集未评估”“近重复尚未检查”“训练尚未消费快照”等陈述仅属于当时的 v1：v2 后来已完成分组划分、快照驱动训练及一次 757 条最终测试，详情见 [版本历史](HISTORY.md)。所有模型成绩、检查数量与软件版本仍按原记录解释，不能视为本次重新验收。

```text
文本模型工作台：数据、训练、接口、标签纠错与Vue前端

数据来源：UCI SMS Spam Collection，Almeida & Hidalgo (2011)，DOI 10.24432/C5CC84。
来源页：https://archive.ics.uci.edu/dataset/228/sms+spam+collection
UCI页面列出的许可为CC BY 4.0；原始压缩包、原文、readme和SHA-256保留在data/。

在本目录中运行；当前环境已经安装依赖。新环境先执行：
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-backend.txt

首次构建前端（本轮已构建；实际验证Node 24.21.0、Vue 3.5.44、Vite 8.3.5）：
cd frontend
npm ci
npm run build
cd ..
构建后再启动后端，工作台地址为http://127.0.0.1:8765/workbench/。
前端由同一后端服务提供，使用同源API；/docs仍可查看接口。
修改Vue后重新构建并刷新页面。开发模式npm run dev会将API代理到8765。

按顺序执行：
.venv/bin/python -m legacy.baseline download
.venv/bin/python -m legacy.baseline prepare
.venv/bin/python -m legacy.baseline train
.venv/bin/python -m legacy.baseline validate --thresholds 0.5 0.8 0.9
.venv/bin/python -m legacy.baseline predict "Free gift" --threshold 0.5

CPU小模型首次训练与加载预测：
.venv/bin/python -m legacy.train_mlp train
.venv/bin/python -m legacy.train_mlp predict "Free gift" --threshold 0.5

本轮mlp_cpu_v1已经完成；同名目录拒绝覆盖。再次独立实验时指定新的--run-name，
例如：.venv/bin/python -m legacy.train_mlp train --run-name mlp_cpu_v2
隐藏宽度通过--hidden-units调整，两层Linear共享这个宽度，默认仍为32。
16维对照已保存为mlp_cpu_h16_v1；再次运行需另取新名称，例如：
.venv/bin/python -m legacy.train_mlp train --run-name mlp_cpu_h16_v2 --hidden-units 16
两种宽度的本轮比较见artifacts/width_comparison_v1.json，测试集仍未评估。
模型为2000维TF-IDF→32隐藏单元→ReLU→1个原始得分，默认20轮、每批64条、CPU。
小模型复用基线的向量器，使用BCEWithLogitsLoss和Adam（学习率0.001）。
每轮在固定模型上记录训练/验证损失，以验证损失最低的一轮保存推断检查点。
结果在artifacts/mlp_cpu_v1/report.json，包含首步参数审计与保存加载一致性。
检查点仅支持加载推断，未保存优化器状态以支持精确断点续训。

本地预测接口：
.venv/bin/python -m uvicorn legacy.app:app --host 127.0.0.1 --port 8765
启动后可查看交互文档：http://127.0.0.1:8765/docs
GET /health检查服务状态，GET /models列出模型，POST /predict进行预测。
请求JSON示例：{"text":"Free gift","model":"mlp16","threshold":0.5}
model必填，可选lr、mlp32、mlp16；threshold默认0.5且须为[0,1]有限数值。
text须为1..2000字符的字符串，规范化后为空白会被拒绝。
有效请求返回200和label/spam_score/threshold/model/model_version。
校验失败返回422；错误体给出字段位置和原因，不返回预测结果。
模型启动时加载一次；每次请求只做预处理、特征转换与推断，不更新权重。
在另一终端运行真实HTTP检查：.venv/bin/python -m legacy.verify_api
验证报告：artifacts/api_verification_v1.json；本轮通过26项接口检查。
该接口仅供本机学习。

样本查询与标签纠错：
GET /samples?limit=20&offset=0&label=spam&q=free
GET /samples/{sample_id}
PATCH /samples/{sample_id}/label，JSON示例：{"label":"ham","note":"复核后纠正"}
GET /samples/{sample_id}/corrections
SQLite工作库为data/working.sqlite3；首次启动只复制3611条train样本。
更改标签与追加历史在同一事务中完成，同标签重复提交不会新增修订。
同一源版本重启时保留工作库中的标签和历史；版本不匹配会停止，不自动重置。
固定splits.json、原始数据与模型产物不随纠错变化；验证/测试样本不进入可编辑工作副本。
若要把纠错用于后续训练，应另建数据版本并显式训练。
隔离存储检查：.venv/bin/python -m legacy.verify_storage
该检查使用临时SQLite副本验证修改、重连、重启与失败回滚，不改实际工作标签。
报告为artifacts/storage_verification_v1.json；本轮通过24项检查。
本机HTTP读取和同标签无改动保存验证见artifacts/sample_live_api_verification_v1.json。

前端页面：文本预测、样本纠错、实验对比。
实验指标与曲线通过GET /experiments读取现有验证报告，不生成演示分数。
样本的编辑草稿与已保存值分离；后端确认成功后才更新已保存状态和成功提示。
浏览器验收覆盖预测/阈值、空结果/筛选/翻页、纠错成功/历史、失败回滚/草稿、刷新持久化与390像素窄屏。
保存写入与故障注入使用SMS_LAB_DB指定的隔离临时库，未改实际工作标签。
浏览器报告：artifacts/frontend/browser-verification.json；截图保存在同目录。
需要隔离工作库时，启动服务前设置SMS_LAB_DB为新SQLite路径，默认工作库仍为data/working.sqlite3。

prepare固定数据划分；如果已有划分与新数据/规则不一致，会停止，需另建数据版本。
向量器与逻辑回归保存在同一个artifacts/baseline.joblib中。
预测入口先执行textlab.data.normalize，再复用保存的向量器和分类器。
不要直接把未经规范化的文本传给bundle中的Pipeline。
validate更改阈值不重训模型，报告写入artifacts/validation_report.json。
报告包括样本量、混淆计数、各类别指标、macro-F1、多数类基线及真实误判解释。

当前只报告验证集，没有选定业务阈值，也没有计算测试集成绩。
目前去除的是统一大小写/空白后的完全重复；近重复、模板改写尚未检查。
本地数据到模型、接口、前端预测和纠错闭环已运行。最终测试集、近重复处理、外部复现部署和学习者独立验收尚未完成。

2026-10-28 新增：不可变训练数据快照
.venv/bin/python -m legacy.training_snapshot
.venv/bin/python -m legacy.verify_training_snapshot

导出会以只读事务取得工作库中的原train样本与纠错历史，核对来源和连续修订链，
保存到artifacts/training_snapshots/train-<完整内容哈希>.json。
相同完整状态复用版本；修订生成新版本；同名内容损坏会报错，不覆盖旧快照。
导出不修改实际标签、历史、冻结数据或模型。源划分只解析manifest和train，
其余内容只做二进制来源哈希；不评估测试集。
本轮7项隔离边界检查通过；实际3611条训练样本、0条纠错，记录见
artifacts/training_snapshot_verification_v1.json。
训练脚本尚未消费快照；本功能不能表述为已经完成“纠错后重新训练”。
```
