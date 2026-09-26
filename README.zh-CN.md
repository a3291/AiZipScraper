[English](README.md) | 简体中文

# AiZipScraper

AI 驱动的压缩包/文件内容刮削器：登记目标，用可插拔提取器取文本，让本地大模型通过分页对话识别每个目标，并在目标旁写一份 `publish.json`。

灵感来自媒体库刮削器（如 Plex）：每个包在文件旁留有一份可检索、可核验的档案。

本项目使用了大语言模型开发，所以有些混乱，已经尽力修正，本人代码并不是精湛，目前始终处于开发一个小工具的目的。

## 布局

```
main.py                       入口
config.json                   运行配置
scripts/
  cli.py                      scan / backend 命令
  ai_identify.py              会话引擎（chatlog 唯一模式、分 session 归档）
  paths.py                    布局常量与 JSON 读写
  schema.py                   模板主导验证
  backend.py                  AI 后端访问
  prompt_builder.py           prompts.json 加载、{_contract:xxx} 注入
  registry.py                 目标注册表（目标状态的唯一事实）
  run_extractor.py            工人：加载 <名>/extractor.py，回传 extract() 返回值
  context_scanner.py          文件 → 文本识别
  context_builder.py          分页打包（目录首页、句对齐内容页、元数据尾页）
extractors/
  _contract.json              session_actions + chatlog_summary 两份契约
  default/
    extractor.py              zip/7z 抽样提取器
    config.json               提取器上限（单文件/总量字节、文件数、清单条数）
    password.json             加密压缩包的密码候选
    prompts.json              会话提示词注册表（{role, frontier, text}）
    publish.json              publish 文档模板
runs/<run_id>/
  registry.json               目标状态
  run.json                    提取子进程运行记录
  context.json                每目标的分页上下文
  chatlog.json                每目标滚页而成的 chatlog 文档
  memo.json                   每目标的只追加分页 memo 文档
  messages.json               逐条原始消息，带 session 号
  publishes/                  每份 <目标>.publish.json 的完整拷贝
```

## 安装

需要 Python >= 3.12。本工具从仓库目录直接运行——没有包安装这一步；
依赖只有 `py7zr` 和 `pyzipper`。

