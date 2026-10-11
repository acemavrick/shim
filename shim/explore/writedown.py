"""Bounded evidence batches; retain distinct inputs rather than duplicate fetches."""
from urllib.parse import parse_qsl, urlsplit
import json
import re


def candidate_batches(observed, pages):
    groups = {}
    for item in observed:
        if not item.interesting:
            continue
        parts = urlsplit(item.url)
        key = (item.method, parts.netloc, parts.path, tuple(sorted(k for k, _ in parse_qsl(parts.query))))
        group = groups.setdefault(key, [])
        if len(group) < 2 and not any((v.url, v.post_data) == (item.url, item.post_data) for v in group):
            group.append(item)
    values = list(groups.values())[:24]
    return [{"observed": [v for group in values[i:i + 3] for v in group],
             "pages": [p for p in pages if any(urlsplit(v.url).path == urlsplit(p.url).path
                        for group in values[i:i + 3] for v in group)][:3]}
            for i in range(0, len(values), 3)]


def validate_inputs(tool, observed):
    placeholders = re.findall(r"\{([^}]+)\}", tool.url)
    if any(not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", name) for name in placeholders) or len(placeholders) != len(set(placeholders)):
        return "invalid or repeated path placeholder"
    pattern = re.escape(tool.url.split("?")[0].rstrip("/"))
    pattern = re.sub(r"\\\{([^}]+)\\\}", lambda m: f"(?P<{m.group(1)}>[^/?]+)", pattern)
    values = {}
    for observation in observed:
        match = re.fullmatch(pattern, observation.url.split("?")[0].rstrip("/"))
        if not match or observation.method != tool.method.upper():
            continue
        payload = dict(parse_qsl(urlsplit(observation.url).query))
        if observation.post_data:
            try:
                body = json.loads(observation.post_data)
                if isinstance(body, dict):
                    payload.update(body)
            except ValueError:
                payload.update(dict(parse_qsl(observation.post_data)))
        for name, value in payload.items():
            values.setdefault(("wire", name), set()).add(str(value).lower() if isinstance(value, bool) else str(value))
        for name, value in match.groupdict().items():
            from urllib.parse import unquote
            values.setdefault(("path", name), set()).add(unquote(value))
    for name, spec in tool.inputs.items():
        key = ("path", name) if spec.get("in") == "path" else ("wire", spec.get("wire") or name)
        actual = values.get(key, set())
        if spec.get("examples") and any((str(v).lower() if isinstance(v, bool) else str(v)) not in actual for v in spec["examples"]):
            return f"input {name} contains unobserved example values"
    for wire in tool.constants:
        if len(values.get(("wire", wire), set())) > 1:
            return f"varying field {wire} cannot be pinned as a constant"
        if re.search(r"token|session|cookie|auth|signature", wire, re.I):
            return f"session field {wire} cannot be pinned as a constant"
    return None
