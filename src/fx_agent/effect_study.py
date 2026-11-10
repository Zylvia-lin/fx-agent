"""Read-only visual study plans; the host inspects media and resolves ambiguity."""

import fnmatch
import json
import posixpath
import re
from collections import defaultdict
from itertools import pairwise
from pathlib import Path

from fx_agent.archive import contained
from fx_agent.effect_cards import EffectLibrary, _heads, identity_status
from fx_agent.store import Store, compact
from fx_agent.template_extract import extract_project
from fx_agent.templates import IMAGES, MOVIES, TemplatePolicy


def annotations(snapshot: dict) -> list[dict]:
    result = []
    for kind, key, text_key in (
        ("network_box", "netboxes", "label"),
        ("sticky_note", "sticky_notes", "text"),
    ):
        for item in snapshot.get(key, []):
            result.append(
                {
                    "kind": kind,
                    "text": item[text_key],
                    **item,
                    "scope": "explicit_members_or_geometry_candidates_only",
                }
            )
    for node in snapshot["nodes"]:
        for key in ("comment", "label"):
            value = node.get(key) or node.get("parameters", {}).get(key)
            if value:
                result.append({"kind": "node_" + key, "text": value, "node_path": node["path"]})
    return result


def render_outputs(snapshot: dict) -> list[dict]:
    nodes = {n["path"]: n for n in snapshot["nodes"]}
    upstream = defaultdict(set)
    for edge in snapshot["connections"]:
        upstream[edge["dst_path"]].add(edge["src_path"])
    for path in nodes:
        parent = posixpath.dirname(path)
        if parent in nodes:
            upstream[parent].add(path)
    # Saved object references are useful even when the render GEO has no wires.
    for path, node in nodes.items():
        for key, value in node["parameters"].items():
            if not re.search(r"object|objpath|soppath|sop_path|targetpath", key, re.I):
                continue
            for token in str(value).strip('"').split():
                if re.search(r"[$`\[\]\n]", token) or token.startswith("^"):
                    continue
                if token.startswith("/"):
                    pattern = token
                elif token.startswith("."):
                    pattern = posixpath.normpath(posixpath.join(posixpath.dirname(path), token))
                else:
                    continue
                upstream[path].update(p for p in nodes if fnmatch.fnmatchcase(p, pattern))
    outputs = []
    for path, node in nodes.items():
        parameters = node["parameters"]
        keys = [
            k for k in ("vm_picture", "picture", "outputimage", "output_image") if k in parameters
        ]
        if node["type"] in {"Write", "DeepWrite"} and "file" in parameters:
            keys.append("file")
        for key in keys:
            visited, queue = set(), list(upstream[path])
            while queue:
                item = queue.pop()
                if item not in visited:
                    visited.add(item)
                    queue.extend(upstream[item] - visited)
            outputs.append(
                {
                    "node_path": path,
                    "parameter": key,
                    "raw_path": parameters[key],
                    "related_node_paths": sorted(visited),
                    "frame_parameters": {
                        k: parameters[k]
                        for k in ("f1", "f2", "f3", "first", "last")
                        if k in parameters
                    },
                    "object_selection": {
                        k: v for k, v in parameters.items() if re.search(r"object|objpath", k, re.I)
                    },
                    "relation_basis": "saved_connections_static_references_and_container_candidates",
                    "limitations": "选择表达式、排除遮罩、Take及运行时可见性须由宿主复核。",
                }
            )
    return outputs


def _path_pattern(raw: str, project: Path, node_path: str) -> str | None:
    from pathlib import PureWindowsPath

    if PureWindowsPath(str(project)).drive:
        project = PureWindowsPath(str(project))
    value = raw.strip().strip('"').replace("\\", "/")
    for name, replacement in (
        ("HIPNAME", project.stem),
        ("HIP", project.parent.as_posix()),
        ("OS", node_path.rsplit("/", 1)[-1]),
    ):
        value = value.replace("${" + name + "}", replacement)
        value = re.sub(r"\$" + name + r"\b", lambda _, replacement=replacement: replacement, value)
    value = re.sub(r"\$\{F(\d*)\}|\$F(\d*)|%0?(\d*)d|#+", "{FRAME}", value)
    if re.search(r"[$`\[\]\n]", value):
        return None
    if not (Path(value).is_absolute() or PureWindowsPath(value).is_absolute()):
        value = project.parent.as_posix() + "/" + value
    return re.escape(posixpath.normpath(value).lower()).replace(re.escape("{frame}"), r"\d+")


