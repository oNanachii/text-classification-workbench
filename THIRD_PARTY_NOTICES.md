# 第三方与数据来源

- 原始短信数据：Almeida, T. & Hidalgo, J. (2011)，UCI SMS Spam Collection，DOI 10.24432/C5CC84。来源与原始文件摘要保留在data/source.json，原始readme保留在data/raw/readme。2026-10-28已核对[UCI当前来源页](https://archive.ics.uci.edu/dataset/228/sms+spam+collection)列出的许可为CC BY 4.0。数据许可不因本项目源码许可而改变。
- Vue、Vite及其构建依赖通过npm官方包安装，遵守各包自身许可；锁文件保留精确依赖。本项目不包含外部CDN脚本、字体或图片。
- Python/FastAPI、NumPy、scikit-learn、PyTorch及其依赖分别遵守各项目许可。公开数值推断无需Torch或pickle加载。
- examples/authored_sms.csv是AI辅助编写的烟测输入，不属于UCI原数据，也不代表外部用户标注或质量收益。
