# 外部短信数据来源与下载

本次选择 Sandhya Mishra、Devpriya Soni 发布的 **SMS PHISHING DATASET FOR MACHINE LEARNING AND PATTERN RECOGNITION**，Mendeley Data V1，发布于 2022-06-20，DOI `10.17632/f45bkkt8pr.1`。

- [固定版本与作者说明](https://data.mendeley.com/datasets/f45bkkt8pr/1)
- [官方许可元数据](https://data.mendeley.com/public-api/datasets/f45bkkt8pr)
- [固定版本文件清单](https://data.mendeley.com/public-api/datasets/f45bkkt8pr/files?folder_id=root&version=1)
- [CC BY 4.0 许可](https://creativecommons.org/licenses/by/4.0/)
- [原论文与正式引文](https://link.springer.com/chapter/10.1007/978-3-031-27524-1_57)

提供方明确标注 CC BY 4.0。该许可允许研究、复制、改编及分享，包括发布汇总结果；须署名、链接许可证、说明处理改动，且不能暗示原作者为本项目背书。本仓库保留上述作者、来源及许可信息；本次改动为统一标签、规范化、去重及按冻结规则过滤，再发布 ID 与汇总数值。源码 MIT 许可不替代数据许可。

原作者说明数据由互联网文本和截图经 OCR 整理、按来源标注，并致谢 Almeida、Sonowal；原始 5971 条含大量 UCI 来源内容，不能把整份数据直接当作与 UCI 独立的新测试。实验先按 UCI 训练／验证数据过滤，再仅用旧划分 ID 与模板摘要隔离历史测试重复；不打开旧测试短信。源码只读取 LABEL 和 TEXT，URL、EMAIL、PHONE 属性不作模型特征或标签提示。语言字符审计不是逐条人工语言复核，公开数据是否进入大模型预训练也未知。

## 固定下载与校验

[官方原始 ZIP 下载](https://data.mendeley.com/public-files/datasets/f45bkkt8pr/files/edb361de-918d-469f-9106-e84823830665/file_downloaded)

- 文件名：`Dataset_5971.zip`，244698 bytes。
- 文件 ID：`edb361de-918d-469f-9106-e84823830665`。
- ZIP SHA-256：`9bbf3188fdad81495d8e82825648b9b63b53fc86841a3d26c02629990b233cc3`（官方元数据与下载实测一致）。
- 唯一成员：`Dataset_5971.csv`，UTF-8（按 utf-8-sig 解码）。
- CSV SHA-256：`649844f1c62a6b27e145eaf17a65f7010c56c390e11a794ea8329993a05ba71e`。
- 列：`LABEL,TEXT,URL,EMAIL,PHONE`；原始标签含大小写变体，精确映射见冻结计划。

可从下载链接获取 ZIP，保存到自己创建的私有 `var/external-test-*/raw/`，检查上述摘要后仅读取 CSV；不运行数据包中的任何程序。`textlab.external_test --prepare` 也实现固定下载、摘要检查与单 CSV 提取，但本仓库已有冻结计划后不允许用该命令覆盖计划或开启另一轮试验。

本目录只提交此说明。原 ZIP、CSV、短信正文、含正文的规范化副本均留在被 Git 忽略的独立 `var/external-test-20261101/`；公开报告和逐 ID 证据位于 `docs/EXTERNAL_TEST.md`、`artifacts/external-test/`。离线复算不需要下载短信，也不会调用 API：

```sh
.venv/bin/python -m textlab.external_test --root artifacts/external-test --replay
```
