# 文本分类工作台讲解与关键修改

代码由AI辅助实现。下面用于本人实际讲解和修改验收，生成文档不代表已掌握，也不要求手写全部系统。

## 一次运行的讲解顺序

先用一条英文短信预测，指出返回的模型、run和snapshot版本。然后进入本地审核页，说明原UCI标签只是源数据标签，不是逐条人工验收；新增CSV需要预览、确认和复核才进入训练快照。

沿一个实际快照找到对应run：训练只使用其中已确认的训练行，向量器也只从这些训练文本拟合。验证集只用于checkpoint与版本选择。展示一次真实曲线和FP/FN，说明新数据或MLP不保证变好。最后区分注册清单里的候选与进程实际加载版本，演示重启生效和回退。

首轮新增的四条CSV样本是AI编写的明确烟测输入，复核未改变标签，没有真实错标修正。它们证明数据流打通；不能把两个run的验证差异包装成业务数据质量提升。

## 关键代码入口

| 问题 | 入口 |
| --- | --- |
| 数据如何划分，近重复规则覆盖到什么程度 | dataflow.py中的template_key、char_grams、DataStore.prepare |
| 预览为何不直接入训练，重复确认如何处理 | DataStore.preview_csv、confirm_import、review |
| 并发修改与事务历史 | DataStore.review的expected_revision及transaction |
| 训练究竟用了哪批标签 | DataStore.snapshot、load_snapshot与experiments.train的snapshot_id |
| 向量器为何不能看验证或测试文本拟合 | experiments.train中fit_transform仅用于快照，transform用于固定验证 |
| 模型怎样在无Torch的服务里推断 | safe_model.py中的SafeModel，JSON词表与allow_pickle=False的NPZ权重 |
| 候选为何不会立即改变当前预测 | experiments.select/rollback及app_v2.py启动时load_selected |
| 最终测试为何只能执行一次 | evaluation.py冻结依赖、版本和候选，evaluate_final在推断前O_EXCL保留尝试记录 |
| 迟到的预览响应为何不能导入旧内容 | frontend/src/importGuard.js及App.vue的importChanged与guardedPreview |
| 公网如何避免匿名修改 | app_v2.py的writable及SMS_LAB_PUBLIC模式；公开包没有工作库 |

## 本人需要完成的三个练习

1. 用一次性工作空间导入一条自己编写的英文短信，解释“导入成功”“审核通过”“进入快照”三个时点；保存原标签，核对没有制造纠错历史。
2. 打开同一条待审样本的两个页面，先确认一个，再用旧revision提交另一个，核对409并说明为什么要重新读取。
3. 在隔离副本里改一个验证阈值或审核条件，比较实际FP/FN或接口行为，并指出它不能反过来修改已经封存并评估的最终方案。

实验必须采用新run或一次性副本，不覆盖现有权重和报告。最终测试已执行后不再用它选择模型；本人修改练习可使用原验证集或新的隔离样例，并明确用途。
