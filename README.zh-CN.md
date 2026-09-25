# AiZipScraper

[English](README.md)

压缩包 AI 刮削识别器——对 zip / 7z 压缩包做内容识别，生成结构化的元数据摘要，并以侧车 JSON 的形式锚定在文件旁边。

灵感来自媒体库刮削器（如 Plex）：不让一个来路不明的压缩包变成命名黑洞，让每个包都拥有"这是什么、里面有什么、用来干什么"的可检索档案。

## 特性

- **两档识别深度**
  - `listing`：零解压，仅读压缩包目录区（绝对安全，快速预览）
  - `listing+sample`（默认）：沙箱内白名单抽样解压文本类文件，为 AI 提供真实内容证据
- **AI 结构化识别**：默认对接 [LM Studio](https://lmstudio.ai/)，兼容任意 OpenAI 协议后端（Ollama `/v1`、vLLM、llama.cpp server…）；输出经 JSON Schema 强约束，标题 / 大类 / 总结 / 标签 / 置信度直接可用
- **密码轮询**：提供密码文件即可自动尝试加密包，命中后继续抽样；密码本身不落盘
- **侧车元数据锚定**：结果写入 `包名.zip.meta.json`，以 SHA256 为主键——重刮幂等跳过、文件改名可校验修复
- **批量友好**：串行批处理、单包失败不中断整批、缓存跳过、体检报告（孤儿侧车 / 哈希漂移 / 低置信清单）、JSONL 汇总导出
- **安全边界硬编码**：单文件 256KB / 累计 4MB / 条目 3 万上限，路径归一化校验防 zip 炸弹与路径穿越；不执行内容、不解析宏

## 安装

要求 [uv](https://docs.astral.sh/uv/) 与 Python 3.12+。

```bash
git clone <repo-url> AiZipScraper
cd AiZipScraper
uv sync
```

## 快速开始

```bash
# 批量刮削一个目录（默认抽样解压深度）
uv run python main.py scan D:/downloads --pwfile passwords.txt

# 快速预览模式（不解压）
uv run python main.py scan D:/downloads --depth listing

# 中文展示侧车内容
uv run python main.py show D:/downloads

# 体检：孤儿侧车 / 哈希不匹配 / 低置信
uv run python main.py check D:/downloads

# 汇总导出 JSONL，供表格或检索使用
uv run python main.py export D:/downloads -o summary.jsonl
```

已安装为命令行工具时，也可直接用 `uv run scraper <子命令>`。

## 配置

AI 后端通过根目录的 `scraper.json` 配置：

```json
{
  "ai": {
    "provider": "lmstudio",
    "base_url": "http://localhost:1234/v1",
    "model": "",
    "api_key": "lm-studio",
    "temperature": 0.2,
    "timeout": 300
  }
}
```

| 字段 | 说明 |
|------|------|
| `provider` | `lmstudio`（默认，`:1234/v1`）或 `ollama`（`:11434/v1`）；`base_url` 显式指定时优先 |
| `base_url` | 任意 OpenAI 兼容端点 |
| `model` | 留空自动取后端第一个已加载模型 |
| `api_key` | 本地后端通常随意填写 |
| `temperature` / `timeout` | 采样温度与请求超时（秒） |

临时换配置可用 `--config other.json`。

## 侧车格式

每个包旁生成 `<原名>.<原扩展名>.meta.json`：

```json
{
  "schema_version": "1.0",
  "anchoring":  { "sha256": "…", "source_path": "…", "file_size": 0, "mtime": "…" },
  "scrape":     { "scraped_at": "…", "engine": "lmstudio:auto", "depth": "listing+sample", "confidence": 0.86 },
  "identity":   { "title": "…", "category": "dataset", "summary": "…", "tags": [], "language": [] },
  "structure":  { "entry_count": 0, "top_extensions": {}, "top_level_dirs": [], "notable_files": [] },
  "sample_evidence": [{ "file": "README.md", "excerpt": "…前200字…" }],
  "flags":      { "password_protected": false, "exe_present": false, "macro_docs": false },
  "warnings":   []
}
```

- `anchoring.sha256` 是主锚点：文件改名/移动后凭哈希仍可认领，无需重刮
- `sample_evidence` 保留 AI 结论的原文证据摘录，便于人工抽查
- 正确密码**不会**写入侧车，仅记 `password_protected` 标记

## 安全边界

抽样解压在一次性沙箱目录中进行，且硬编码以下约束（不可配置）：

- 白名单后缀（`.txt/.md/.json/.csv/…`）及 README / 说明类文件名
- 单文件 256KB、累计 4MB、条目 3 万上限
- 成员路径归一化校验，拒绝绝对路径与 `..` 穿越
- 不执行任何内容、不解析宏、不读二进制

## 项目结构

```
├── main.py          # 统一入口
├── cli.py           # scan / show / check / export 四个子命令
├── extractor.py     # 信号采集：清单 + 沙箱抽样 + 密码轮询
├── ai_identify.py   # OpenAI 兼容 AI 识别层
├── sidecar.py       # 侧车读写 / 幂等 / 改名校验 / 孤儿检测
├── schema.py        # 侧车契约（v1.0）与校验
├── scraper.json     # AI 后端配置
└── dev/             # 开发资料与测试夹具（不入库）
```

## 许可证

见 [LICENSE.txt](LICENSE.txt)。
