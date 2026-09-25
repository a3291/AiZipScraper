"""ai_identify.py — 识别会话引擎。

我是什么：与 AI 后端逐轮交互的会话逻辑（连接逻辑在 backend.py）。
我的接口：run_session(context_pkg, prompts, cfg, mlog, hint_exts)
  → (identity, confidence, warnings, stats)；逐轮追加 messages.json。
护栏：max_turns、非法JSON容忍1次、同页停滞转强制、强制后仍翻页2次降级；
任何失败路径降级 unknown。
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime
from pathlib import Path

import backend
import schema
import paths
from backend import PROVIDER_DEFAULTS

CATEGORIES = schema.CATEGORIES
LOW_CONFIDENCE = schema.LOW_CONFIDENCE

JSONS = paths.JSONS

REQUIRED_AI_KEYS = ["provider", "base_url", "model", "api_key", "temperature",
                    "timeout", "page_chars", "remind_at", "force_publish_at",
                    "max_turns"]


def datetime_now_iso() -> str:
    return datetime.now().astimezone().isoformat()


def load_config(path: str | None = None) -> dict:
    """读 jsons/scraper.json——唯一配置真相，缺键直接报错（不静默回落默认值）。"""
    p = Path(path) if path else JSONS / "scraper.json"
    try:
        raw = json.loads(p.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as e:
        raise ValueError(f"配置文件不可读: {p} ({e})") from e

    ai = raw.get("ai") or {}
    missing = [k for k in REQUIRED_AI_KEYS if k not in ai]
    if missing:
        raise ValueError(f"scraper.json 缺少 ai 键: {missing}（文件 {p}）")

    conc = raw.get("concurrency")
    if not isinstance(conc, int) or conc < 1:
        raise ValueError(f"scraper.json concurrency 必须为正整数（当前: {conc!r}）")

    cfg = dict(ai)
    if cfg["provider"] in PROVIDER_DEFAULTS and not cfg["base_url"]:
        cfg["base_url"] = PROVIDER_DEFAULTS[cfg["provider"]]
    cfg["concurrency"] = conc
    cfg["limits"] = dict(raw.get("limits") or {})
    return cfg


def load_prompts(path: str | None = None) -> dict:
    """读 jsons/prompt.json（提示词注册表 + action 契约）。

    契约硬化：publish action 的 category 枚举必须与 schema.CATEGORIES 一致。
    """
    p = Path(path) if path else JSONS / "prompt.json"
    data = json.loads(p.read_text(encoding="utf-8-sig"))
    contract = data["contract"]
    try:
        enum = contract["schema"]["properties"]["identity"]["anyOf"][1] \
            ["properties"]["category"]["enum"]
    except (KeyError, IndexError, TypeError):
        raise ValueError(f"prompt.json 契约缺少 identity.category 枚举: {p}")
    if sorted(enum) != sorted(schema.CATEGORIES):
        raise ValueError(f"prompt.json category 枚举 {enum} 与 schema.CATEGORIES "
                         f"{schema.CATEGORIES} 不一致")
    prompts = {k: v["text"] for k, v in data["prompts"].items()}
    roles = {k: v.get("role", "user") for k, v in data["prompts"].items()}
    return {"contract": contract, "prompts": prompts, "roles": roles}


# ---------- 主会话 ----------

def _hint_from_extensions(exts: dict[str, int]) -> str:
    total = sum(exts.values()) or 1
    ratio = lambda names: sum(n for k, n in exts.items() if k in names) / total
    if ratio({".shp", ".dbf", ".shx", ".geojson", ".tif", ".tiff", ".gdb"}) > 0.3:
        return "dataset"
    if ratio({".mp4", ".mkv", ".avi", ".mp3", ".flac", ".jpg", ".png", ".pdf"}) > 0.5:
        return "media"
    if ratio({".py", ".js", ".ts", ".java", ".go", ".rs", ".c", ".cpp", ".h"}) > 0.2:
        return "software"
    if ratio({".docx", ".doc", ".xlsx", ".pptx", ".txt", ".md", ".epub"}) > 0.5:
        return "documents"
    return "mixed"


def _extract_json(content: str) -> dict | None:
    text = content.strip()
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        text = m.group(1)
    else:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            text = text[start:end + 1]
    try:
        out = json.loads(text)
        return out if isinstance(out, dict) else None
    except json.JSONDecodeError:
        return None


def _estimate_tokens(messages: list[dict]) -> int:
    return sum(len(m.get("content", "")) for m in messages) // 4


# ---------- messages.json（按 message 组织，逐轮原子重写） ----------

class MessageLog:
    """文件即真相：append 带锁重读-合并-原子写，支持 api 并发多实例。"""

    _lock = None   # 类级锁，首个实例创建

    def __init__(self, run_dir: Path, entry_id: str):
        if MessageLog._lock is None:
            import threading
            MessageLog._lock = threading.Lock()
        self.path = run_dir / "messages.json"
        self.entry_id = entry_id

    def _load(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            return {"version": "2.0", "packages": {}}

    def append(self, role: str, text: str, prompt_key: str | None = None):
        with MessageLog._lock:
            data = self._load()
            msgs = data.setdefault("packages", {}) \
                       .setdefault(self.entry_id, {"messages": []})["messages"]
            msg = {"role": role, "text": text}
            if prompt_key:
                msg["prompt_key"] = prompt_key
            msgs.append(msg)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                           encoding="utf-8")
            import os
            os.replace(tmp, self.path)

    def count(self) -> int:
        with MessageLog._lock:
            data = self._load()
            return len(data.get("packages", {})
                       .get(self.entry_id, {}).get("messages", []))


# ---------- 主会话 ----------

def run_session(context_pkg: dict, prompts: dict, cfg: dict,
                mlog: MessageLog, hint_exts: dict[str, int] | None = None,
                ) -> tuple[dict, float, list[str], dict]:
    """单包识别会话。返回 (identity, confidence, warnings, stats)。"""
    warnings: list[str] = []
    stats = {"pages": context_pkg["stats"]["page_count"], "pages_read": 0,
             "turns": 0, "tokens_in": 0, "tokens_out": 0}

    if not backend.endpoint_available(cfg):
        return _fallback(f"AI 后端不可用（{cfg['base_url']}）"), 0.0, warnings, stats
    model = backend.resolve_model(cfg)
    if model is None:
        return _fallback(f"AI 后端无已加载模型（{cfg['base_url']}）"), 0.0, warnings, stats

    P = prompts["prompts"]          # key -> text 模板
    R = prompts["roles"]            # key -> role
    contract = prompts["contract"]

    exts = hint_exts or {}
    hint = _hint_from_extensions(exts)
    tail_text = context_pkg["tail_page"]["text"]   # 末尾页（元数据）默认先展示
    catalog = f"内容页共 {stats['pages']} 页（第1..{stats['pages']}页），末尾页为第 {context_pkg['tail_page']['no']} 页。"

    messages: list[dict] = [
        {"role": R["system"], "content": P["system"]},
        {"role": R["first"], "content": P["first"].format(
            depth="full", hint=hint, max_turns=cfg["max_turns"],
            page_chars=cfg["page_chars"], tail_page=tail_text,
            catalog=catalog)},
    ]
    mlog.append(messages[0]["role"], messages[0]["content"], "system")
    mlog.append(messages[1]["role"], messages[1]["content"], "first")

    payload_base = {
        "model": model,
        "temperature": cfg["temperature"],
        "stream": False,
        "response_format": {"type": "json_schema", "json_schema": contract},
    }
    url = f"{cfg['base_url'].rstrip('/')}/chat/completions"

    reminded = forced = False
    forced_retries = bad_json = dupes = 0
    read_pages: set[int] = set()
    usage_tokens = 0
    pages = context_pkg["pages"]
    tail = context_pkg["tail_page"]

    for turn in range(1, cfg["max_turns"] + 1):
        stats["turns"] = turn
        tokens = usage_tokens or _estimate_tokens(messages)
        if not forced and tokens >= cfg["force_publish_at"]:
            messages.append({"role": R["force_publish"],
                             "content": P["force_publish"]})
            mlog.append(R["force_publish"], P["force_publish"], "force_publish")
            forced = True
        elif not reminded and tokens >= cfg["remind_at"]:
            messages.append({"role": R["remind"], "content": P["remind"]})
            mlog.append(R["remind"], P["remind"], "remind")
            reminded = True

        try:
            t0 = time.monotonic()
            resp = backend.post(url, {**payload_base, "messages": messages,
                               "_api_key": cfg.get("api_key", "")}, cfg["timeout"])
            stats["tokens_in"] = int((resp.get("usage") or {}).get("prompt_tokens", 0) or 0)
            stats["tokens_out"] = int((resp.get("usage") or {}).get("completion_tokens", 0) or 0)
            usage_tokens = stats["tokens_in"] + stats["tokens_out"]
            content = resp["choices"][0]["message"]["content"]
        except (OSError, KeyError, IndexError) as e:
            return _fallback(f"AI 会话请求失败: {type(e).__name__}: {e}"), 0.0, warnings, stats
        messages.append({"role": "assistant", "content": content})
        mlog.append("assistant", content)

        out = _extract_json(content)
        if out is None or out.get("action") not in ("read_page", "publish"):
            bad_json += 1
            if bad_json > 1:
                return _fallback(f"AI 输出非法 control JSON（{bad_json} 次）"), 0.0, warnings, stats
            txt = P["bad_json_retry"].format(attempt_left=2 - bad_json)
            messages.append({"role": R["bad_json_retry"], "content": txt})
            mlog.append(R["bad_json_retry"], txt, "bad_json_retry")
            continue
        action = out["action"]

        if action == "publish":
            ident = out.get("identity")
            if not isinstance(ident, dict):
                return _fallback("publish 缺少 identity 对象"), 0.0, warnings, stats
            identity, conf, vw = _validate_identity(ident)
            warnings.extend(vw)
            return identity, conf, warnings, stats

        # read_page
        page = out.get("page")
        valid = isinstance(page, int) and 1 <= page <= len(pages)
        if forced:
            forced_retries += 1
            if forced_retries > 2:
                return _fallback("强制发布后仍多次翻页，放弃识别"), 0.0, warnings, stats
            messages.append({"role": R["force_publish_only"],
                             "content": P["force_publish_only"]})
            mlog.append(R["force_publish_only"], P["force_publish_only"],
                        "force_publish_only")
            continue
        if not valid or page in read_pages:
            dupes += 1
            if dupes >= 2:
                messages.append({"role": R["stall_to_publish"],
                                 "content": P["stall_to_publish"]})
                mlog.append(R["stall_to_publish"], P["stall_to_publish"],
                            "stall_to_publish")
                forced = True
            else:
                why = "该页不存在" if not valid else "该页已读过"
                txt = P["dup_page"].format(why=why)
                messages.append({"role": R["dup_page"], "content": txt})
                mlog.append(R["dup_page"], txt, "dup_page")
            continue
        read_pages.add(page)
        stats["pages_read"] = len(read_pages)
        dupes = 0
        body = pages[page - 1]["text"] if page <= len(pages) else tail["text"]
        txt = P["page_deliver"].format(page_no=page, page_text=body)
        messages.append({"role": R["page_deliver"], "content": txt})
        mlog.append(R["page_deliver"], txt, "page_deliver")

    return _fallback(f"达到 max_turns={cfg['max_turns']} 上限仍未发布"), 0.0, warnings, stats


def _validate_identity(out: dict) -> tuple[dict, float, list[str]]:
    warnings: list[str] = []
    conf = out.get("confidence")
    if not (isinstance(conf, (int, float)) and 0.0 <= conf <= 1.0):
        warnings.append("AI 返回的 confidence 非法，按 0.5 处理")
        conf = 0.5
    if out.get("category") not in CATEGORIES:
        warnings.append(f"AI 返回的 category 非法: {out.get('category')!r}，按 unknown 处理")
        out["category"] = "unknown"
    identity = {
        "title": str(out.get("title", ""))[:200],
        "category": out["category"],
        "summary": str(out.get("summary", ""))[:2000],
        "tags": [str(t) for t in out.get("tags", [])][:20],
        "language": [str(t) for t in out.get("language", [])][:10],
    }
    if conf < LOW_CONFIDENCE:
        warnings.append(f"低置信度 {conf:.2f} < {LOW_CONFIDENCE}，建议人工复核")
    return identity, round(float(conf), 2), warnings


def _fallback(reason: str) -> dict:
    return {"title": "", "category": "unknown", "summary": "", "tags": [],
            "language": [], "_fallback_reason": reason}
