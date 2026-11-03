"""Static HIP/NK inspection: never import a DCC, evaluate expressions or cook nodes."""

import base64
import hashlib
import posixpath
import re
from collections import defaultdict

from fx_agent.vendor import kleer_hip_parser as upstream

EXTRACTOR_VERSION = "1"
TEXT_SECTIONS = {"init", "def", "parm", "chn", "spare", "net", "inp", "postitdef", "netboxinit"}


def _cpio_sections(data: bytes) -> dict[str, bytes]:
    """Validate old ASCII cpio boundaries instead of accepting a truncated network."""
    if not data.startswith(b"070707"):
        raise ValueError("Unsupported HIP container; original retained, no DCC load attempted")
    sections, offset = {}, 0
    while offset < len(data):
        # SideFX also emits newline/NUL padding between otherwise valid ODC records.
        while offset < len(data) and data[offset] in b"\0\n\r \t":
            offset += 1
        if offset == len(data):
            break
        if offset + 76 > len(data):
            raise ValueError("Truncated cpio header")
        header = data[offset : offset + 76]
        if not header.startswith(b"070707"):
            raise ValueError("Invalid cpio header")
        try:
            name_size, size = int(header[59:65], 8), int(header[65:76], 8)
        except ValueError as exc:
            raise ValueError("Invalid cpio size") from exc
        start = offset + 76
        if name_size < 1 or start + name_size + size > len(data):
            raise ValueError("Truncated cpio section")
        name_bytes = data[start : start + name_size]
        # SideFX ODC variants also count the terminating NUL in the body size.
        name = name_bytes.rstrip(b"\0").decode("utf-8", errors="replace")
        offset = start + name_size
        body = data[offset : offset + size]
        offset += size
        if name == "TRAILER!!!":
            return sections
        if name in sections:
            raise ValueError("Duplicate cpio section")
        sections[name] = body
    # Some SideFX files have no trailer; accept only an exact section boundary.
    if offset != len(data) or not sections:
        raise ValueError("Truncated cpio archive")
    return sections


