[English](README.md) | 简体中文

# AiScraper

AI 驱动的压缩包/文件内容刮削器：登记目标，用可插拔提取器取文本，让本地大模型通过分页对话识别每个目标，并在目标旁写一份 `publish.json`。

## 布局

```
main.py                       入口
config.json                   运行配置
scripts/
  cli.py                      scan / backend 命令
  registry.py                 目标注册表（目标状态的唯一事实）
  backend.py                  AI 后端连接
  prompt_builder.py           prompts.json 加载、{_contract:xxx} 注入、场景组装
  context_scanner.py          文件 → 文本识别
  context_builder.py          分页打包（句对齐分页、元数据尾页）
  ai_identify.py              会话引擎（chatlog 唯一模式、分 session 归档）
  schema.py                   模板主导验证
  run_extractor.py            提取子进程（退出码即成败）
extractors/
  _contract.json              session_actions + chatlog_summary 两份契约
  default/
    extractor.py              zip/7z 抽样提取器（自包含）
    config.json               提取上限与白名单
    password.json             压缩包密码候选
    prompts.json              提示词注册表：{role, frontier, text}
    publish.json              publish 文档模板
runs/<run_id>/
  registry.json               目标与状态
  run.json                    提取子进程记录（退出码、stderr、耗时）
  context.json                每目标的分页上下文
  sessions.json               每目标的 session 边界与滚页数
  messages.json               全部原始消息，逐条带 session 号
```

## 用法

```
python main.py backend                       校验后端连通
python main.py scan <path>                   一条龙：登记 → 提取 → 识别 → 发布
python main.py scan <path> --extractor default --workers 4
```

`scan` 只登记 `<path>` 第一层下的文件与文件夹（不递归；隐藏项、`_` 开头项、`runs/`、已存在的 `*.publish.json` 跳过），子进程池并发提取，逐目标跑识别会话，在每个目标旁写 `<目标名>.publish.json`。提取成败只看退出码；每次运行的 stdout、stderr、退出码记入 `run.json`。

## 配置（config.json）

| 键 | 含义 |
|---|---|
| `concurrency` | `scan` 并发数 |
| `ai.base_url` | OpenAI 兼容端点（必填非空） |
| `ai.model` | 模型名；留空取端点列出的第一个模型 |
| `ai.temperature` / `ai.timeout` | 采样温度与 HTTP 超时 |
| `ai.page_chars` | 每内容页字符数 |
| `ai.remind_at` | 令牌水位：要求模型总结并折页 |
| `ai.force_publish_at` | 令牌水位：不做总结直接折页 |
| `ai.max_turns` | 折页上限；`-1` 不限；达上限后两水位回落为提醒发布/强制发布 |
| `limits.extract_timeout_s` | 单目标提取子进程超时 |
| `limits.max_text_file_bytes` | 打包进页面的单文件上限 |
| `limits.sentence_max_ratio` | 超过 page_chars x 该比例的句子整句丢弃 |

## 契约

`extractors/_contract.json` 以模板形式存放两份契约：`session_actions`（模型每轮回复：`read_page` / `publish` / `help`）与 `chatlog_summary`（折页总结侧呼叫）。`schema.py` 拿文档对模板验证——模板中已填值必须字面一致，空值是必须存在且类型吻合的槽——并用同一份模板推导发给后端的 `response_format` JSON Schema。提示词条目分两段：frontier 是固定骨架（契约引用、协议标签、token 结构），text 是可变注册内容（AI 角色定义、载荷槽位）；frontier 用 `{_contract:<名>}` 引用契约，加载时注入渲染后的协议文本。

## 会话引擎

开卷场景为 `system | context | chatlog`；模型每轮必须回一个符合 `session_actions` 的 JSON 对象。`read_page` 送页，`publish` 以身份结束会话，`help` 重发协议复述。令牌达到 `remind_at` 时以侧呼叫要求模型总结会话，摘要折入 chatlog 段并开新 session；`force_publish_at` 不等总结直接折页。原始消息永不丢弃：`messages.json` 按 session 号保留每条消息，`sessions.json` 记录分段边界。

放弃路径（持续格式错误、翻页停滞）发布降级身份——category `unknown`、confidence 0——原因记入文档的 `warnings`。

## Publish 文档

`publish.json` 就是结果的形状：`identity`（标题、类别、摘要、标签、语言、置信度）由模型填写，`warnings` 由程序填写。填好的文档先对模板验证，通过才写为 `<目标名>.publish.json`。

## 扩展

把 `extractors/default/` 复制为 `extractors/<名>/`，保留含 `extract(in_path, out_dir) -> {"files_kept", "warnings"}` 函数的 `extractor.py`（不 import 项目模块；模块自己的配置/密码文件留在其目录内），按需调整它的 `prompts.json` 与 `publish.json`，然后 `scan <path> --extractor <名>`。

## 许可

Apache-2.0
