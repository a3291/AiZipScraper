[English](README.md) | 简体中文

# AiZipScraper

面向压缩包与文件的 AI 刮削器——识别一个包**是什么、里面有什么、干什么用**，并把结果作为侧车 JSON 锚定在文件旁边。

灵感来自媒体库刮削器（如 Plex）：每个包在文件旁留有一份可检索、可核验的档案。交互形态上，使用一次性纯文本类 agent 封装 API 调用，AI 的最终结果通过显式 JSON 契约接收。

## 特性

- **抽样提取，原件保留**：可插拔的提取器对 zip/7z 抽样解包（普通文件原样复制）到每目标的独立目录——只取白名单文本与值得注意文件名的成员，受单文件（256KB）、累计（4MB）、文件数（8）上限约束；不修改原文件
- **密码轮询**：密码由提取器自己的 `password.json` 持有；加密包自动尝试——密码不写入命令行参数与产物文件
- **分页 AI 识别**：提取内容打包成句对齐的分页，带目录页与元数据尾页；AI 通过 JSON 契约翻页（`read_page` / `publish`）
- **上下文护栏**：`max_turns` 轮上限（负数 = 无限）、临限提醒、令牌顶格强制发布、非法 JSON 容忍、publish 格式重输（3 次提醒）、翻页停滞检测——失败路径降级为带标记的 `unknown`
- **侧车锚定**：结果落在目标旁的 `<名称>.publish.json`，以 SHA256 为主锚——重扫时已发布目标自动跳过；`check` 检测哈希漂移、低置信与孤儿侧车
- **批量友好**：并发提取池与识别池隔一道栅栏，单目标失败隔离，`runs/` 下全量 run 归档，支持 JSONL 导出

## 生产消费链

六环加一层静态契约层。以文件为界的交接：目标 → extracted/ + _result.json → 侧车；环 3–5 在 `cli.py` 进程内运行，内存传 dict——runs/ 下的 JSON 文件（context.json、messages.json、checklist.json）是随之写入的归档。`cli.py` 依次调用各环模块，环脚本之间互不 import。

```
静态契约层              jsons/scraper.json · prompt.json · publish.json

main.py ──> cli.py
  环 1  find_targets        → 目标清单（全部文件；排除 runs/ 与 *.publish.json；
                              entry_id = sha256[:8]；已有侧车的目标记 skipped）
  环 2  extract_one ── 子进程 ──> run_extractor.py ── 加载 ──> extractors/<name>/
          │                 → runs/<id>/extracted/<entry_id>/ + _result.json
          │                 （提取器读自己的 config.json 与 password.json；
          │                  心跳文件起手写、成功删）
  环 3  context_builder     只读 extracted/（跳过下划线前缀文件）
          │                 → context dict；归档进 runs/<id>/context.json
  环 4  ai_identify         读环 3 的 context dict + prompt.json + scraper.json
          │   └ backend.py  → runs/<id>/messages.json（逐轮原子追加）
          │                   + identity dict
  环 5  publisher           读程序侧字段 + identity + publish.json 模板
          │                 → <名称>.publish.json 侧车（校验不过不写盘）
  环 6  run_logger          读 runs/<id>/{checklist,context,messages}.json + 侧车
                            → run 报告；scan 过程中不打印，结束时报告一次
```

环 1–2 以工作池运行，池 join 后环 3–5 逐目标执行、内存传 dict；环 6 只读。`extractors/` 不 import 项目模块、不读 `jsons/`；提取器目录整体插拔。

## 安装