def media_inventory(entries: list[dict], outputs: list[dict], project: Path) -> list[dict]:
    groups = defaultdict(list)
    result = []
    for entry in entries:
        path = Path(entry["path"])
        suffix = path.suffix.lower()
        if suffix in MOVIES:
            result.append({"kind": "movie", "path": str(path), "size_bytes": entry["size_bytes"]})
        elif suffix in IMAGES:
            match = re.search(r"(\d+)(?=\.[^.]+$)", path.name)
            if match:
                key = str(
                    path.with_name(
                        path.name[: match.start()] + "#" * len(match[1]) + path.name[match.end() :]
                    )
                )
                groups[key].append((int(match[1]), entry))
            else:
                result.append({"kind": "image", "path": str(path), "sample_paths": [str(path)]})
    for pattern, members in sorted(groups.items()):
        members.sort(key=lambda x: x[0])
        frames = [x[0] for x in members]
        missing = [[a + 1, b - 1] for a, b in pairwise(frames) if b - a > 1]
        indices = sorted({round(i * (len(members) - 1) / 8) for i in range(9)})
        result.append(
            {
                "kind": "sequence" if len(members) > 1 else "image",
                "path": pattern,
                "frame_start": frames[0],
                "frame_end": frames[-1],
                "frame_count": len(frames),
                "missing_frame_count": sum(b - a + 1 for a, b in missing),
                "missing_frame_scope": "between_first_and_last_existing_frame",
                "missing_ranges": missing[:100],
                "missing_ranges_truncated": len(missing) > 100,
                "sample_frames": [members[i][0] for i in indices],
                "sample_paths": [members[i][1]["path"] for i in indices],
                "size_bytes": sum(x[1]["size_bytes"] for x in members),
            }
        )
    patterns = [
        (x["node_path"], _path_pattern(str(x["raw_path"]), project, x["node_path"]))
        for x in outputs
    ]
    for item in result:
        sample = (item.get("sample_paths") or [item["path"]])[0].replace("\\", "/").lower()
        item["render_node_paths"] = sorted(
            {node for node, pattern in patterns if pattern and re.fullmatch(pattern, sample)}
        )
        item["pairing"] = "output_path_candidate" if item["render_node_paths"] else "unassigned"
        item["analysis_state"] = "not_viewed"
        item["fingerprint_kind"] = "metadata_only"
        item["expected_frame_checks"] = []
        if "frame_start" in item:
            frames = sorted(x[0] for x in groups[item["path"]])
            for output in outputs:
                if output["node_path"] not in item["render_node_paths"]:
                    continue
                parms = output["frame_parameters"]
                first, last, step = (str(parms.get(k, "")) for k in ("f1", "f2", "f3"))
                if not first.isdigit() or not last.isdigit() or step not in {"", "1"}:
                    item["expected_frame_checks"].append(
                        {
                            "render_node_path": output["node_path"],
                            "state": "unresolved_saved_range_or_step",
                        }
                    )
                    continue
                start, end = int(first), int(last)
                if start > end:
                    continue
                available = [f for f in frames if start <= f <= end]
                bounds = [start - 1, *available, end + 1]
                missing = [[a + 1, b - 1] for a, b in pairwise(bounds) if b - a > 1]
                item["expected_frame_checks"].append(
                    {
                        "render_node_path": output["node_path"],
                        "state": "saved_range_candidate",
                        "frame_start": start,
                        "frame_end": end,
                        "missing_frame_count": sum(b - a + 1 for a, b in missing),
                        "missing_ranges": missing[:100],
                        "truncated": len(missing) > 100,
                    }
                )
    return result


