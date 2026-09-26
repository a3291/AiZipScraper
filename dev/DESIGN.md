# AiScraper 设计稿

## 1. 定位

AI 驱动的压缩包/文件内容刮削器：登记目标 → 提取 → 识别 → 发布。使用一次性纯文本类 agent 封装 API 调用，通过显式 JSON 契约接收 AI 的最终结果。

## 2. 目录

```
main.py                       入口
config.json                   运行配置
scripts/
  cli.py                      scan / backend
  ai_identify.py              会话引擎
  common/
    paths.py                  布局常量与 JSON 读写
    schema.py                 模板主导验证
    backend.py                AI 后端连接
    prompt_builder.py         prompts.json 加载与 {_contract:xxx} 注入
    registry.py               目标注册表
  extractor/
    run_extractor.py          工人：加载 <名>/extractor.py，回传 extract() 返回值
    context_scanner.py        文件 → 文本识别
    context_builder.py        分页打包
extractors/
  _contract.json              契约
  default/
    extractor.py
    config.json
    password.json
    prompts.json
    publish.json
runs/<run_id>/
  registry.json
  run.json
  context.json
  chatlog.json
  memo.json
  sessions.json
  messages.json
```

依赖方向：顶层 → 子域；extractor → common；common 与 extractor 互不引用；不建 `__init__.py`。

## 3. config.json

- `concurrency`：并行 worker 数。
- `ai`：`base_url`（必填非空，留空报错）、`model`、`api_key`、`temperature`、`timeout`、`page_chars`、`remind_at`、`force_publish_at`、`max_turns`（-1 = 不限）、`estimate_chunk`、`publish_retries`（固定 3）。
- `limits`：`extract_timeout_s`、`sentence_max_ratio`。提取量不归这里管，归提取器自己的 config。
- 缺键报错。

## 4. 契约

`extractors/_contract.json`，两份：

```json
{
  "session_actions": {
    "template": {"action": "", "page": null, "memo": null, "identity": null},
    "enums": {"action": ["read_page", "read_chatlog", "read_memo", "write_memo", "publish", "help"]}
  },
  "chatlog_summary": {
    "template": {"summary": ""}
  }
}
```

template 语义：非空值 = 强制值（字面一致）；空串/空表/0 = 类型槽；null = 任意值；enums = 取值枚举。

同一份 template 推导 response_format 发给后端，本地 schema.check 用同一份校验。

提示词中 `{_contract:名称}` 注入契约内容。（渲染文字待定）

## 5. prompts.json

条目：`{role, frontier, text}`。frontier 固定不动（契约功能、协议标签、token 结构，如 `Page {page} of {page_total}:`）；text 是可变注册内容（含 system 的角色定义）。发送时 frontier 在前，非空段以空行拼接。

场景：system、first、page_deliver、chatlog、chatlog_deliver、memo、memo_deliver、remind、force_publish、bad_json_retry、publish_retry、stall_to_publish、help、chatlog_summarize、chatlog_summarize_reply。

待定：memo_saved、memo_reject、add 三条是否保留；全部条目的 frontier/text 文字。

## 6. scan 管道

一条龙，非递归：

1. 登记：路径第一层为目标，runs/ 排除。registry.json 是目标状态的唯一事实。状态机 pending → extracted → published / failed / skipped。（排除规则与键名待定）
2. 提取：每目标一个子进程跑 `extractors/<名>/extractor.py` 的 `extract(in_path, out_dir)`，通过返回值判断是否正常。不正常（异常/超时/进程死亡/非 dict/零保留）一律 skipped，不进会话。运行记录落 runs/<run_id>/run.json。（字段形状待定）
3. 上下文：context_scanner 识别文本，context_builder 分页打包，归档 context.json。内容页 1..N，元数据页为尾页。（尾页版式、catalog 格式、分页标点集待定）
4. 识别：每目标一场会话（第 7 节）。
5. 发布：publish.json 模板直填、自检、写 `<目标>.publish.json` 于目标旁。模板检查不过不写文件。（检查不过记什么状态待定）

backend 子命令：端点连通自检。（去留待定）

## 7. 会话引擎

chatlog 唯一模式。

- 开卷顺序：system → context（首页位）→ chatlog（尾页位）→ memo（尾页位）。（add 槽待定）
- 归档：messages.json 逐条记录，每条带 session 号；sessions.json 记 session 边界；每次折页开新 session。（prompt_key 标注待定）
- 水位线：`remind_at` 触发总结侧呼叫（chatlog_summary 契约），总结成功则折页入摘要；`force_publish_at` 直接折页，本 session 原始消息折入。`max_turns` 是唯一折页上限，-1 不限；到顶后两条水位线恢复提醒发布/强制发布本义。
- 折页产物：chatlog 是与 context 同构的分页文档，持久化 runs/<run_id>/chatlog.json，模型用 read_chatlog 逐页读。（chatlog 页可否重复读待定）
- 每轮一个 JSON 对象，action 六选一：
  - read_page：读内容页或尾页。重复页、越界页即发 stall_to_publish 进入强制发布。
  - read_chatlog：读 chatlog 页。
  - read_memo / write_memo：memo 持久化 runs/<run_id>/memo.json，按目标分包。（写失败反馈与大小上限待定）
  - publish：identity 过模板检查后收下。格式错误时 publish_retries=3 次重新输入机会，仅强制发布后启用；未强制时格式错误直接放弃。（放弃后形态待定）
  - help：回协议复述卡，随时可调，无上限。
- 坏 JSON 回复：每次回 bad_json_retry 提示，无上限，不放弃。

## 8. 提取器 default

- 只收 zip/7z；文件夹拒绝，非压缩包文件拒绝，拒绝传导为 skipped。
- 提取量由提取器决定，上限写提取器自己的 config.json。
- 加密包用 password.json 密码候选。
- （成员筛选规则、caps 数值待定）

## 9. 发布文档

`<目标>.publish.json` = 模板直填。identity 块 + warnings 块（程序填）。（identity 键集取值口径、warnings 合并方式待定）

## 10. 已删除

publisher.py、run_logger.py、show/check/export、心跳、_result.json、sha256、占位符机制、publish_sidecar/publish_identity/extract_result/heartbeat 契约、max_text_file_bytes、roll_hard_cap/forced_retries/bad_json_tolerance、退出码判定、dup_page。版本号不进生产内容。

## 11. 待定

1. chatlog_summary 长度上限
2. 契约渲染文字
3. prompts.json 全部条目文字；memo_saved/memo_reject/add 去留
4. 登记排除规则；目标键名；out_dir 布局
5. run.json 字段形状
6. 尾页元数据版式；catalog 格式；分页标点集与超长句规则
7. chatlog 页可否重复读
8. memo 写失败反馈；memo 大小上限
9. publish 放弃后的形态
10. 模板检查失败时目标状态
11. backend 子命令去留
12. 提取器成员筛选规则；caps 数值
13. identity 键集与取值口径
14. warnings 合并方式