需要 [uv](https://docs.astral.sh/uv/) 与 Python 3.12+。

```bash
git clone https://github.com/a3291/AiZipScraper.git
cd AiZipScraper
uv sync
```

## 快速上手

```bash
# 推荐的 scan 用法：配置取自 jsons/scraper.json + chatlog 滚页 + 发布后清理，单线程，默认提取器，强制全量重刮
uv run python main.py scan D:/downloads --config jsons/scraper.json --auto-chatlog --auto-extracted-clean --workers 1 --extractor default --force

# 批量刮削一个目录（已缓存目标自动跳过）
uv run python main.py scan D:/downloads

# 仅本次覆盖并发数（默认取 scraper.json 的 "concurrency"）
uv run python main.py scan D:/downloads --workers 8

# 换用另一个提取器（extractors/ 下的目录名；默认 default）
uv run python main.py scan D:/downloads --extractor my_extractor

# 强制全量重刮
uv run python main.py scan D:/downloads --force

# 目标发布成功后立即删除其提取产物（省磁盘；失败目标保留现场）
uv run python main.py scan D:/downloads --auto-extracted-clean

# AI 会话历史超长时折叠为归档 JSON 总结（语义见 API 交互节）
uv run python main.py scan D:/downloads --auto-chatlog

# 查看侧车
uv run python main.py show D:/downloads

# 健康检查：未刮削 / 哈希不符 / 低置信 / 孤儿侧车
uv run python main.py check D:/downloads

# 导出 JSONL 摘要，供表格或检索使用
uv run python main.py export D:/downloads -o summary.jsonl

# 独立回放某个 run 的报告
uv run python scripts/run_logger.py <run_id>
```

## 配置

`jsons/scraper.json` 是唯一的配置文件；缺键或类型不符直接报错：

```json
{
  "concurrency": 4,
  "ai": {
    "base_url": "http://localhost:1234/v1",
    "model": "",
    "api_key": "lm-studio",
    "temperature": 0.2,
    "timeout": 300,
    "page_chars": 3000,
    "remind_at": 32000,
    "force_publish_at": 60000,
    "max_turns": -1
  },
  "limits": {
    "extract_timeout_s": 1800,
    "max_text_file_bytes": 33554432,
    "sentence_max_ratio": 0.1
  }
}
```

| 键 | 含义 |
|-----|---------|
| `concurrency` | 一个数值同时管提取池与识别池（两池不重叠；`--workers` 仅本次覆盖） |
| `ai.base_url` | 留空 → 自动探测：LM Studio 原生 `/api/v1/chat` → LM Studio `/v1/chat/completions` → Ollama `/v1/chat/completions`（先做可达性检查再发最小会话，第一个通过者胜出）；非空则按原样使用。请求形态跟随 URL：以 `/chat` 结尾的完整路径发原生 `{model, input}` 请求体；`/chat/completions` 或裸基座（如 `/v1`）发 messages 数组 |
| `ai.model` | 留空则自动取后端第一个已加载模型 |
| `ai.page_chars` | 目标页大小（字符数） |
| `ai.remind_at` / `ai.force_publish_at` | 估算令牌阈值：先提醒 AI，随后强制发布 |
| `ai.max_turns` | 会话轮上限；负数（默认 `-1`）= 无限——令牌顶格、停滞检测与非法 JSON 容忍仍会使会话终止 |
| `limits.extract_timeout_s` | 单目标提取的时间盒 |
| `limits.max_text_file_bytes` | 超过此大小的文件只登记、不作为文本读取 |
| `limits.sentence_max_ratio` | 超过 `page_chars × ratio` 的句子整句跳过；分页对齐句子末尾，句子不跨页切断 |

提示词在 `jsons/prompt.json`；侧车模板在 `jsons/publish.json`。临时换配置用 `--config other.json`。

## API 交互

`backend.py` 在每个识别会话的每一轮与 AI 服务器通信一次。

**端点解析**（每次 run 一次，首个会话开始前）：

- `ai.base_url` 非空 → 按原样使用
- `ai.base_url` 为空 → 按序探测候选：LM Studio 原生
  `http://localhost:1234/api/v1/chat` → LM Studio
  `http://localhost:1234/v1/chat/completions` → Ollama
  `http://localhost:11434/v1/chat/completions`；每个候选先做可达性 POST
  （收到任何 HTTP 响应即算可达），再发一次最小会话（必须能取回文本），
  第一个通过者胜出——端点、请求形态与自动选中的模型记录后供整个 run 使用

**请求形态跟随 URL：**

- 以 `/chat` 结尾的完整路径（LM Studio 原生）→ `{"model", "input": [{"type": "text", "content": …}]}`；整个会话（system、首条提示、已交付页面、AI 回复）渲染成一条带 `[role]` 标签的文本
- 其余情况（OpenAI 兼容）→ `{"model", "messages": […], "temperature", "stream": false, "response_format": {"type": "json_schema", "json_schema": …}}`，以发布契约作为 schema

`temperature` 仅在 OpenAI 兼容模式下生效；原生请求体只带 `model` 与 `input`。

**响应**：OpenAI 的 `choices[0].message.content` 与原生的 `output[]` 消息列表两种形状都归一化为同一提取文本；`usage` 计数在存在时保留，供令牌护栏使用。

**会话契约**：模型每轮返回一个 JSON 对象——`{"action": "read_page", "page": N}` 或 `{"action": "publish", "identity": {…}}`。护栏：非法 JSON 容忍一次；publish 的 identity 非对象或缺 `title`/`category`/`summary` 键给 3 次提醒重输（首次 publish 不计，用尽放弃）；重复页/不存在页的停滞转入强制发布；估算令牌达到 `remind_at` 提醒、达到 `force_publish_at` 强制（强制后再给两轮翻页机会，然后放弃）；`max_turns` 为负数时不设轮上限。放弃路径以带标记的 `unknown` 侧车收尾。

**Chatlog 模式**（`--auto-chatlog`）：历史超过水位线时折叠进与页面 context 平行的上下文通道——`remind_at` 触发总结侧调用，JSON 摘要与已读进度并入重发开卷 prompt 的末尾页段位（对话通道不出现摘要消息；软边界：`messages.json` 逐条保留全部原始消息）；无有效摘要时 `force_publish_at` 直接强制滚页；`max_turns` 换义为滚动次数上限（负数 = 不限），达到后两条水位线恢复上述原意。

## 侧车格式

每个目标旁生成 `<名称>.publish.json`，写盘前经 `schema.py` 校验，校验不过则不写文件：

```json
{
  "anchoring":  { "sha256": "…", "source_path": "…", "file_size": 0, "mtime": "…" },
  "scrape":     { "scraped_at": "…", "engine": "…", "depth": "full", "confidence": 0.86 },
  "identity":   { "title": "…", "category": "dataset", "summary": "…", "tags": [], "language": [] },
  "structure":  { "entry_count": 0, "dir_count": 0, "total_uncompressed": 0,
                  "top_extensions": {}, "top_level_dirs": [], "notable_files": [] },
  "flags":      { "password_protected": false, "multi_part": false, "nested_archives": [],
                  "exe_present": false, "macro_docs": false },
  "warnings":   []
}
```

- `anchoring.sha256` 是主锚：`check` 会对目标重算哈希以发现内容漂移
- `flags.password_protected` 只表示加密——可用的密码不持久化
- `warnings` 包含提取器自己的备注，原样透传

## 项目结构

```
├── main.py                        # 统一入口（转交 scripts/cli.py）
├── scripts/                       # 流水线各环（互不 import）
│   ├── cli.py                     # scan / show / check / export
│   ├── run_extractor.py           # 提取器运行器（子进程入口，契约校验）
│   ├── context_builder.py         # extracted/ → 分页上下文
│   ├── ai_identify.py             # 识别会话引擎
│   ├── backend.py                 # AI 服务器/API 连接（端点自动识别）
│   ├── publisher.py               # 模板填充 → 校验 → 侧车
│   ├── run_logger.py              # run 报告汇总器（纯读盘）
│   ├── schema.py                  # 侧车契约（大类枚举、阈值、校验）
│   └── paths.py                   # 项目路径常量
├── extractors/                    # 提取器目录（可插拔、自包含）
│   └── default/                   # extractor.py + config.json + 自持 password.json
├── jsons/                         # 静态契约层
└── runs/                          # 每次 scan 的归档（不入库）
```

## 安全边界

- 成员路径规范化，`..`/绝对路径成员被跳过；抽样提取受单文件（256KB）、累计（4MB）、文件数（8）与条目清单（3 万，截断）上限约束
- 不执行成员文件、不解析宏；仅读取文本
- AI 通过 JSON 契约翻页与发布 identity；哈希、结构、锚定等程序侧字段不发给模型
- 非法输出降级为带标记的 `unknown` 侧车