class EffectStudy:
    def __init__(self, root: Path):
        self.effects = EffectLibrary(root)
        self.root = self.effects.root

    def plan(
        self,
        study_id: str,
        media_source: str = "",
        *,
        section: str = "all",
        offset: int = 0,
        limit: int = 50,
        max_chars: int = 12000,
        node_path: str = "",
    ) -> dict:
        if (
            section not in {"all", "annotations", "render_outputs", "media"}
            or offset < 0
            or not 1 <= limit <= 100
            or not 1024 <= max_chars <= 200000
        ):
            raise ValueError("Invalid study plan section or pagination")
        study, snapshot, _, _ = self.effects._source(study_id)
        # Re-extract the archived original, so old histories gain new metadata
        # without changing their immutable snapshot, fingerprint or card IDs.
        project_member = next(a for a in study["artifacts"] if a["role"] == "project")
        original = contained(self.root / "cases" / study["case_id"], project_member["archive_path"])
        snapshot = extract_project(original.read_bytes(), study["software"])
        outputs = render_outputs(snapshot)
        if node_path and node_path not in {n["path"] for n in snapshot["nodes"]}:
            raise ValueError("Focus node is absent from source study")
        focus_outputs = [
            o["node_path"]
            for o in outputs
            if any(
                p == node_path or p.startswith(node_path + "/")
                for p in [o["node_path"], *o["related_node_paths"]]
            )
        ]
        entries = []
        if media_source:
            _, entries = self.effects.templates._inventory(media_source, TemplatePolicy())
        content = {
            "annotations": annotations(snapshot),
            "render_outputs": outputs,
            "media": media_inventory(entries, outputs, Path(study["source_path"])),
        }
        names = list(content) if section == "all" else [section]
        result = {
            "study_id": study_id,
            "visual_analysis": "required_host_inspection",
            "source_modified": False,
            "parse_status": snapshot["parse_status"],
            "media_source": media_source,
            "media_scan_required": not bool(media_source),
            "focus_node_path": node_path,
            "focus_render_node_paths": sorted(set(focus_outputs)) if node_path else [],
            "totals": {k: len(v) for k, v in content.items()},
            "next_offsets": {},
            "truncated": False,
            "instructions": "先查作者标注，再从分支追踪渲染输出并实际查看视频及序列；路径匹配不是画面验证。无法读取、无法归属或无法判断时记录具体问题并询问用户。",
        }
        for name in names:
            result[name] = content[name][offset : offset + limit]
            result["next_offsets"][name] = offset + len(result[name])
        while len(compact(result)) > max_chars:
            name = max(names, key=lambda k: len(compact(result[k])) if result[k] else 0)
            if not result[name]:
                raise ValueError("max_chars too small for plan metadata")
            result[name].pop()
            result["next_offsets"][name] = offset + len(result[name])
        result["truncated"] = any(result["next_offsets"][k] < len(content[k]) for k in names)
        if any(not result[k] and offset < len(content[k]) for k in names):
            result["detail_required"] = True
            result["required_chars"] = max(
                len(compact(content[k][offset])) + 2000 for k in names if offset < len(content[k])
            )
        return result

    def queue(self, *, offset: int = 0, limit: int = 20, max_chars: int = 12000) -> dict:
        if offset < 0 or not 1 <= limit <= 100 or not 1024 <= max_chars <= 200000:
            raise ValueError("Invalid queue pagination")
        with Store(self.root).connection() as db:
            studies = [
                json.loads(r[0])
                for r in db.execute("SELECT payload FROM template_studies ORDER BY rowid")
            ]
        parents = {p for study in studies for p in study["parent_ids"]}
        rows = _heads(self.effects._rows())
        by_study = defaultdict(list)
        for card in rows:
            by_study[card["study_id"]].append(card)
        items = []
        for study in studies:
            if study["study_id"] in parents:
                continue
            cards = by_study[study["study_id"]]
            states = [identity_status(c) for c in cards]
            pending = [c for c, state in zip(cards, states, strict=True) if state != "identified"]
            catalog = self.effects.catalog(study["study_id"])
            if catalog["learning_state"] == "reviewed":
                continue
            items.append(
                {
                    "study_id": study["study_id"],
                    "title": study["title"],
                    "source_path": study["source_path"],
                    "learning_state": catalog["learning_state"],
                    "card_count": len(cards),
                    "pending_card_count": len(pending),
                    "questions": [
                        {
                            "card_id": c["card_id"],
                            "title": c["title"],
                            "questions": c.get("questions", []),
                        }
                        for c in pending
                    ],
                    "media_issues": catalog["media_issues"],
                    "next_tool": "fx_plan_effect_study",
                }
            )
        page = items[offset : offset + limit]
        while page and len(compact(page)) > max_chars - 500:
            page.pop()
        result = {
            "items": page,
            "total": len(items),
            "next_offset": offset + len(page),
            "truncated": offset + len(page) < len(items),
            "history_preserved": True,
        }
        if not page and offset < len(items):
            result["detail_required"] = True
            result["required_chars"] = len(compact(items[offset])) + 500
        return result
