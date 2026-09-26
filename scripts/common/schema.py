"""Template-led validation.

A template's filled values are fixed (must match literally); empty values
("", [], 0, 0.0) are slots that must exist with the matching type; null slots
must exist with any value. The same derivation produces the response_format
JSON schema sent to the backend, so backend enforcement and local checks read
one template.
"""


def check(obj, template):
    problems = []

    def walk(node, tmpl, path):
        if isinstance(tmpl, dict):
            if not isinstance(node, dict):
                problems.append(f"{path or 'root'}: expected object")
                return
            for key, sub in tmpl.items():
                if key not in node:
                    problems.append(f"{path}.{key}: missing" if path else f"{key}: missing")
                else:
                    walk(node[key], sub, f"{path}.{key}" if path else key)
            for key in node:
                if key not in tmpl:
                    problems.append(f"{path}.{key}: unexpected" if path else f"{key}: unexpected")
        elif tmpl is None:
            pass
        elif isinstance(tmpl, bool):
            if node is not tmpl:
                problems.append(f"{path}: expected {tmpl}")
        elif tmpl == "":
            if not isinstance(node, str):
                problems.append(f"{path}: expected string")
        elif tmpl == []:
            if not isinstance(node, list):
                problems.append(f"{path}: expected list")
        elif tmpl == 0:
            if isinstance(node, bool) or not isinstance(node, (int, float)):
                problems.append(f"{path}: expected number")
        else:
            if node != tmpl:
                problems.append(f"{path}: expected {tmpl!r}")

    walk(obj, template, "")
    return problems


def to_json_schema(template, enums=None):
    def node(t):
        if isinstance(t, dict):
            return {
                "type": "object",
                "properties": {k: node(v) for k, v in t.items()},
                "required": list(t.keys()),
                "additionalProperties": False,
            }
        if isinstance(t, list):
            return {"type": "array"}
        if isinstance(t, bool):
            return {"type": "boolean"}
        if isinstance(t, (int, float)):
            return {"type": "number"}
        if isinstance(t, str):
            return {"type": "string"}
        return {}

    schema = node(template)
    for key, values in (enums or {}).items():
        if key in schema.get("properties", {}):
            schema["properties"][key]["enum"] = list(values)
    return schema


def derive(name, template, enums=None):
    return {"name": name, "strict": True, "schema": to_json_schema(template, enums)}
