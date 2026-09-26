"""Loads extractor prompts and contracts, assembles messages.

Prompt entries are {role, frontier, text}: frontier is the standing frame —
contract references, protocol labels, tokenized structure ("Page {page} of
{page_total}:"); text is the registered content — role persona, payload slots
({page_text}, {head}). Sending joins the non-empty parts with a blank line,
frontier first. {_contract:<name>} inside either part is replaced at load time
with the contract rendered from extractors/_contract.json. Runtime tokens are
replaced per send via get().
"""
import json
from pathlib import Path

from schema import derive

_TOKEN = "{_contract:"


class Builder:
    def __init__(self, prompts, contracts, publish):
        self.prompts = prompts
        self.contracts = contracts
        self.publish_template = publish

    def response_format(self, name):
        c = self.contracts[name]
        return derive(name, c["template"], c.get("enums"))

    def get(self, key, **tokens):
        entry = self.prompts[key]
        content = "\n\n".join(p for p in (entry["frontier"], entry["text"]) if p)
        for token, value in tokens.items():
            content = content.replace("{" + token + "}", str(value))
        return {"role": entry["role"], "content": content}


def _render(name, c):
    lines = [f"contract {name} - reply with exactly one of:"]
    template = c["template"]
    variants = [
        {**template, key: value}
        for key, values in c.get("enums", {}).items()
        for value in values
    ]
    if not variants:
        variants = [template]
    lines.extend(json.dumps(v, ensure_ascii=False) for v in variants)
    return "\n".join(lines)


def _inject(text, contracts):
    out = text
    while _TOKEN in out:
        start = out.index(_TOKEN)
        end = out.index("}", start)
        name = out[start + len(_TOKEN):end]
        out = out[:start] + _render(name, contracts[name]) + out[end + 1:]
    return out


def load(contract_path, prompts_path, publish_path):
    contracts = json.loads(Path(contract_path).read_text("utf-8"))
    data = json.loads(Path(prompts_path).read_text("utf-8"))
    publish = json.loads(Path(publish_path).read_text("utf-8"))
    prompts = {}
    for key, entry in data["prompts"].items():
        prompts[key] = {
            "role": entry["role"],
            "frontier": _inject(entry.get("frontier", ""), contracts),
            "text": _inject(entry.get("text", ""), contracts),
        }
    for name, c in contracts.items():
        if not isinstance(c.get("template"), dict):
            raise ValueError(f"_contract.json: {name} has no template object")
    return Builder(prompts, contracts, publish)