def _commands(text: str) -> list[str]:
    """Split Tcl-like commands at top-level newlines; keep nested blocks verbatim."""
    commands, start, braces, brackets, quoted, escaped = [], 0, 0, 0, False, False
    comment = False
    for index, char in enumerate(text):
        if comment:
            if char == "\n":
                comment, start = False, index + 1
            continue
        if char == "#" and not (braces or brackets or quoted) and not text[start:index].strip():
            comment = True
            continue
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == '"' and braces == 0:
            quoted = not quoted
        if not quoted:
            if char == "{":
                braces += 1
            elif char == "}":
                braces -= 1
            elif char == "[" and braces == 0:
                brackets += 1
            elif char == "]" and braces == 0:
                brackets -= 1
        if braces < 0 or brackets < 0:
            raise ValueError("Unbalanced script delimiters")
        if char in "\n;" and not (braces or brackets or quoted):
            command = text[start:index].strip()
            if command and not command.startswith("#"):
                commands.append(command)
            start = index + 1
    if braces or brackets or quoted:
        raise ValueError("Unterminated script block")
    if text[start:].strip():
        commands.append(text[start:].strip())
    return commands


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        return value[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return value


def _hip_parameters(text: str) -> dict:
    """Keep serialized values, including multiline VEX, without evaluating channels."""
    result = {}
    pattern = re.compile(r"(?m)^\s*([^\s{}]+)\s+(?:\[[^\n]*?\]\s+)?\(")
    offset = 0
    while match := pattern.search(text, offset):
        start, depth, quoted, escaped = match.end(), 1, False, False
        for end in range(start, len(text)):
            char = text[end]
            if escaped:
                escaped = False
                continue
            if char == "\\":
                escaped = True
            elif char == '"':
                quoted = not quoted
            elif not quoted:
                if char == "(":
                    depth += 1
                elif char == ")":
                    depth -= 1
                    if depth == 0:
                        result[match[1]] = _unquote(text[start:end])
                        offset = end + 1
                        break
        else:
            break  # The raw parm section remains available for an unsupported encoding.
    return result


def extract_hip(data: bytes) -> dict:
    sections = _cpio_sections(data)
    result = upstream._build_result(sections, "archived_project")
    decoded = {
        name: body.lstrip(b"\0").decode("utf-8", errors="replace")
        for name, body in sections.items()
    }
    by_node = defaultdict(dict)
    for name, text in decoded.items():
        path, _, suffix = name.rpartition(".")
        if suffix in TEXT_SECTIONS:
            by_node[path][suffix] = text
    for node in result["nodes"]:
        prefix = node["path"].lstrip("/")
        raw = by_node.get(prefix, {})
        node["raw_sections"] = raw
        node["parameters"] = _hip_parameters(raw.get("parm", ""))
        definition = raw.get("def", "")
        comment = re.search(r"(?m)^\s*comment\s+(.+)$", definition)
        if comment:
            node["comment"] = _unquote(comment[1])
        position = re.search(r"(?m)^position\s+([-\d.eE]+)\s+([-\d.eE]+)", definition)
        node["position"] = [float(position[1]), float(position[2])] if position else None
    # Connections can use relative or absolute source paths.
    result["connections"] = []
    for node in result["nodes"]:
        definition = sections.get(node["path"].lstrip("/") + ".def")
        if definition is None:
            continue
        for connection in upstream._parse_inputs(definition):
            source = connection["src_name"]
            source = (
                source
                if source.startswith("/")
                else posixpath.join(posixpath.dirname(node["path"]), source)
            )
            result["connections"].append(
                {
                    "src_path": posixpath.normpath(source),
                    "src_output": connection["src_output"],
                    "dst_path": node["path"],
                    "dst_input": connection["dst_input"],
                }
            )
    result["sections"] = [
        {"name": name, "size_bytes": len(body), "sha256": hashlib.sha256(body).hexdigest()}
        for name, body in sections.items()
    ]
    result["global_sections"] = {
        name: text
        for name, text in decoded.items()
        if name.startswith(".") or name in {"expression.func"}
    }
    result["binary_metadata"] = {
        name: base64.b64encode(body).decode("ascii")
        for name, body in sections.items()
        if name.rsplit(".", 1)[-1] in TEXT_SECTIONS and b"\0" in body.lstrip(b"\0")
    }
    result["limitations"] = [
        "静态结构提取；原始 parm/chn/def 等文本保留，不将序列化曲线冒充已求值参数。",
        "未运行 Houdini、加载 HDA、检查插件、求解或验证画面；类别按路径推断。",
    ]
    result["parse_status"] = "parsed" if result["nodes"] else "partial"
    return result


def extract_nuke(data: bytes) -> dict:
    text = data.decode("utf-8-sig", errors="replace")
    nodes, connections, limitations, global_commands = [], [], [], []
    stack, variables, groups, context = [], {}, [], ""
    for command in _commands(text):
        if command.startswith(("#", "version ")):
            continue
        if command == "end_group":
            if not groups:
                raise ValueError("Unexpected end_group")
            context, stack, variables = groups.pop()
            continue
        if match := re.fullmatch(r"set\s+(\S+)\s+\[stack\s+(\d+)\]", command):
            index = int(match[2])
            variables[match[1]] = stack[-1 - index] if len(stack) > index else None
            continue
        if command.startswith("push "):
            value = command[5:].strip()
            if value == "0":
                stack.append(None)
            elif value.startswith("$") and value[1:] in variables:
                stack.append(variables[value[1:]])
            else:
                stack.append(None)
                limitations.append("未解析 push 引用：" + value[:200])
            continue
        match = re.fullmatch(r"([A-Za-z_][\w:]*)\s*\{(.*)\}", command, re.S)
        if not match:
            limitations.append("保留但未执行命令：" + command[:200])
            continue
        node_type, body = match.groups()
        parameters = {}
        for knob in _commands(body):
            split = knob.split(None, 1)
            key, value = split[0], split[1] if len(split) > 1 else ""
            value = value.strip()
            parameters[key] = (
                value[1:-1] if value.startswith("{") and value.endswith("}") else _unquote(value)
            )
        if node_type != "Root" and "name" not in parameters:
            global_commands.append(command)
            if node_type not in {"define_window_layout_xml", "add_layer", "addFormat"}:
                limitations.append("未识别为节点的命令已保留：" + node_type)
            continue
        name = parameters.get("name", f"{node_type}_{len(nodes)}")
        path = context + "/" + name
        if any(n["path"] == path for n in nodes):
            raise ValueError("Duplicate Nuke node path")
        node = {
            "path": path,
            "name": name,
            "type": node_type,
            "category": "NUKE",
            "parameters": parameters,
            "raw_script": command,
            "children": [],
        }
        node["position"] = [parameters.get("xpos"), parameters.get("ypos")]
        nodes.append(node)
        if node_type == "Root":
            continue
        default_inputs = (
            0
            if node_type
            in {
                "Read",
                "DeepRead",
                "Constant",
                "Input",
                "ReadGeo2",
                "Camera2",
                "Light2",
                "BackdropNode",
                "StickyNote",
            }
            else 1
        )
        count_text = parameters.get("inputs", str(default_inputs))
        if not count_text.isdigit() or int(count_text) > 1024:
            limitations.append("未解析输入数量：" + path)
            count = 0
        else:
            count = int(count_text)
        for port in range(count):
            source = stack.pop() if stack else None
            if source:
                connections.append(
                    {"src_path": source, "src_output": 0, "dst_path": path, "dst_input": port}
                )
        if node_type not in {"BackdropNode", "StickyNote"}:
            stack.append(path)
        if node_type in {"Group", "Gizmo"}:
            groups.append((context, stack, variables))
            context, stack, variables = path, [], {}
    if groups:
        raise ValueError("Unclosed Nuke group")
    by_path = {n["path"]: n for n in nodes}
    for node in nodes:
        if parent := by_path.get(node["path"].rsplit("/", 1)[0]):
            parent["children"].append(node["path"])
    return {
        "nodes": nodes,
        "connections": connections,
        "sticky_notes": [],
        "netboxes": [],
        "global_sections": {str(i): command for i, command in enumerate(global_commands)},
        "parse_status": "partial" if limitations or not nodes else "parsed",
        "limitations": limitations
        + [
            "静态读取；表达式、曲线、回调和 Tcl/Python 均作为原文保存，未执行。外部 Gizmo 内部结构需另提供文件。"
        ],
    }


def extract_project(data: bytes, software: str) -> dict:
    try:
        result = extract_hip(data) if software == "houdini" else extract_nuke(data)
    except (ValueError, UnicodeError, OverflowError) as exc:
        result = {
            "nodes": [],
            "connections": [],
            "sticky_notes": [],
            "netboxes": [],
            "parse_status": "partial",
            "limitations": [str(exc)],
        }
    return result | {
        "schema_version": 1,
        "extractor_version": EXTRACTOR_VERSION,
        "software": software,
        "execution": "static_only",
        "visual_validation": "not_performed",
    }


def extract_patterns(snapshot: dict) -> list[dict]:
    """Preserve exact node configurations; topology and recipes get stable hashes."""
    from fx_agent.store import compact

    nodes = [
        {key: value for key, value in node.items() if key not in {"raw_sections", "raw_script"}}
        for node in snapshot["nodes"]
    ]
    if not nodes:
        return []
    patterns = []

    def add(kind, content):
        text = compact(content)
        patterns.append(
            {
                "type": kind,
                "pattern_id": hashlib.sha256((kind + text).encode()).hexdigest(),
                "text": text,
            }
        )

    add("scene", {k: snapshot[k] for k in ("nodes", "connections", "sticky_notes", "netboxes")})
    by_path = {n["path"]: n for n in nodes}
    adjacency = defaultdict(set)
    for edge in snapshot["connections"]:
        a, b = edge["src_path"], edge["dst_path"]
        if a in by_path and b in by_path:
            adjacency[a].add(b)
            adjacency[b].add(a)
    visited = set()
    for path in sorted(adjacency):
        if path in visited:
            continue
        queue, component = [path], set()
        while queue:
            current = queue.pop()
            if current not in component:
                component.add(current)
                queue.extend(adjacency[current] - component)
        visited.update(component)
        add(
            "subgraph",
            {
                "nodes": [by_path[p] for p in sorted(component)],
                "connections": [
                    e
                    for e in snapshot["connections"]
                    if e["src_path"] in component and e["dst_path"] in component
                ],
            },
        )
    seen = set()
    for node in nodes:
        recipe = {"type": node["type"], "parameters": node["parameters"]}
        key = compact(recipe)
        if key not in seen:
            add("recipe", recipe | {"source_node": node["path"]})
            seen.add(key)
    return patterns