用 [uv](https://docs.astral.sh/uv/)（仓库自带 `uv.lock`）：

```
uv venv
uv pip install py7zr pyzipper
```

或在任意已激活的 Python >= 3.12 环境里用 pip：

```
pip install py7zr pyzipper
```

## 快速开始

```
.venv\Scripts\activate          # Windows（POSIX 用 source .venv/bin/activate）
python main.py backend
python main.py scan D:\downloads --workers 4
```

推荐：`python main.py scan D:\downloads --extractor default --workers 4`
（scan 不递归；每目标一场会话；在每个目标旁写 `<目标名>.publish.json`）。

## 配置（config.json）

| 键 | 默认值 | 含义 |
|---|---|---|
| concurrency | 4 | 提取池与识别池的并行 worker 数 |
| ai.base_url | http://localhost:1234/v1 | OpenAI 兼容端点（必填） |
| ai.model | — | 模型名；必填非空 |
| ai.api_key | lm-studio | 端点需要时带的 bearer token |
| ai.temperature | 0.7 | 采样温度 |
| ai.timeout | 300 | 单次请求超时秒数 |
| ai.probe_timeout | 10 | 端点探测超时秒数（backend 校验） |
| ai.page_chars | 3000 | 每页字符数 |
| ai.remind_at | 32000 | 水位线：请求总结并滚页（到顶则提醒发布） |
| ai.force_publish_at | 60000 | 水位线：滚入原始消息（到顶则强制发布） |
| ai.max_turns | -1 | 滚页上限；-1 不限 |
| ai.estimate_chunk | 4 | 后端不回报用量时按"字符÷该值"估算 token |
| ai.publish_retries | 3 | 强制后格式错误 publish 的重新输入机会数 |
| limits.extract_timeout_s | 1800 | 提取子进程超时 |
| limits.sentence_max_ratio | 0.1 | 句长上限占 page_chars 比例（超限整句丢弃） |
| limits.sniff_bytes | 8192 | 二进制嗅探头部字节数（头部像二进制的文件不可作文本） |

## 管道（scan）

1. **登记** — 路径下第一层（跳过隐藏项、`_` 开头、`runs/`、已存在的
   `*.publish.json`）；每个目标在 `registry.json` 里成为 `t1..tN`，状态
   留空，处理到哪步填哪步。
2. **提取** — `extractors/<名>/extractor.py` 在子进程中按目标运行；
   `extract(in_path, out_dir)` 的返回值判定正常与否。提取器名不得以 `_`
   开头或含路径分隔符。不正常（异常、超时、
   进程死亡、返回非 dict、零保留文件）把目标记为 `error` 并带原因，
   不进会话。每次运行记入 `run.json`：`ok`、返回的 `result` 或 `error`
   原因、耗时。
3. **上下文** — 目标 `extracted/<tN>/` 下的文件经 `context_scanner` 识别，
   打成分页：目录首页、内容页、元数据尾页，归档为 `context.json`。
   `_` 开头的名字跳过。
4. **识别** — 每目标一场会话。开卷场景：system、context（首页：目录）、
   可选 `add`、chatlog（尾页）、memo。每条原始消息带 session 号
   归档进 `messages.json`——session 划分就记在这里；总结折入后开新 session。
5. **发布** — 用模型产出的 identity 填模板 `publish.json`——publish 是纯
   内容结论，不带任何域的过程注记（提取记录留 `run.json`，会话注记走
   控制台）——自检通过后写为 `<目标名>.publish.json`，完整拷贝存入
   `runs/<run_id>/publishes/`。
   目标终态仅 `ok`（正常）或 `error`（出错）；任一阶段出错即跳过该目标
   后续阶段。

## 会话引擎

模型每轮用一个 JSON action 驱动（`{_contract:session_actions}`）：

- `read_page` — 请求上下文页（第 1 页是目录；元数据页是最后一页）。
  已读页或越界请求停滞进入发布。
- `read_chatlog` — 请求 chatlog 页；chatlog 页可重复读。
- `read_memo` / `write_memo` — memo 是只允许追加的分页文档，与 chatlog 同构：
  `write_memo` 把 `memo` 字段追加为新段落，`read_memo` 按页号请求。
  笔记按目标存于 `runs/<run_id>/memo.json`，跨 session 与滚页保留，
  开卷默认展示尾页；非字符串写入以 `memo_reject` 拒绝。
- `publish` — 最终 identity。未进入强制状态时格式错误的 publish 直接放弃
  （目标记 `error`，不写文件）；强制后给 `publish_retries` 次重新输入机会。
- `help` — 协议复述，随时可调。

滚页：到 `remind_at` 引擎向模型侧呼叫（`{_contract:chatlog_summary}`）
要总结，过 `chatlog_summary` 契约检查后滚入 chatlog 文档；到
`force_publish_at` 不做总结，本 session 原始消息滚入（软边界——消息在
chatlog 文档里保留）。每次滚页开新 session。`max_turns: -1` 不限次滚页；
到顶后水位线回落为提醒发布 /
强制发布的原意。

提示词条目为 `{role, frontier, text}`：frontier 是固定骨架（契约引用、
协议标签、页码 token），text 是可变注册内容（角色定义、载荷槽位）。
契约放 `extractors/_contract.json`，经 `{_contract:<名称>}` 注入。

## 发布格式

`<目标名>.publish.json` 对应模板：identity（title、category、summary、
tags、language、confidence），仅此而已。每份已发布文档同时
拷贝到 `runs/<run_id>/publishes/`。发布过不了模板检查的
目标记 `error`，不写文件。

## default 提取器

只收 `.zip` / `.7z` 压缩包；文件夹与普通文件拒绝（其目标记 `error`，
不进会话）。成员按自有上限抽样（`extractors/default/config.json`）：
单文件 256 KiB、总量 4 MiB、8 个文件、清单 3 万条。加密压缩包尝试
`extractors/default/password.json` 的密码候选。量的策略归提取器，
不归根配置。
