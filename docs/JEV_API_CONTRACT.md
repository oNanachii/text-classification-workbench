# Jev 单条判断与并排对照契约

2026-10-29，独立于既有 v1/v2 训练与最终测试。官方来源：[Choice](https://docs.typesafe.ai/primitives/choice)、[Models](https://docs.typesafe.ai/models)、[Confidence](https://docs.typesafe.ai/confidence)。本页定义本地实现契约，不表示已调用真实付费服务或已部署。

## 同一任务

固定 `english-sms-ham-spam-v1`，英文单条短信，标签仅 `ham`（正常个人、事务或明确订阅消息）与 `spam`（未经请求的推广、奖品诱导、诈骗）。模糊结果供人工复核。当前本地模型仅有旧英文短信范围的证据；本功能不扩展为中文、留言或新标签模型。对照使用完全相同的输入文本与标签，模型建议不会进入审核标签、训练库或快照。

## 请求

`POST /api/decision`，同源 JSON，最大 16 KiB；正文：

```json
{"text":"Please bring the blue notebook tomorrow.","engine":"compare","credential_source":"byok","external_consent":true}
```

- `text`：非空且最多 2000 字符；`engine`：`local`、`jev`、`compare`。
- `credential_source`：`byok`（默认）或 `server`。BYOK 在 `X-TypeSafe-Key` 请求头；不在 JSON、URL、模型输入或返回中。密钥仅保留当前页面内存与该请求调用期间，不保存于浏览器存储、数据库或日志。清除、编辑输入/凭据或离开面板后拒绝迟到响应。
- 选择 Jev 或对照必须 `external_consent: true`，页面明确告知文本由工作台转发至 TypeSafe，可能消耗该 Key 额度。每次提交至多一次上游请求；不自动重试，网络失败或超时不等于未计费。
- 浏览器在发送 Key 前即阻止不安全连接，后端独立复核；公网请求必须 HTTPS；本机非 public 模式仅允许 loopback HTTP 开发。HTTPS 判定使用服务器可信协议信息，不直接信任客户端 `X-Forwarded-Proto`；代理只信任回环代理地址。浏览器必须同源。
- `server` 仅允许非 public 且已认证的操作者显式选择，同时需配置 `SMS_LAB_JEV_SERVER_KEY_ENABLED=true` 与 `TYPESAFE_API_KEY`。public 永不使用服务器 Key，BYOK 缺失/失效也不回退。
- `SMS_LAB_JEV_ENABLED` 默认 `false`；开启只启用能力，不产生付费调用。

## 返回

顶层 `contract_version`、`request_id`、`task`（ID、语言与标签定义）、`engine`、`local`、`jev`、`disagreement`、`elapsed_ms`、`human_review_required`。未请求的引擎为 `null`；各引擎独立返回 `status`、`error_code`（如有）、`model`、`model_version`、`label`、`probabilities`、`probability_kind`、`confidence`、`confidence_kind`、`elapsed_ms`、`usage`、`outcome_unknown`。不回传文本或任意上游错误内容。

- local 的 `probabilities` 是 `spam_score` 及其补数，`probability_kind=uncalibrated_model_score`；保留阈值、训练快照和数据版本，`confidence=null`。
- Jev 只请求官方 `https://api.typesafe.ai/v1/systemone`，固定 `model=jev-1.14.0`，单个固定 Choice 问题 `sms_class`。校验响应实际版本、标签集合、有限数值、概率和、最大概率标签及 confidence 与分布的一致性。`probability_kind=provider_reported_not_locally_calibrated`、`confidence_kind=choice_distribution_concentration`。confidence 是概率分布集中程度，不是这条短信判断正确率。usage 仅保留非负 input/output token 数，缺失为 null。
- 两边成功才计算 `disagreement`；错误、未知、未运行时为 null。`human_review_required=true` 表示这只是供人判断的建议，不能作为自动标注授权。
- 请求格式、授权、同意、TLS、能力开关、速率/容量失败使用 4xx/503；进入判断后的外部失败使用成功 HTTP 返回的独立引擎错误状态，因此比较中的本地结果仍可见。错误字段为白名单常量。

## 边界与验收

每进程每来源最多 6 次/分钟、总计 30 次/分钟、最多 2 个并发；限流表最多 1024 来源。生产多进程需网关共享配额。上游总截止 15 秒、读取 10 秒、连接 3 秒，最多读取解压后 16 KiB；禁重定向、隐式代理与自动重试。401/403、429、5xx、非 JSON、错误版本/标签/概率均脱敏处理。客户端撤销只丢弃结果，不能保证已发上游的调用停止或未计费。

`GET /api/config` 新增 `decision`：enabled、task、model_version、server_key_available、limits。默认 `local`，旧 `/api/predict` 与所有审核、导入、服务选择权限保持。

明确自编烟测位于 `examples/jev_authored_sms.json`，不属于正式训练或测试证据。`scripts/verify_jev.py` 默认 dry-run，不读环境 Key、不联网、不读旧数据；只有 `--live` 才提示输入 Key 并显式外发自编样例。输出只包含样例 ID、版本、状态、标签、耗时/用量，无 Key 或短信原文。离线 mock 覆盖失败和并发；真实质量与成本仍待用户主动 live 验证。

新的效果评估须另立数据版本，预先固定任务、来源/时间/近重复隔离、人工标注和分歧处理，开发集用于选择配置，独立新测试集冻结后一次评估。现有 757 条已开封最终测试及其冻结五文件、权重、阈值和报告保持，不用于 Jev 调参或重跑。
