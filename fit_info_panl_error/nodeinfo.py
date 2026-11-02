from __future__ import annotations
import html
import itertools
import math
import pathlib
import re
from datetime import datetime, timedelta
from time import perf_counter
from typing import (TYPE_CHECKING, Any, Callable, Collection, Iterable,
                    NamedTuple, Optional, Sequence, Mapping, Match, Self)

from hutil.PySide import QtCore, QtGui
from hutil.PySide.QtCore import Qt

from pxr import Usd, Kind

import hou
import usdprimicons

# Fun fact: can't import visualizers here because it doesn't work in MPlay.
from . import attrdiffs, custom, general
from hutil.qt.common import core, models
from hutil.help.houdini import api as help_api
from hutil.qt.pluto import colors

if TYPE_CHECKING:
    from . import tops


if hou.isUIAvailable():
    # hou.viewportVisualizers isn't available in MPlay
    GENERIC_VIZ = hou.viewportVisualizers.type("vis_generic")
    TAG_VIZ = hou.viewportVisualizers.type("vis_tag")
    COLOR_VIZ = hou.viewportVisualizers.type("vis_color")
    MARKER_VIZ = hou.viewportVisualizers.type("vis_marker")

# Expression matching evaluated things in string expressions
var_expr = re.compile(r"""
( # local/environment variable
    [$]  # starts with a dollar sign
    (?P<bare>[A-Z][A-Z0-9]*)  # bare word
    |
    ({(?P<braced>[A-Z][A-Z0-9]*)})  # word in braces
)
| # or, expression in backticks
(?P<ticked>[`][^`]*[`])
""", re.VERBOSE)
geo_memory_expr = re.compile(r"""
(?P<full>[^(;]+)(?P<inst>\(Instanced\))?
(;[ ]New:[ ](?P<new>[^;]+))?
(;[ ]Unique:[ ](?P<unique>[^;]+))?
""", re.VERBOSE)

node_specific_branch_expr = re.compile(r".* [A-Z]OP Info$")
node_path_expr = re.compile(
    "[.]?/[A-Za-z_][A-Za-z0-9_]*(/[A-Za-z_][A-Za-z0-9_]*)*"
)
lop_geo_count_key_expr = re.compile(r"^([^(]+) \(([^)]+)\):Total$")


def abbr(string: str, weight=400) -> str:
    return f"<span style='font-weight: {weight}'>{string}</span>"


def span(string: str, weight=400) -> str:
    return f"<span style='font-weight: {weight}'>{string}</span>"


def markupFloat(string: float | str, show_fraction=True,
                whole_weight=400, fraction_weight=200) -> str:
    string = str(string)
    dot = string.find(".")
    if dot >= 0:
        out = span(string[:dot], weight=whole_weight)
        if show_fraction:
            out += span(string[dot:], weight=fraction_weight)
        return out
    else:
        return span(string, weight=whole_weight)


def divideSeconds(secs: float) -> tuple[int, int, float]:
    # days, secs = divmod(secs, 86_400)
    hours, secs = divmod(secs, 3600)
    mins, secs = divmod(secs, 60)
    return int(hours), int(mins), secs


def formatHMS(secs: float, markup=True, decimal_places=0, auto_decimal=2,
              less_than_1_ms=False, whole_weight=400, fraction_weight=200,
              zero="0") -> str:
    if not secs:
        return zero
    if less_than_1_ms and secs < 0.001:
        return "&lt; 1ms" if markup else "< 1ms"
    if secs < 1 and auto_decimal > decimal_places:
        decimal_places = auto_decimal

    if secs < 0.1:
        msec = round(secs * 1000.0, auto_decimal)
        if msec.is_integer():
            msec = int(msec)
        suffix = abbr('ms') if markup else "ms"
        return f"{msec}{suffix}"

    secs = round(secs, decimal_places)
    if isinstance(secs, float) and secs.is_integer():
        secs = int(secs)

    parts: list[tuple[float, str]] = []
    hours, mins, secs = divideSeconds(secs)
    secs = round(secs, decimal_places)
    if hours:
        parts.append((hours, "h"))
    if mins or hours:
        parts.append((mins, "m"))
    parts.append((secs, "s"))

    if markup:
        return " ".join(
            markupFloat(v, whole_weight=whole_weight,
                        fraction_weight=fraction_weight) + abbr(unit)
            for v, unit in parts
        )
    else:
        return " ".join(f"{v}{unit}" for v, unit in parts)


def almostEqual(x: float, y: float, places=6) -> bool:
    return round(x, places) == round(y, places)


def formatNum(value: float, places=2) -> str:
    if math.isnan(value):
        text = "NaN"
    elif math.isinf(value):
        text = "Inf"
        if value < 0:
            text = f"-{text}"
    else:
        text = str(round(value, places))
    return text


def formatVoxelSize(size: hou.Vector3) -> str:
    if almostEqual(size[0], size[1], 5) and \
            almostEqual(size[0], size[2], 5):
        return formatNum(size[0], places=5)
    else:
        return "\xD7".join(formatNum(x, places=5) for x in size)


def usdPrimIcon(prim_name: str) -> str:
    if prim_name in general.prim_type_icons:
        return general.prim_type_icons[prim_name]
    else:
        stem = prim_name.lower().replace(" ", "_")
        return f"SCENEGRAPH_primtype_{stem}"


def countListKeyFn(exemplars: Sequence[str]) -> Callable:
    # Returns a sort key function to order names by the order they appear in
    # the given list of names
    ordering = {name: i for i, name in enumerate(exemplars)}

    def key_fn(count: Number) -> int:
        return ordering.get(count.name, len(exemplars))

    return key_fn


def formatDatetime(dt: datetime, relative_days=True, use_24h=False) -> str:
    if not dt:
        return ""

    time_html = dt.strftime(general.TIME_FORMAT_24HR if use_24h
                            else general.TIME_FORMAT_12HR)
    date_html = dt.strftime(general.DATE_FORMAT)
    if relative_days:
        date = dt.date()
        today = datetime.today().date()
        if date == today:
            date_html = "Today"
        elif date == today - timedelta(days=1):
            date_html = "Yesterday"
    return general.DATETIME_FORMAT.format(date=date_html, time=time_html,
                                          dt_obj=dt)


def objSortKey(obj: hou.Attrib | hou.Group) -> tuple[str | int, ...]:
    return general.naturalSortKey(obj.name())


def refSortKeyFn(node: hou.Node, parm_refs=False
                 ) -> Callable[[str], tuple[int, str]]:
    node_path = node.path()
    if not node_path.endswith("/"):
        node_path += "/"

    def refSortKey(ref: str) -> tuple[int, str]:
        # Sort references on or under the current node (start with "./") last,
        # except for parm refs sort "./parm" before any "./node_path/parm"
        if ref.startswith(node_path):
            if parm_refs and not ref.removeprefix(node_path).count("/"):
                order = 1
            else:
                order = 2
        else:
            order = 0
        return order, ref

    return refSortKey


def unique(seq: Iterable[Any]) -> list[Any]:
    # Remove duplicates while preserving order
    seen = set()
    seen_add = seen.add
    return [x for x in seq if not (x in seen or seen_add(x))]



def nodeTypeSummary(nodetype: hou.NodeType) -> Optional[str]:
    # tt = perf_counter()
    try:
        zone = help_api.getZone()
    except FileNotFoundError:
        return None
    doc_path = zone.source_path(help_api.nodeTypeToPath(nodetype))
    doc = zone.searcher().document(doc_path)
    if doc:
        return doc.summary
    # print("Help", perf_counter() - tt)


def _isDefaultBitSize(num_type: hou.numericData, default_bitsize: int
                      ) -> bool:
    if default_bitsize == 32:
        return num_type in (hou.numericData.Int32, hou.numericData.Float32)
    elif default_bitsize == 64:
        return num_type in (hou.numericData.Int64, hou.numericData.Float64)
    else:
        return False


def attrBriefDataDesc(attrib: hou.Attrib, pref_prec: int) -> str:
    dtype = attrib.dataType()
    size = attrib.size()
    type_desc = general.attr_data_type_to_name.get(dtype)

    if dtype == hou.attribData.Float and attrib.qualifier() == "Index Pair":
        return f"{size // 2} unique"

    if dtype == hou.attribData.Int or dtype == hou.attribData.Float:
        num_type = attrib.numericDataType()
        if not _isDefaultBitSize(num_type, pref_prec):
            type_desc = general.num_type_to_brief_name.get(
                num_type, num_type.name())

    if size > 1:
        type_desc = f"{size}\u2219{type_desc}"
    if attrib.isArrayType():
        type_desc += "[ ]"

    if dtype == hou.attribData.String:
        string_count = attrib.stringCount()
        if string_count > 1:
            type_desc += f" ({string_count:,} unique)"

    return type_desc


def attrFullDataDesc(attrib: hou.Attrib, include_string_count=True) -> str:
    dtype = attrib.dataType()
    type_desc = general.attr_data_type_to_name.get(dtype)
    if dtype == hou.attribData.Int or dtype == hou.attribData.Float:
        num_type = attrib.numericDataType()
        type_desc = general.num_type_to_full_name.get(num_type, num_type.name())

    size = attrib.size()
    if size > 1:
        type_desc = f"{size}\xd7{type_desc}"
    if attrib.isArrayType():
        type_desc = f"Array of {type_desc}"
    if include_string_count and dtype == hou.attribData.String:
        string_count = attrib.stringCount()
        if string_count:
            type_desc += f" / {string_count} unique values"

    return type_desc


def attrDataTypeColor(attrib: hou.Attrib) -> Optional[QtGui.QColor]:
    dtype = attrib.dataType()
    is_array = attrib.isArrayType()
    size = attrib.size()
    qual = attrib.qualifier()

    n: Optional[str] = None
    if qual == "Color":
        n = "Color"
    elif qual == "Normal":
        n = "Normal"
    elif dtype == hou.attribData.Int:
        n = "IntArray" if is_array else "Int"
    elif qual == "Vector":
        if size == 2:
            n = "Vector2"
        elif size == 3:
            n = "Vector"
        elif size == 4:
            n = "Vector4"
        if is_array:
            n += "Array"
    elif qual == "Matrix":
        if size == 4:
            n = "Matrix2"
        elif size == 9:
            n = "Matrix3"
        elif size == 16:
            n = "Matrix4"
        if is_array:
            n += "Array"
    elif dtype == hou.attribData.Float:
        n = "FloatArray" if is_array else "Float"
    elif dtype == hou.attribData.String:
        n = "StringArray" if is_array else "String"
    elif dtype == hou.attribData.Dict:
        n = "DictArray" if is_array else "Dict"

    if n:
        hcolor = hou.ui.colorFromName(f"VopInOut{n}Color")
        return hou.qt.toQColor(hcolor)


def attrQualifers(attrib: hou.Attrb) -> tuple[str, str]:
    qual = attrib.qualifier()
    dtype = attrib.dataType()
    size = attrib.size()
    if dtype == hou.attribData.Int and qual == "Non-arithmetic":
        brief = ""
    elif dtype == hou.attribData.Float and size in (9, 16) and qual == "Matrix":
        if size == 9:
            brief = "3\xd73 Matrix"
        else:
            brief = "4\xd74 Matrix"
    else:
        brief = general.brief_qualifier.get(qual, qual)
    return qual, brief


def frozenGeometry(node: hou.SopNode, output_index=0) -> Optional[hou.Geometry]:
    if geom := node.geometry(output_index):
        geom = geom.freeze(clone_data_ids=True)
    return geom


def nodeDefinitionString(node: hou.Node) -> Optional[str]:
    nodetype = node.type()
    if defn := nodetype.definition():
        path = hou.text.collapseCommonVars(
            defn.libraryFilePath(),
            ("$HFS", "$HIP", "$JOB", "$HOME")
        )
        return f"{path}?{nodetype.nameWithCategory()}"


def objectOutputNode(obj_node: hou.Node) -> Optional[hou.Node]:
    for out_node in obj_node.subnetOutputs():
        # subnetOutputs() *seems* to return the outputs in index order, and even
        # though it's possible to have two outputs set to the same index, it
        # seems to return the "actually used" output 0 first, which is all we
        # need here
        parm = out_node.parm("outputidx")
        if parm and parm.evalAsInt() == 0:
            return out_node


def countLabel(noun: str, count: int, plural: str = None) -> str:
    if count != 1:
        noun = plural if plural is not None else f"{noun}s"
    return f"{count:,} {noun}"


def getDataId(data_id: hou.AttribDataId | tuple[int, ...]) -> int:
    # ????????????
    if isinstance(data_id, hou.AttribDataId):
        data_id = data_id.vexAttribDataId()
    return data_id[3]


def trs(m: hou.Matrix4, xform_order: str, rot_order: str,
        pivot: hou.Vector3, pivot_rot: hou.Vector3,
        ) -> list[tuple[float, float, float]]:
    d: dict[str, tuple[float, float, float]] = \
        m.explode(transform_order=xform_order, rotate_order=rot_order,
                  pivot=pivot, pivot_rotate=pivot_rot)
    return [
        tuple(d["translate"]),
        tuple(d["rotate"]),
        tuple(d["scale"])
    ]


def _canDiffWith(input_node: hou.Node) -> bool:
    return (input_node and
            input_node.cookCount() and
            hasattr(input_node, "geometry") and
            not input_node.needsToCook())


def _attribs(geom: hou.Geometry, attr_type: str, debug=False
             ) -> list[hou.Attrib]:
    # hou.Geometry API is annoying
    if attr_type == "point":
        method = geom.pointAttribs
    elif attr_type == "primitive":
        method = geom.primAttribs
    elif attr_type == "vertex":
        method = geom.vertexAttribs
    elif attr_type == "detail":
        method = geom.globalAttribs
    else:
        raise TypeError(attr_type)
    attribs = sorted(method(hou.attribScope.Public), key=objSortKey)
    # More bad API: can't get public and private attrs in one call
    # anymore, have to make two calls now
    if debug:
        attribs = list(method(hou.attribScope.Private)) + attribs
    return attribs


def makeAttr(attrib: hou.Attrib, type_name: str, pref_prec: int, debug=False,
             added_names: Collection[str] = None,
             diffs: attrdiffs.Differences = None, full_diff=False,
             viz_names: Collection[str] = None) -> Attr:
    attr = Attr()
    attr.name = name = attrib.name()
    attr.typeName = type_name
    attr.dataType = attrib.dataType().name()
    attr.size = attrib.size()
    attr.dataTypeName = attrBriefDataDesc(attrib, pref_prec)
    attr.dataTypeDesc = attrFullDataDesc(attrib)
    attr.dataTypeColor = attrDataTypeColor(attrib)
    attr.qualifier, attr.qualifierBrief = attrQualifers(attrib)
    if debug:
        attr.debugDataID = getDataId(attrib.dataId())

    if added_names and (not name.startswith(".")) and name in added_names:
        attr.isNew = True
        attr.diffText = "Added"
    if diffs and full_diff:
        adiff = diffs.diffs[attrib.type(), name]
        if adiff.isModified():
            attr.isModified = True
            attr.diffText = adiff.diffText()
        elif adiff.isEdited():
            attr.isEdited = True
            attr.diffText = "Edited"

    if viz_names:
        from . import visualizers
        attr.visualized = visualizers.attrVisName(type_name, name) in viz_names
    else:
        attr.visualized = False

    return attr


def attrIterator(geom: hou.Geometry, type_name: str, debug=False,
                 viz_names: Collection[str] = None,
                 diffs: attrdiffs.Differences = None, full_diff=False
                 ) -> tuple[int, Iterable[Attr]]:
    pref_prec = geom.preferredPrecision()
    attribs = _attribs(geom, type_name, debug=debug)

    if diffs:
        attr_type = general.name_to_attr_type[type_name]
        added_names = diffs.added_names.get(attr_type, ())
    else:
        added_names = None
    iterator = (makeAttr(attrib, type_name, pref_prec, debug=debug,
                         viz_names=viz_names, added_names=added_names,
                         diffs=diffs, full_diff=full_diff)
                for attrib in attribs)

    if diffs and full_diff:
        # Add fake attrs representing deletions
        attr_type = general.name_to_attr_type[type_name]
        del_iter = (Attr.deleted(name) for name
                    in diffs.removed_names.get(attr_type, ()))
        iterator = itertools.chain(iterator, del_iter)

    return len(attribs), iterator


def getAttrs(geom: hou.Geometry, type_name: str, debug=False,
             viz_names: Collection[str] = None,
             diffs: attrdiffs.Differences = None, full_diff=False
             ) -> tuple[int, list[Attr]]:
    count, attrs = attrIterator(geom, type_name, debug=debug,
                                viz_names=viz_names, diffs=diffs,
                                full_diff=full_diff)
    attr_list = list(attrs)
    return count, attr_list


def _computeAttrDiffs(node: hou.Node, geom: hou.Geometry, full_diff: bool
                      ) -> Optional[attrdiffs.Differences]:
    if inputs := node.inputs():
        input_node = inputs[0]
        if _canDiffWith(input_node):
            try:
                input_geom = input_node.geometry()
            except (hou.OperationFailed, hou.NotAvailable):
                return None

            return attrdiffs.computeDiffs(
                input_geom, geom, check_metadata=full_diff,
                compare_values=full_diff
            )


def getGroups(geom: hou.Geometry, group_type: str) -> list[Group]:
    if group_type == "point":
        method = geom.pointGroups
    elif group_type == "primitive":
        method = geom.primGroups
    elif group_type == "vertex":
        method = geom.vertexGroups
    elif group_type == "edge":
        method = geom.edgeGroups
    else:
        raise TypeError(group_type)

    groups: list[Group] = []
    for group in sorted(method(), key=objSortKey):
        g = Group()
        g.name = group.name()
        g.typeName = group_type
        g.count = group.count()
        # EdgeGroup does not have an isOrdered() method
        g.isOrdered = (False if isinstance(group, hou.EdgeGroup)
                       else group.isOrdered())
        groups.append(g)
    return groups


def _annoAttrs(attr_type: hou.attribType, attrs: list[Attr],
               diffs: attrdiffs.Differences, full_diff: bool) -> None:
    added_names = diffs.added_names.get(attr_type, ())
    for attr in attrs:
        name = attr.name
        # Don't annotate hidden attributes
        if name.startswith("."):
            continue

        # Don't do something like attr.isNew = attr.name in added_names here
        # because changing values on Attr fires a signal
        if name in added_names:
            attr.isNew = True
            attr.diffText = "Added"

        if full_diff:
            adiff = diffs.diffs[attr_type, attr.name]
            if adiff.isModified():
                attr.isModified = True
                attr.diffText = adiff.diffText()
            elif adiff.isEdited():
                attr.isEdited = True
                attr.diffText = "Edited"

    if full_diff:
        had_deleted = False
        # Add fake attrs representing deletions
        for name in diffs.removed_names.get(attr_type, ()):
            del_attr = Attr()
            del_attr.name = name
            del_attr.isDeleted = True
            del_attr.diffText = "Deleted"
            attrs.append(del_attr)
            had_deleted = True
        if had_deleted:
            attrs.sort(key=lambda at: general.naturalSortKey(at.name))


def linkify(node: hou.Node, text: str, weight=700) -> str:
    if not node:
        return text

    node_network = node.path() + "/"

    if not text.startswith("<!doctype html>"):
        text = html.escape(text)
        text = text.replace("\n", "<br>")

    # Callback function to pass to Regex.sub()
    def _path_link(match: Match) -> str:
        path = match.group(0)
        try:
            # Node.parent() and Node.parm() can raise permission errors on
            # black box HDAs
            if target := node.node(path):
                parts = path.rsplit("/", 1)
                path = f"<a href='node:{target.path()}'>"
                if len(parts) > 1:
                    pre, stem = parts
                    if pre.startswith(node_network):
                        pre = "./" + pre.removeprefix(node_network)
                    path += f"{pre}/<b style='font-weight: {weight}'>{stem}</b>"
                path += "</a>"
            elif parm_target := node.parm(path):
                parm_name = parm_target.name()
                node_target = parm_target.node()
                node_name = node_target.name()
                if node_target == node:
                    path = f"./<i>{parm_name}</i>"
                else:
                    parent = node_target.parent()
                    pre = ""
                    if parent == node.parent():
                        pre = "../"
                    elif parent:
                        pre = parent.path() + "/"
                        prefix = node.path() + "/"
                        if pre.startswith(prefix):
                            pre = "./" + pre.removeprefix(prefix)
                    node_link = (
                        f"<a href='node:{node_target.path()}'>"
                        f"{pre}"
                        f"<b style='font-weight: {weight}'>{node_name}</b>"
                        f"</a>"
                    )
                    path = f"{node_link}/<i>{parm_name}</i>"
        except hou.PermissionError:
            return path
        return path

    return node_path_expr.sub(_path_link, text)


def _count(label: str, count: int, icon: str = None) -> Number:
    cc = Number()
    cc.name = label
    cc.iconName = icon
    count = cc.value = count
    cc.valueString = f"{int(count):,}"
    return cc


def brectToRect(br: hou.BoundingRect) -> QtCore.QRectF:
    # Working with hou.BoundingRect is a pain
    mn = br.min()
    mx = br.max()
    return QtCore.QRectF(mn[0], mn[1], mx[0] - mn[0], mx[1] - mn[1])


def _vecToStr(vec: tuple[float, float, float]) -> str:
    return ", ".join(str(round(x, 3)) for x in vec)


def xformToString(xform: hou.Matrix4, t: tuple[float, float, float],
                  r: tuple[float, float, float], s: tuple[float, float, float]
                  ) -> str:
    if xform == general.IDENTITY_MATRIX:
        return "Identity"
    return f"t({_vecToStr(t)}) r({_vecToStr(r)}) s({_vecToStr(s)})"


class PerfStats(core.Info):
    profileName: str
    cookCount: int
    cookCountString: str
    cookTotal: float
    cookTotalString: str
    cookSelf: float
    cookSelfString: str
    cookAvg: float
    cookAvgString: str
    cookMin: float
    cookMinString: str
    cookMax: float
    cookMaxString: str
    startFrame: int = 0
    endFrame: int = 0
    perFrame: list[float]
    perFrameNormalized: list[float]

    @staticmethod
    def _findNodeStats(stats: dict[str, Any], node_path: str
                       ) -> Optional[dict[str, Any]]:
        parts = node_path.removeprefix("/").removesuffix("/").split("/")
        if children := stats.get("children"):
            for d in children:
                if d.get("name") == "Nodes":
                    while d and parts:
                        n = parts.pop(0)
                        for kid in d.get("children", ()):
                            if kid.get("name") == n:
                                d = kid
                                continue

                    return d if not parts else None

    @classmethod
    def forPath(cls, node_path: str) -> Optional[PerfStats]:
        from array import array

        profile = hou.perfMon.activeProfile()
        if not profile or profile.isActive():
            return

        if not node_path.startswith("/"):
            raise ValueError(f"Path not absolute: {node_path}")
        d = cls._findNodeStats(profile.stats(), node_path)
        if not d:
            return

        ps = cls()
        # This seems to always return an empty string :(
        ps.profileName = profile.title()

        start, end = hou.playbar.frameRange()
        length = int(end) + 1
        gs: dict[str, float] = d.get("global_stats", {})
        ps.cookCount = 0
        ps.cookTotal = gs.get("Cook", 0.0) / 1000
        ps.cookSelf = gs.get("Cook (Self)", 0.0) / 1000
        ps.cookAvg = gs.get("Cook (Avg)", 0.0) / 1000
        ps.cookMin = gs.get("Cook (Min)", 0.0) / 1000
        ps.cookMax = gs.get("Cook (Max)", 0.0) / 1000

        ps.cookTotalString = formatHMS(ps.cookTotal)
        ps.cookSelfString = formatHMS(ps.cookSelf)
        ps.cookAvgString = formatHMS(ps.cookAvg)
        ps.cookMinString = formatHMS(ps.cookMin)
        ps.cookMaxString = formatHMS(ps.cookMax)

        stats: dict[str, dict[str, dict[str, float]]] = d.get("stats", {})
        start_frame = length
        end_frame = 0
        per_frame = array("f", (0.0 for _ in range(length)))
        for thread_stats in stats.values():
            for frame_num_str, frame_stats in thread_stats.items():
                # It doesn't seem like it works currently, but theoretically
                # the frame numbers could be fractional subframes
                frame = int(float(frame_num_str))
                if frame >= length:
                    continue
                start_frame = min(frame, start_frame)
                end_frame = max(frame, end_frame)
                per_frame[frame] += frame_stats.get("Cook", 0.0) / 1000
                ps.cookCount += frame_stats.get("Cook Count", 0)

        ps.startFrame = start_frame
        ps.endFrame = end_frame
        ps.perFrame = list(per_frame[:end_frame + 1])

        per_frame_max = max(ps.perFrame, default=0.0)
        if per_frame_max > 0:
            ps.perFrameNormalized = [v / per_frame_max for v in ps.perFrame]
        else:
            ps.perFrameNormalized = []

        ps.cookCountString = f"{ps.cookCount:,}"
        return ps


class Message(core.Info):
    original: str
    html: str


class Xform(core.Info):
    isIdentity: bool
    translate: tuple[float, float, float]
    rotate: tuple[float, float, float]
    scale: tuple[float, float, float]
    string: str

    @classmethod
    def fromMatrix(cls, mtx: hou.Matrix4, xord="srt", rord="xyz",
                   pivot: tuple | hou.Vector3 = (0, 0, 0),
                   pivot_rot: tuple | hou.Vector3 = (0, 0, 0)) -> Xform:
        xform = cls()
        if isinstance(pivot, (list, tuple)):
            pivot = hou.Vector3(pivot)
        if isinstance(pivot_rot, (list, tuple)):
            pivot_rot = hou.Vector3(pivot_rot)
        xform.isIdentity = mtx == general.IDENTITY_MATRIX
        xform.translate, xform.rotate, xform.scale = \
            trs(mtx, xord, rord, pivot, pivot_rot)
        xform.string = xformToString(
            mtx, xform.translate, xform.rotate, xform.scale
        )
        return xform


class PathExpansion(core.Info):
    rawText: str
    expandedText: str
    rawHtml: str
    expandedHtml: str


def expandedPath(parm: hou.Parm, theme: colors.Theme) -> PathExpansion:
    pe = PathExpansion()
    raw = pe.rawText = parm.rawValue()
    expanded = pe.expandedText = parm.evalAsString()
    colors = [theme.getColor(nc).name() for nc in general.color_cycle]
    color_map: dict[str, str] = {}
    raw_parts: list[str] = []
    expanded_parts: list[str] = []
    expanded_texts: list[str] = []
    counter = 0
    last_pos = 0
    with hou.ScriptEvalContext(parm):
        for m in var_expr.finditer(raw):
            variable = m.group(0)
            var_name = m.group("braced") or m.group("bare") or m.group("ticked")
            if var_name in color_map:
                color = color_map[var_name]
            else:
                color = color_map[var_name] = colors[counter]
                counter = (counter + 1) % len(colors)
            if m.start() > last_pos:
                previous = raw[last_pos:m.start()]
                raw_parts.append(previous)
                expanded_parts.append(previous)
                expanded_texts.append(previous)
            last_pos = m.end()
            value = hou.text.expandString(variable)
            strike = "; text-decoration: line-through" if not value else ""
            raw_parts.append(
                f"<span style='color:{color}{strike}'>{variable}</span>"
            )
            if value:
                expanded_parts.append(
                    f"<span style='color:{color}'>{value}</span>"
                )
                expanded_texts.append(value)
    if last_pos < len(raw):
        ending = raw[last_pos:]
        raw_parts.append(ending)
        expanded_parts.append(ending)
        expanded_texts.append(ending)

    if expanded == "".join(expanded_texts):
        pe.rawHtml = "".join(raw_parts)
        pe.expandedHtml = "".join(expanded_parts)
    else:
        pe.rawHtml = raw
        pe.expandedHtml = expanded
    return pe


class ObjInfo(core.Info):
    origin: tuple[float, float, float]
    pivot: tuple[float, float, float]
    pivotRotate: tuple[float, float, float]
    xformOrder: str
    rotateOrder: str
    xformOrderString: str
    displayNodeName: str
    renderNodeName: str
    preXform: Xform
    worldXform: Xform
    localXform: Xform

    @classmethod
    def forNode(cls, node: hou.ObjNode, recook: bool = False
                ) -> Optional[ObjInfo]:
        if not isinstance(node, hou.ObjNode):
            return

        output_node = objectOutputNode(node)
        display_node = output_node or node.displayNode()
        render_node = output_node or node.renderNode()

        oi = cls()
        oi.displayNodeName = display_node.name() if display_node else ""
        oi.renderNodeName = render_node.name() if render_node else ""

        # if the node is in error, calling these methods will cause it to
        # recook
        if recook or (node.cookCount() and not node.errors()):
            oi.origin = tuple(node.origin())
            pivot = hou.Vector3(node.parmTuple("p").eval())
            oi.pivot = tuple(pivot)
            pivot_rot = hou.Vector3(node.parmTuple("pr").eval())
            oi.pivotRotate = tuple(pivot_rot)

            xord = node.parm("xOrd").evalAsString()
            oi.xformOrder = " ".join(general.xform_order_codes[char]
                                     for char in xord)
            rord = node.parm("rOrd").evalAsString()
            oi.rotateOrder = " ".join(f"R{char}" for char in rord)
            oi.xformOrderString = oi.xformOrder.replace("Rot", oi.rotateOrder)

            oi.preXform = Xform.fromMatrix(node.preTransform(), xord, rord,
                                           pivot, pivot_rot)
            oi.worldXform = Xform.fromMatrix(node.worldTransform(), xord, rord,
                                             pivot, pivot_rot)
            oi.localXform = Xform.fromMatrix(node.localTransform(), xord, rord,
                                             pivot, pivot_rot)
        return oi


class ChopTrack(core.Info):
    name: str
    color: QtGui.QColor
    sampleCount: int
    current: float
    minimum: float
    maximum: float
    extendLeft: str
    extendRight: str
    override: str


class ChopInfo(core.Info):
    mode: str
    sampleRate: float
    sampleCount: int
    frameCount: int
    duration: float
    durationString: str
    startSample: int
    startFrame: float
    startTime: float
    startTimeString: str
    endSample: int
    endFrame: float
    endTime: float
    endTimeString: str
    tracks: list[ChopTrack]
    minimum: float = 0.0
    maximum: float = 0.0
    minMaxString: str
    hasOverrides: bool = False

    @classmethod
    def forNode(cls, node: hou.ChopNode, output_index: int
                ) -> Optional[ChopInfo]:
        if not isinstance(node, hou.ChopNode):
            return
        clip = node.clip(output_index)

        ci = cls()
        ci.sampleRate = clip.sampleRate()
        ci.sampleCount = clip.numSamples()

        ci.startSample, ci.endSample = clip.sampleRange()
        ci.startFrame = round(clip.samplesToFrame(ci.startSample), 3)
        ci.startTime = round(clip.samplesToTime(ci.startSample), 3)
        ci.startTimeString = formatHMS(ci.startTime)
        ci.endFrame = round(clip.samplesToFrame(ci.endSample), 3)
        ci.endTime = round(clip.samplesToTime(ci.endSample), 3)
        ci.endTimeString = formatHMS(ci.endTime)

        ci.frameCount = int(ci.endFrame - ci.startFrame + 1)
        ci.duration = round(ci.frameCount / ci.sampleRate, 3)
        ci.durationString = formatHMS(ci.duration)
        ci.mode = clip.mode().name()

        ci.tracks = []
        for track in clip.tracks():
            name = track.name()
            tr = ChopTrack()
            tr.name = name
            tr.sampleCount = track.numSamples()
            tr.current = round(track.eval(), 4)
            tr.extendLeft = track.extendLeft().name()
            tr.extendRight = track.extendRight().name()
            tr.color = QtGui.QColor.fromRgbF(*track.color().rgb())
            samples = track.allSamples()
            tr.minimum = round(min(samples, default=0.0), 4)
            ci.minimum = min(ci.minimum, tr.minimum)
            tr.maximum = round(max(samples, default=0.0), 4)
            ci.maximum = max(ci.maximum, tr.maximum)
            override_parm = track.overrideParm()
            tr.override = override_parm.path() if override_parm else ""
            ci.hasOverrides = ci.hasOverrides or bool(override_parm)
            ci.tracks.append(tr)

        return ci


class Attr(core.Info):
    name: str
    typeName: str
    dataType: str
    size: int
    dataTypeName: str
    dataTypeDesc: str
    dataTypeColor: QtGui.QColor
    qualifier: str
    qualifierBrief: str
    stringCount: int = 0
    visualized: bool = False
    isNew: bool = False
    isModified: bool = False
    isEdited: bool = False
    isDeleted: bool = False
    diffText: str
    debugDataID: int

    @classmethod
    def deleted(cls, name: str) -> Self:
        del_attr = cls()
        del_attr.name = name
        del_attr.isDeleted = True
        del_attr.diffText = "Deleted"
        return del_attr


class Group(core.Info):
    name: str
    typeName: str
    count: int
    isOrdered: bool
    visualized: bool


class VolumeInfo(core.Info):
    name: str
    primNum: int
    typeName: str
    resolution: tuple[int, ...]
    resolutionStr: str
    voxelSize: tuple[float, ...]
    voxelSizeStr: str
    visMode: str
    isHeightfield: bool
    isSDF: bool
    dataTypeName: str
    isEmpty: bool
    voxelCount: int
    voxelCountStr: str
    background: float
    hasBackground: bool = False
    bandwidth: float
    hasBandwidth: bool = False
    # memory: int
    # memoryString: str
    visualized: bool

    def setResolution(self, resolution: hou.Vector3) -> None:
        self.resolution = tuple(resolution)
        self.resolutionStr = "\xD7".join(formatNum(x) for x in resolution)

    def setVoxelCount(self, count: int) -> None:
        self.voxelCount = count
        self.voxelCountStr = f"{count:,}"

    def setVoxelSize(self, size: hou.Vector3) -> None:
        self.voxelSize = tuple(size)
        self.voxelSizeStr = formatVoxelSize(size)

    @classmethod
    def fromDopData(cls, name: str, data: hou.DopData) -> VolumeInfo:
        vol = cls()
        opts = data.options()
        vol.name = name
        vol.primNum = 0
        vol.typeName = "Grid"
        vol.isEmpty = False

        res = opts.field("div")
        vol.setResolution(res)
        vol.setVoxelCount(opts.field("totalvoxels"))

        is_16b = opts.field("usefp16")
        is_vector = data.dataType() == "SIM_VectorField"

        if is_vector and is_16b:
            vol.dataTypeName = "vec3(fp16)"
        elif is_vector:
            vol.dataTypeName = "vec3"
        elif is_16b:
            vol.dataTypeName = "fp16"
        else:
            vol.dataTypeName = "float"

        div_size = opts.field("divsize")
        if isinstance(div_size, float):
            div_size = hou.Vector3((div_size, div_size, div_size))
        vol.setVoxelSize(div_size)

        return vol

    @classmethod
    def fromPrim(cls, prim: hou.Volume, name: str = None) -> VolumeInfo:
        vol = cls()

        if not name:
            try:
                name = prim.stringAttribValue("name")
            except hou.OperationFailed:
                # No 'name' attribute on the volume
                name = ""
        vol.name = name
        vol.primNum = prim.number()
        vol.setResolution(prim.resolution())
        voxel_size = prim.voxelSize()
        largest_dim: float = max(*voxel_size)
        vol.setVoxelSize(voxel_size)
        vol.isHeightfield = prim.isHeightField()
        vol.isSDF = prim.isSDF()

        vol.background = 0.0
        vol.bandwidth = 0.0
        vol.hasBackground = False
        vol.hasBandwidth = False

        vol.visMode = prim.intrinsicValue("volumevisualmode")
        prim_type = prim.type()
        if prim_type == hou.primType.VDB:
            vol.typeName = "VDB"
            vol.isEmpty = prim.isEmpty()
            vol.setVoxelCount(prim.activeVoxelCount())
            vol.dataTypeName = prim.intrinsicValue("vdb_value_type")
            if prim.intrinsicValue("vdb_is_saved_as_half_float"):
                vol.dataTypeName += "16"

            background = prim.intrinsicValue("background")
            if isinstance(background, (tuple, list)):
                vol.background = tuple(round(v, 5) for v in background)
            else:
                vol.background = round(background, 5)
            vol.hasBackground = True
            if vol.isSDF:
                vol.bandwidth = round(vol.background / largest_dim, 1)
                vol.hasBandwidth = True
        else:
            vol.typeName = "Grid"
            vol.isEmpty = False
            vol.setVoxelCount(vol.resolution[0] * vol.resolution[1] *
                              vol.resolution[2])

            storage_type = prim.storageType()
            channel_count = prim.channelCount()
            if channel_count == 1 and \
                    storage_type == hou.volumeStorageType.Float:
                vol.dataTypeName = "float"
            elif channel_count == 2 and \
                    storage_type == hou.volumeStorageType.Int:
                vol.dataTypeName = "int"
            else:
                vol.dataTypeName = f"vec{channel_count}"

        # vol.memory = prim.intrinsicValue("memoryusage")
        # vol.memoryString = mem_formatter.formatNumber(vol.memory).html()

        return vol


class GeometryMemory(core.Info):
    full: str
    isInstanced: bool
    new: str
    unique: str

    @classmethod
    def fromString(cls, geo_memory: str) -> GeometryMemory:
        match = geo_memory_expr.match(geo_memory)
        mi = cls()
        mi.full = match.group("full").strip()
        mi.isInstanced = bool(match.group("inst"))
        mi.new = (match.group("new") or "").strip()
        mi.unique = (match.group("unique") or "").strip()
        return mi


class Number(core.Info):
    name: str = ""
    iconName: str = ""
    value: float = 0
    valueString: str = ""
    fraction: float = 0.0
    color: QtGui.QColor


class DonutItem(core.Info):
    name: str
    text: str
    value: float
    color: QtGui.QColor


class GeoInfo(core.Info):
    sessionID: int
    outputIndex: int = 0
    isSop: bool = False
    bboxSize: list[float]
    bboxCenter: list[float]
    bboxMin: list[float]
    bboxMax: list[float]
    primCount: int = 0
    primCountString: str
    pointCount: int = 0
    pointCountString: str
    vertexCount: int = 0
    vertexCountString: str
    componentCounts: list[Number]
    primTypeCounts: list[Number]

    detailAttrs: list[Attr]
    primitiveAttrs: list[Attr]
    pointAttrs: list[Attr]
    vertexAttrs: list[Attr]
    # detailAttrs: InfoListModel[Attr, str]
    # primitiveAttrs: InfoListModel[Attr, str]
    # pointAttrs: InfoListModel[Attr, str]
    # vertexAttrs: InfoListModel[Attr, str]
    canShowDiffs: bool = False

    detailAttrCount: int = 0
    primitiveAttrCount: int = 0
    pointAttrCount: int = 0
    vertexAttrCount: int = 0

    primGroups: list[Group]
    pointGroups: list[Group]
    vertexGroups: list[Group]
    edgeGroups: list[Group]
    memory: GeometryMemory
    volumes: list[VolumeInfo]

    debugChangeCount: int = -1
    debugDetailID: int = -1
    debugPrimID: int = -1
    debugTopologyID: int = -1
    # Don't use "Changed" at the end because that's already the name of the
    # changed signal for the corresponding properties above
    debugChangeCountAltered: bool = False
    debugDetailIDAltered: bool = False
    debugPrimIDAltered: bool = False
    debugTopologyIDAltered: bool = False

    _exclude_from_eq = (
        "debugChangeCountAltered", "debugDetailIDAltered", "debugPrimIDAltered",
        "debugTopologyIDAltered"
    )
    _debug_attrs = (
        "debugChangeCount", "debugDetailID", "debugPrimID", "debugTopologyID"
    )
    _exclude_from_update = (
        "debugChangeCountAltered", "debugDetailIDAltered", "debugPrimIDAltered",
        "debugTopologyIDAltered"
    )

    def __init__(self, session_id: int = -1):
        super().__init__(sessionID=session_id)
        self._fillers: dict[int, models.BatchFiller[Attr, str]] = {}

    def _update(self, other: Self) -> None:
        is_same_node = self.sessionID == other.sessionID
        for name in core.fieldNames(other):
            # Don't copy over *Altered attrs
            if name in self._exclude_from_update:
                continue

            a = getattr(self, name)
            b = getattr(other, name)

            if name in self._debug_attrs:
                # If this is the same node as last time, and the
                setattr(self, f"{name}Altered",
                        is_same_node and -1 < a != b)

            if a != b:
                setattr(self, name, b)

    def node(self) -> Optional[hou.Node]:
        return hou.nodeBySessionId(self.sessionID)

    def isEmpty(self) -> bool:
        return self.primCount + self.pointCount == 0

    def fillCommonGeoInfo(self, geom: hou.Geometry) -> None:
        bbox = geom.boundingBox()
        self.bboxSize = [round(x, 5) for x in bbox.sizevec()]
        self.bboxCenter = [round(x, 5) for x in bbox.center()]
        self.bboxMin = [round(x, 5) for x in bbox.minvec()]
        self.bboxMax = [round(x, 5) for x in bbox.maxvec()]

        component_counts: list[Number] = []
        for name, intrinsic, field, str_field in (
                ("Points", "pointcount", "pointCount", "pointCountString"),
                ("Primitives", "primitivecount", "primCount",
                 "primCountString"),
                ("Vertices", "vertexcount", "vertexCount",
                 "vertexCountString")):
            cc = Number()
            cc.name = name
            cc.value = geom.intrinsicValue(intrinsic)
            cc.valueString = f"{int(cc.value):,}"
            component_counts.append(cc)
            setattr(self, field, cc.value)
            setattr(self, str_field, cc.valueString)

        self.componentCounts = component_counts

        prim_type_counts: list[Number] = []

        def addCount(name: str, label: str, count: int, icon: str) -> None:
            label = general.prim_type_renames.get(name, label)
            icon = icon or "PRIMITIVES_unknown"
            if count:
                cc = _count(label, count, icon)
                prim_type_counts.append(cc)

        for name, label, icon in zip(geom.primTypeNames(),
                                     geom.primTypeLabels(),
                                     geom.primTypeIcons()):
            addCount(name, label, geom.countPrimType(name), icon)
        # Even though it doesn't make pure logical sense, we display
        # "unconnected points" as a prim type count
        addCount("", general.UNUSED_POINTS_LABEL,
                 geom.countUnusedPoints(), "PRIMITIVES_points")
        # print("Count prims", perf_counter() - t)

        self.primTypeCounts = prim_type_counts

    def fillDetails(self, geom: hou.Geometry, debug=False, highlight_new=False,
                    full_diff=False) -> None:
        self.fillAttrs(geom, debug, highlight_new=highlight_new,
                       full_diff=full_diff)
        self.fillGroups(geom, debug)
        self.fillVolumes(geom)

    def _fillModels(self, geom: hou.Geometry, viz_names: Collection[str] = None,
                    debug=False, diffs: attrdiffs.Differences = None,
                    full_diff=False) -> None:
        for type_name in ("detail", "primitive", "point", "vertex"):
            model_name = f"{type_name}Attrs"
            count_name = f"{type_name}AttrCount"
            model: models.InfoListModel[Attr, str] = getattr(self, model_name)
            if model:
                model.clear()
            else:
                model = models.InfoListModel[Attr, str]("name", Attr)
                setattr(self, model_name, model)

            count, source = attrIterator(geom, type_name, debug=debug,
                                         viz_names=viz_names,
                                         diffs=diffs, full_diff=full_diff)
            setattr(self, count_name, count)

            if count > general.BATCH_SIZE:
                filler: models.BatchFiller[Attr, str] = models.BatchFiller(
                    model, source, batch_size=general.BATCH_SIZE,
                    parent=self
                )
                filler.finished.connect(self._onFillerFinished)
                self._fillers[id(filler)] = filler
                filler.start()
            else:
                model.resetItems(source)

    def _onFillerFinished(self) -> None:
        filler = self.sender()
        self._fillers.pop(id(filler), None)
        filler.deleteLater()

    def stopBackgroundActivity(self) -> None:
        for ident, filler in list(self._fillers.items()):
            filler.stop()
            self._fillers.pop(ident, None)

    def updateVisualizers(self) -> None:
        from . import visualizers
        viz_names = visualizers.visualizerNames()
        for model in (self.detailAttrs, self.primitiveAttrs, self.pointAttrs,
                      self.vertexAttrs):
            for attr in model:
                viz_name = visualizers.attrVisName(attr.typeName, attr.name)
                attr.visualized = viz_name in viz_names

    def fillAttrs(self, geom: hou.Geometry, debug=False,
                  highlight_new=False, full_diff=False) -> None:
        from . import visualizers
        viz_names = visualizers.visualizerNames()
        self.canShowDiffs = ((node := self.node()) and
                             (inputs := node.inputs()) and
                             _canDiffWith(inputs[0]))
        diffs: Optional[attrdiffs.Differences] = None
        if (highlight_new or full_diff) and (node := self.node()):
            diffs = _computeAttrDiffs(node, geom, full_diff)

        # self._fillModels(geom, debug=debug, diffs=diffs,
        #                  full_diff=full_diff, viz_names=viz_names)

        self.detailAttrCount, self.detailAttrs = getAttrs(
            geom, "detail", debug=debug, viz_names=viz_names, diffs=diffs,
            full_diff=full_diff
        )
        self.primitiveAttrCount, self.primitiveAttrs = getAttrs(
            geom, "primitive", debug=debug, viz_names=viz_names, diffs=diffs,
            full_diff=full_diff
        )
        self.pointAttrCount, self.pointAttrs = getAttrs(
            geom, "point", debug=debug, viz_names=viz_names, diffs=diffs,
            full_diff=full_diff
        )
        self.vertexAttrCount, self.vertexAttrs = getAttrs(
            geom, "vertex", debug=debug, viz_names=viz_names, diffs=diffs,
            full_diff=full_diff
        )

    def fillGroups(self, geom: hou.Geometry, debug=False) -> None:
        self.primGroups = getGroups(geom, "primitive")
        self.pointGroups = getGroups(geom, "point")
        self.vertexGroups = getGroups(geom, "vertex")
        self.edgeGroups = getGroups(geom, "edge")

    def fillVolumes(self, geom: hou.Geometry) -> None:
        vols: list[VolumeInfo] = []
        for i, prim_type in enumerate((hou.primType.Volume, hou.primType.VDB)):
            for prim in geom.iterPrimsOfType(prim_type):
                vol = VolumeInfo.fromPrim(prim)
                vols.append(vol)
        self.volumes = vols

    @QtCore.Slot(bool, bool, bool)
    def recomputeAttrs(self, highlight_new: bool, full_diff: bool, debug: bool
                       ) -> None:
        if node := self.node():
            geom = node.geometry(self.outputIndex)
            self.fillAttrs(geom, debug, highlight_new=highlight_new,
                           full_diff=full_diff)

    @classmethod
    def forNode(cls, node: hou.SopNode | hou.DopNode,
                geo_tree: Optional[hou.NodeInfoTree],
                output_index: int, highlight_new=True, full_diff=False,
                debug: bool = False) -> Optional[GeoInfo]:
        if not isinstance(node, hou.SopNode):
            return None
        if not node.cookCount():
            return None
        if node.errors():
            return None

        gi = cls(node.sessionId())
        gi.outputIndex = output_index
        gi.isSop = isinstance(node, hou.SopNode)

        if isinstance(node, hou.SopNode) and geo_tree:
            # No way to get SOP memory usage from HOM yet
            geo_mem = core.findInfoRow(geo_tree, "Memory")
            if geo_mem:
                gi.memory = GeometryMemory.fromString(geo_mem)

        if not node.errors():
            geom = node.geometry(output_index)
            if geom:
                from . import visualizers
                gi.fillCommonGeoInfo(geom)
                gi.fillDetails(geom, debug, highlight_new=highlight_new,
                               full_diff=full_diff)
                visualizers.updateVolumeStates(gi.volumes)

                if debug:
                    gi.debugChangeCount = geom.modificationCounter()
                    gi.debugDetailID = getDataId(geom.vexAttribDataId())
                    gi.debugPrimID = \
                        getDataId(geom.primitiveIntrinsicsDataId())
                    gi.debugTopologyID = getDataId(geom.topologyDataId())
        return gi


class SimGeo(core.Info):
    name: str
    geometry: GeoInfo


class SimObject(core.Info):
    path: str
    dataType: str
    geometries: list[SimGeo]
    volumes: list[VolumeInfo]
    created: bool = False

    @classmethod
    def forData(cls, dop_obj: hou.DopObject) -> SimObject:
        so = cls()
        so.path = dop_obj.path()
        so.dataType = dop_obj.dataType()

        geometries: list[SimGeo] = []
        fields: list[VolumeInfo] = []
        for name, subdata in dop_obj.subData().items():
            dtype: str = subdata.dataType()
            if dtype in ("SIM_VectorField", "SIM_ScalarField"):
                fields.append(VolumeInfo.fromDopData(name, subdata))
            elif geom := dop_obj.geometry(name):
                gi = GeoInfo()
                gi.fillCommonGeoInfo(geom)
                gi.fillAttrs(geom, debug=False)
                gi.fillGroups(geom, debug=False)
                sg = SimGeo(name=name, geometry=gi)
                geometries.append(sg)
            # elif vol_geom := dop_obj.fieldGeometry(name):
            #     for prim in vol_geom.prims():
            #         if isinstance(prim, hou.Volume):
            #             name = prim.stringAttribValue("name") or name
            #             fields.append(VolumeInfo.fromPrim(prim, name))
        so.geometries = geometries
        so.volumes = fields
        return so


class DopInfo(core.Info):
    simMemoryUsage: int
    simMemoryString: str
    lastTimestepFrame: float
    processedObjectCount: int
    objects: list[SimObject]
    changedOptions: list[LabeledValue]

    @classmethod
    def forNode(cls, node: hou.DopNode) -> Optional[DopInfo]:
        if not isinstance(node, hou.DopNode):
            return None

        sim = node.simulation()
        di = cls()
        di.simMemoryUsage = sim.memoryUsage()
        # di.simMemoryString = \
        #     mem_formatter.formatNumber(di.simMemoryUsage).html()

        seen_ids = set()
        object_infos: list[SimObject] = []
        for obj in node.createdObjects():
            oi = SimObject.forData(obj)
            oi.created = True
            object_infos.append(oi)
            seen_ids.add(obj.objid())
        for obj in node.processedObjects():
            if obj.objid() in seen_ids:
                continue
            object_infos.append(SimObject.forData(obj))
        di.objects = object_infos
        di.processedObjectCount = len(di.objects)
        di.lastTimestepFrame = hou.timeToFrame(sim.time())

        changed_opts: list[LabeledValue] = []
        for key, value in node.lastCookContextOptions().items():
            lv = LabeledValue()
            lv.label = key
            if isinstance(value, float) and value.is_integer():
                value = int(value)
            lv.text = str(value)
            changed_opts.append(lv)
        di.changedOptions = changed_opts

        return di


class UsdPrimCountCategory(core.Info):
    label: str
    counts: list[Number]
    total: int
    totalString: str


class UsdPrimInfo(core.Info):
    path: str
    typeName: str
    kindName: str
    iconName: str

    @classmethod
    def _primIcon(self, prim: Usd.Prim) -> str:
        if prim.IsModel() or prim.IsSubComponent():
            kind = prim.GetKind()
            return f"SCENEGRAPH_kind_{kind.lower()}"
        else:
            return usdPrimIcon(prim.GetTypeName())

    @classmethod
    def forPrim(cls, prim: Usd.Prim) -> Self:
        pi = cls()
        pi.path = str(prim.GetPath())
        # In case the prim doesn't exist, handle exceptions.
        try:
            pi.typeName = prim.GetTypeName()
            pi.kindName = prim.GetKind().capitalize()
        except:
            pi.typeName = "Unknown"
            pi.kindName = "Unknown"
        # Protect against issues in the prim icon calculation code.
        try:
            pi.iconName = cls._primIcon(prim)
        except:
            pi.iconName = "SCENEGRAPH_primtype_unknown"
        return pi

    @classmethod
    def forObject(cls, obj: Usd.Object) -> Self:
        # Alternate constructor for collection members, where the object is
        # probably a prim, but could be an Attribute or Relationship, in which
        # case we only fill in some fields
        if isinstance(obj, Usd.Prim):
            return cls.forPrim(obj)
        else:
            pi = cls()
            pi.path = str(obj.GetPath())
            if isinstance(obj, Usd.Attribute):
                pi.typeName = "Attribute"
            elif isinstance(obj, Usd.Relationship):
                pi.typeName = "Relationship"
            return pi


class UsdPrimCounts(core.Info):
    categorizedPrimCounts: list[UsdPrimCountCategory]
    modelKindCounts: list[Number]
    modelTotal: int = 0
    modelTotalString: str
    subcomponentKindCounts: list[Number]
    subcomponentTotal: int = 0
    subcomponentTotalString: str
    otherKindCounts: list[Number]
    otherTotal: int = 0
    otherTotalString: str
    geometryCounts: list[Number]
    countTimeoutMs: int = 1000
    countsTimedOut: bool = False

    def fillFrom(self, node: hou.LopNode, output_index: int = 0, *,
                 prim_path: str = None, lop_geo_details=False,
                 kind_details=True, timeout_ms=1000) -> None:
        self.countTimeoutMs = timeout_ms
        try:
            with hou.InterruptableOperation("Counting USD prims",
                                            timeout_ms=timeout_ms):
                stats_dict: dict[str, int] = \
                    node.stagePrimStats(
                        prim_path,
                        output_index=output_index,
                        do_kind_counts=kind_details,
                        do_geometry_counts=lop_geo_details,
                        include_root=False,
                    )
        except hou.OperationInterrupted:
            self.countsTimedOut = True
            return
        else:
            self.countsTimedOut = False

        # Categorized prim counts
        categorized: dict[str, list[Number]] = {}
        # Kind counts are mixed in with the prim counts (ugh) so pull them out
        # before we process those
        if kind_details:
            model_kind_counts: list[Number] = []
            subcomp_kind_counts: list[Number] = []
            other_kind_counts: list[Number] = []
            for kind in sorted(Kind.Registry.GetAllKinds()):
                key = f"Kind:{kind}:Total"
                if key in stats_dict:
                    count = stats_dict.pop(key)
                    kc = Number(name=kind, value=count,
                                valueString=f"{count:,}")
                    kc.iconName = usdprimicons.getIconForKind(kind)
                    if Kind.Registry.IsModel(kind):
                        model_kind_counts.append(kc)
                    elif Kind.Registry.IsSubComponent(kind):
                        subcomp_kind_counts.append(kc)
                    else:
                        other_kind_counts.append(kc)
            # Find unknown kinds
            for key in list(stats_dict):
                if key.startswith("Kind:"):
                    count = stats_dict.pop(key)
                    if not key.endswith(":Total"):
                        continue
                    kind = key.removeprefix("Kind:").removesuffix(":Total")
                    if kind in ("model", "subcomponent"):
                        continue
                    kc = Number(name=kind, value=count,
                                valueString=f"{count:,}")
                    kc.iconName = f"SCENEGRAPH_primtype_unknown"
                    other_kind_counts.append(kc)
            self.modelKindCounts = model_kind_counts
            self.modelTotal = sum(kc.value for kc in model_kind_counts)
            self.modelTotalString = f"{self.modelTotal:,}"
            self.subcomponentKindCounts = subcomp_kind_counts
            self.subcomponentTotal = sum(kc.value for kc in subcomp_kind_counts)
            self.subcomponentTotalString = f"{self.subcomponentTotal:,}"
            self.otherKindCounts = other_kind_counts
            self.otherTotal = sum(kc.value for kc in other_kind_counts)
            self.otherTotalString = f"{self.otherTotal:,}"

        # Geometry counts are mixed in with the prim counts (ugh) so pull them
        # out before we process those
        if lop_geo_details:
            geo_counts = []
            for key in list(stats_dict):
                if m := lop_geo_count_key_expr.match(key):
                    count = stats_dict.pop(key)
                    prim_name = m.group(1)
                    geo_type = m.group(2)
                    gc = Number(name=geo_type, value=count,
                                valueString=f"{count:,}")
                    gc.iconName = usdPrimIcon(prim_name)
                    geo_counts.append(gc)
            self.geometryCounts = geo_counts

        lookup = general.getUsdPrimCategoryLookup()
        for prim_name, count in stats_dict.items():
            prim_name = prim_name.removesuffix(":Total")
            prim_label = general.prim_count_renames.get(prim_name, prim_name)
            ci = Number(name=prim_label, value=count, valueString=f"{count:,}")
            ci.iconName = usdPrimIcon(prim_name)
            cat = lookup.get(prim_name, "Other")
            if cat in categorized:
                categorized[cat].append(ci)
            else:
                categorized[cat] = [ci]

        # Make sure to build the list using the order in usd_prim_categories
        cat_list: list[UsdPrimCountCategory] = []
        for cat_name in general.usd_categories:
            count_list = categorized.get(cat_name)
            if count_list:
                # Sort the counts by the name order in the usd_categories dict
                count_list.sort(
                    key=countListKeyFn(general.usd_categories[cat_name])
                )

                cat = UsdPrimCountCategory(label=cat_name, counts=count_list)
                cat.total = sum(c.value for c in count_list)
                cat.totalString = f"{cat.total:,}"
                cat_list.append(cat)
        self.categorizedPrimCounts = cat_list


class LopInfo(core.Info):
    sessionID: int
    outputIndex: int = 0
    layerCount: int = 0
    layerCountString: str
    loadedPaths: list[str]
    populatedPaths: list[str]
    mutedLayers: list[str]
    counts: UsdPrimCounts
    modifiedPrims: list[UsdPrimInfo]

    def __init__(self, session_id: int = -1):
        super().__init__(sessionID=session_id)

    def node(self) -> Optional[hou.Node]:
        return hou.nodeBySessionId(self.sessionID)

    def fillPrimCounts(self, node: hou.LopNode, output_index: int, *,
                       lop_geo_details=False, kind_details=True,
                       timeout_ms=1000) -> None:
        if self.counts:
            pcs = self.counts
        else:
            pcs = self.counts = UsdPrimCounts()
        pcs.fillFrom(node, output_index, lop_geo_details=lop_geo_details,
                     kind_details=kind_details, timeout_ms=timeout_ms)

    @QtCore.Slot(bool, int)
    def recomputeCounts(self, lop_geo_details: bool, timeout_ms=1000) -> None:
        if node := self.node():
            self.fillPrimCounts(node, self.outputIndex,
                                lop_geo_details=lop_geo_details,
                                timeout_ms=timeout_ms)

    @classmethod
    def forNode(cls, node: hou.LopNode, output_index: int,
                lop_geo_details=False) -> Optional[LopInfo]:
        if not isinstance(node, hou.LopNode):
            return
        li = cls(node.sessionId())
        li.modifiedPrims = []
        li.outputIndex = output_index
        # This can bump the cook count
        if stage := node.stage():
            # It only makes sense to try getting other info if stage() succeeded
            li.layerCount = node.sourceLayerCount(output_index=output_index)
            masks = node.loadMasks()
            li.layerCountString = f"{li.layerCount:,}"
            li.loadedPaths = None if masks.loadAll() else masks.loadPaths()
            li.populatedPaths = (None if masks.populateAll()
                                 else masks.populatePaths())
            li.mutedLayers = masks.muteLayers()

            if stage:
                for path in sorted(node.lastModifiedPrims()):
                    prim = stage.GetPrimAtPath(path)
                    pi = UsdPrimInfo.forPrim(prim)
                    li.modifiedPrims.append(pi)

            li.fillPrimCounts(node, output_index,
                              lop_geo_details=lop_geo_details)
        return li


class TopInfo(core.Info):
    filtered: bool = False
    filterNodes: list[NodeReference]
    stateName: str
    pdgNodeName: str
    count: int = 0
    canceledCount: int = 0
    failedCount: int = 0
    successCount: int = 0
    cachedCount: int = 0
    cookingCount: int = 0
    dirtyCount: int = 0
    scheduledCount: int = 0
    uncookedCount: int = 0
    waitingCount: int = 0
    dirtyTotal: int = 0
    finishedTotal: int = 0
    cookingTotal: int = 0
    successful: bool = False
    failed: bool = False
    finishedFraction: float = 0.0
    cookingFraction: float = 0.0
    failedFraction: float = 0.0
    totalGenerateTime: float = 0.0
    totalGenerateTimeStr: str = ""
    totalCookTime: float = 0.0
    totalCookTimeStr: str = ""
    averageCookTime: float = 0.0
    averageCookTimeStr: str = ""
    timeSlices: list[float]
    serviceTypeNames: list[str]
    isStandardScript: bool = False
    jobCommand: str
    outputFiles: list[tops.FileInfo]
    outputFilesText: str = ""

    attributes: tops.PdgAttrProxyModel
    attributeCount: int = 0
    maxAttrNameChars: int = 0
    maxAttrCountChars: int = 0

    @classmethod
    def forNode(cls, node: hou.TopNode, output_index: Optional[int]
                ) -> Optional[TopInfo]:
        import pdg
        from hutil.qt.info import tops

        if not isinstance(node, hou.TopNode):
            return None

        show_global = node.type().isManager()
        output_index = output_index or 0

        ti = cls()
        if node.isSubNetwork():
            outputs = node.subnetOutputs()
            if output_index < len(outputs):
                node = outputs[output_index]

        ti.stateName = node.getCookState(False).name()

        ti.filtered = node.isFilterOn()
        if ti.filtered:
            ti.filterNodes = [NodeReference.forNode(n)
                              for n in node.getFilterNodes()]
        else:
            ti.filterNodes = []

        ti.attributes = tops.PdgAttrProxyModel()

        # workItemStates() returns () if the node is bypassed (bug #155626)
        item_states = node.workItemStates() or (0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)
        ti.count = item_states[-1]
        ti.canceledCount = item_states[pdg.workItemState.CookedCancel]
        ti.failedCount = item_states[pdg.workItemState.CookedFail]
        ti.successCount = item_states[pdg.workItemState.CookedSuccess]
        ti.cachedCount = item_states[pdg.workItemState.CookedCache]
        ti.cookingCount = item_states[pdg.workItemState.Cooking]
        ti.dirtyCount = item_states[pdg.workItemState.Dirty]
        ti.scheduledCount = item_states[pdg.workItemState.Scheduled]
        ti.uncookedCount = item_states[pdg.workItemState.Uncooked]
        ti.waitingCount = item_states[pdg.workItemState.Waiting]

        ti.dirtyTotal = ti.dirtyCount + ti.uncookedCount
        ti.finishedTotal = ti.successCount + ti.cachedCount
        ti.cookingTotal = ti.scheduledCount + ti.cookingCount

        if ti.count > 0:
            ti.finishedFraction = ti.finishedTotal / ti.count
            ti.cookingFraction = ti.cookingTotal / ti.count
            ti.failedFraction = ti.failedCount / ti.count

        ti.failed = ti.failedCount > 0
        ti.successful = ti.finishedTotal == ti.count and ti.count > 0

        pdg_context = node.getPDGGraphContext()
        if pdg_graph := pdg_context.graph:
            pdg_node_name = ti.pdgNodeName = node.getPDGNodeName()
            if pdg_node := pdg_graph.node(pdg_node_name):
                stats = pdg_node.stats()
                ti.totalGenerateTime = \
                    stats.stat(pdg.nodeStatType.TotalGenerateTime)
                ti.totalGenerateTimeStr = formatHMS(ti.totalGenerateTime,
                                                    zero="--")
                ti.totalCookTime = stats.stat(pdg.nodeStatType.TotalCookTime)
                ti.totalCookTimeStr = formatHMS(ti.totalCookTime, zero="--")
                if ti.count > 0:
                    ti.averageCookTime = ti.totalCookTime / ti.count
                else:
                    ti.averageCookTime = 0.0
                ti.averageCookTimeStr = formatHMS(ti.averageCookTime, zero="--")

                script_info = pdg_node.scriptInfo
                ti.serviceTypeNames = script_info.serviceTypeNames
                ti.isStandardScript = script_info.isStandardScript
                ti.jobCommand = script_info.command

                if show_global:
                    model: PdgAttributeModel = ti.attributes.sourceModel()
                    model.setAttrsFrom(pdg_graph, force=False)
                    ti.attributeCount = model.rowCount()
                    ti.maxAttrNameChars = model.maxAttrNameChars
                    ti.maxAttrCountChars = model.maxAttrCountChars
                else:
                    ti.attributeCount = 0

                hip_dir = pathlib.Path(hou.hipFile.path()).parent
                ti.outputFiles = [tops.FileInfo.fromPdgFile(f, hip_dir=hip_dir)
                                  for f in pdg_node.outputFiles(True)]
                ti.outputFilesText = "\n".join(fi.path for fi in ti.outputFiles)

        return ti


class CopLayer(core.Info):
    outputIndex: int
    name: str
    label: str
    valid: bool = True
    resolution: QtCore.QSize
    resolutionString: str
    oddResolution: bool = False
    dataRect: QtCore.QRect
    dataRectString: str
    dataIsOffset: bool
    dataIsSized: bool
    displayRect: QtCore.QRect
    displayRectString: str
    displayIsOffset: bool
    displayIsSized: bool
    onGPU: bool
    onCPU: bool
    storageTypeName: str
    isInt: bool  # Can't call this isInteger, that's a JavaScript method name
    storageTypeBriefName: str
    borderTypeName: str
    dataType: str
    visualizationName: str
    isConstant: bool
    channelCount: int
    pixelScale: list[float]
    pixelScaleString: str
    aspectRatio: float
    clipNear: float
    clipFar: float
    projectionName: str
    aperture: float
    focalLength: float
    cameraPos: list[float]
    imageDistance: float
    fStop: float
    focusDistance: float
    cameraIsDefault: bool = True
    attributes: list[LabeledValue]
    imageXform: Xform
    geometry: GeoInfo
    isVDB: bool = False
    leafCount: int = 0
    voxelCount: int = 0
    voxelCountStr: str = ""
    voxelSize: tuple[float, float, float]
    voxelSizeStr: str = ""
    voxelResolution: tuple[int, ...]
    voxelResolutionStr: str
    isAttrib: bool = False
    length: int = 0
    lengthStr: str = ""
    tupleSize: int = 0
    owner: str = ""
    thumbnail: QtGui.QPixmap
    subWires: list[CopCableWire]

    @staticmethod
    def rectToString(rect: QtCore.QRect) -> str:
        return \
            f"@{rect.x():,},{rect.y():,} {rect.width():,}\xd7{rect.height():,}"

    @staticmethod
    def sizeToString(size: QtCore.QSizeF | QtCore.QSize) -> str:
        if size:
            return f"{size.width():,} \xd7 {size.height():,}"
        else:
            return ""

    def setAttrs(self, attrs: Mapping[str, str]) -> None:
        self.attributes = [LabeledValue(label=k, text=str(v))
                           for k, v in sorted(attrs.items())]

    def fillFromVDB(self, vdb: hou.NanoVDB) -> None:
        self.isVDB = True
        self.onGPU = vdb.onGPU()
        self.onCPU = vdb.onCPU()
        self.visualizationName = vdb.typeInfo().name()

        size = self.voxelSize = tuple(vdb.voxelSize())
        self.voxelSizeStr = formatVoxelSize(size)

        self.leafCount = vdb.leafCount()
        self.voxelCount = self.leafCount * 512
        self.voxelCountStr = f"{self.voxelCount:,}"
        self.voxelResolution = vdb.resolution()
        self.voxelResolutionStr = "\xD7".join(formatNum(x) for x in self.voxelResolution)

        self.setAttrs(vdb.properties())

    def fillFromAttribute(self, attrib: hou.DetachedAttrib) -> None:
        self.isAttrib = True
        self.onGPU = attrib.onGPU()
        self.onCPU = attrib.onCPU()
        self.visualizationName = attrib.qualifier()

        self.length = attrib.length()
        self.lengthStr = f"{attrib.length():,}"
        self.tupleSize = attrib.tupleSize()
        self.owner = attrib.type().name()
        if self.owner == "Invalid":
            self.owner = "None"

        self.setAttrs(attrib.properties())

    def fillFromLayer(self, layer: hou.ImageLayer) -> None:
        res = layer.bufferResolution()
        self.resolution = QtCore.QSize(*res)
        self.resolutionString = CopLayer.sizeToString(self.resolution)
        self.dataRect = brectToRect(layer.dataWindow()).toAlignedRect()
        self.dataRectString = self.rectToString(self.dataRect)
        self.displayRect = brectToRect(layer.displayWindow()).toAlignedRect()
        self.displayRectString = self.rectToString(self.displayRect)
        self.onGPU = layer.onGPU()
        self.onCPU = layer.onCPU()

        # Creates a layer with default settings
        default = hou.ImageLayer()
        # Check if the camera settings are at default
        self.cameraIsDefault = (
                layer.projection() == default.projection() and
                layer.aperture() == default.aperture() and
                layer.focalLength() == default.focalLength() and
                layer.cameraPosition() == default.cameraPosition() and
                layer.fStop() == default.fStop() and
                layer.focusDistance() == default.focusDistance() and
                layer.clippingRange() == default.clippingRange()
        )

        storage = layer.storageType()
        self.storageTypeName = general.image_storage_name[storage]
        self.storageTypeBriefName = general.image_storage_name_brief[storage]
        self.isInt = storage in (hou.imageLayerStorageType.Int8,
                                 hou.imageLayerStorageType.Int16,
                                 hou.imageLayerStorageType.Int32)

        pscale = self.pixelScale = [round(x, 4) for x in layer.pixelScale()]
        if pscale[0] == pscale[1]:
            self.pixelScaleString = str(pscale[0])
        else:
            self.pixelScaleString = f"{pscale[0]} \xd7 {pscale[1]}"

        self.borderTypeName = layer.border().name()
        self.visualizationName = layer.typeInfo().name()
        self.channelCount = layer.channelCount()
        self.isConstant = layer.isConstant()
        self.dataIsOffset = not self.dataRect.topLeft().isNull()
        self.dataIsSized = self.dataRect.size() != self.resolution
        self.displayIsOffset = not self.displayRect.topLeft().isNull()
        self.displayIsSized = self.displayRect.size() != self.resolution
        self.aspectRatio = layer.pixelAspectRatio()
        self.imageXform = Xform.fromMatrix(layer.imageToWorldTransform())

        self.projectionName = layer.projection().name()
        self.aperture = round(layer.aperture(), 4)
        self.focalLength = round(layer.focalLength(), 4)
        self.cameraPos = [round(x, 4) for x in layer.cameraPosition()]
        self.imageDistance = round(layer.imagingDistance(), 4)  # cameraPos[2]
        self.fStop = round(layer.fStop(), 4)
        self.focusDistance = round(layer.focusDistance(), 4)
        self.clipNear, self.clipFar = [round(x, 4)
                                       for x in layer.clippingRange()]
        self.setAttrs(layer.properties())


class CopCableWire(core.Info):
    name: str = ""
    dataTypeName: str = ""
    appearanceIndex: int = 0


class CopInfo(core.Info):
    resolution: QtCore.QSize
    resolutionString: str = ""
    mixedResolutions: bool = False
    layers: list[CopLayer]
    thumbnail: QtGui.QPixmap
    viewOutput: int

    def flagChanged(self, node: hou.Node) -> None:
        self.viewOutput = node.outputForViewFlag()

    @classmethod
    def forNode(cls, node: hou.CopNode, output_names: Sequence[str],
                output_labels: Sequence[str], output_index: Optional[int]
                ) -> Optional[CopInfo]:
        if not isinstance(node, hou.CopNode):
            return None

        ci = cls()
        output_types = node.outputDataTypes()
        ci.viewOutput = node.outputForViewFlag()

        if output_index is None:
            layer_indices = range(len(output_names))
        elif output_index < len(output_names):
            layer_indices = [output_index]
        else:
            layer_indices = ()

        if not node.errors():
            ci.layers = []
            for ix in layer_indices:
                cl = CopLayer()
                cl.outputIndex = ix
                cl.name = output_names[ix]
                cl.label = output_labels[ix]
                cl.dataType = dt = output_types[ix]

                cbl = node.outputCableStructure(ix)
                if cbl.wireCount() > 1:
                    cl.subWires = [
                        CopCableWire(name=cbl.wireName(i),
                                     dataTypeName=cbl.wireDataType(i),
                                     appearanceIndex=cbl.appearanceIndex(i))
                        for i in range(cbl.wireCount())
                    ]

                if dt == "Geometry" and (geom := node.geometry(ix)):
                    cl.geometry = GeoInfo()
                    cl.geometry.fillCommonGeoInfo(geom)
                    cl.geometry.fillDetails(geom)
                elif dt in ("FloatVDB", "VectorVDB", "IntVDB") and \
                        (vdb := node.vdb(ix)):
                    cl.fillFromVDB(vdb)
                    del vdb
                elif ((dt in ("IntAttrib", "FloatAttrib", "Vector2Attrib",
                              "VectorAttrib", "Vector4Attrib")) and
                      (attrib := node.attrib(ix))):
                    cl.fillFromAttribute(attrib)
                    del attrib
                elif layer := node.layer(ix):
                    cl.fillFromLayer(layer)
                    del layer
                else:
                    cl.valid = False
                ci.layers.append(cl)

            if ci.layers:
                vix = ci.viewOutput if ci.viewOutput < len(ci.layers) else 0
                ci.resolution = ci.layers[vix].resolution
                ci.resolutionString = CopLayer.sizeToString(ci.resolution)
                if ci.resolution:
                    for cl in ci.layers:
                        if cl.resolution and cl.resolution != ci.resolution:
                            cl.oddResolution = True
                            ci.mixedResolutions = True
        return ci


class RopInfo(core.Info):
    pass


class LabeledValue(core.Info):
    label: str
    text: str
    labelColor: QtGui.QColor
    textColor: QtGui.QColor
    monospaced: bool = False
    link: str = ""
    format: str = "auto"


class NodeReference(core.Info):
    name: str
    path: str
    iconName: str

    @classmethod
    def forNode(cls, node: hou.Node) -> NodeReference:
        nr = cls()
        nr.name = node.name()
        nr.path = node.path()
        nr.iconName = node.type().icon()
        return nr


class NodeSpecificRow(core.Info):
    label: str
    text: str
    visible: bool


class NodeOutput(core.Info):
    index: int
    internalName: str
    label: str


class NodeInfo(core.Info):
    sessionID: int
    name: str
    path: str
    parentPath: str
    className: str
    categoryName: str
    typeName: str
    typeNameWithCategory: str
    typeDesc: str
    iconName: str
    iconPixmap: QtGui.QPixmap
    color: QtGui.QColor
    colorIsDefault: bool
    comment: str
    displayComment: bool
    namespace: str
    basename: str
    version: str
    summary: str
    outputs: list[NodeOutput]
    outputLabels: list[str]
    currentOutputIndex: int
    currentOutputName: str
    incomingParmRefs: list[str]
    outgoingNodePaths: list[str]
    isBypassed: bool
    timeDependent: bool
    isAsset: bool
    matchesCurrent: bool
    creationTime: QtCore.QDateTime
    creationTimeString: str
    baseTime: QtCore.QDateTime
    modificationTime: QtCore.QDateTime
    modificationTimeString: str
    userData: list[LabeledValue]
    assetComment: str = ""
    definedBy: str
    definedInHFS: bool
    debug: bool
    errors: list[Message]
    warnings: list[Message]
    messages: list[Message]
    subnetOutputs: list[str]
    cookCount: int = 0
    lastCookTime: float = 0.0
    lastCookTimeString: str
    perfStats: PerfStats
    nodeSpecificBranchName: str
    nodeSpecificRows: list[LabeledValue]
    geometry: GeoInfo
    objInfo: ObjInfo
    dopInfo: DopInfo
    lopInfo: LopInfo
    topInfo: TopInfo
    copInfo: CopInfo
    ropInfo: RopInfo
    chopInfo: ChopInfo
    customTags: list[custom.CustomTag]
    customTop: list[custom.CustomContainer]
    customMiddle: list[custom.CustomContainer]
    customBottom: list[custom.CustomContainer]

    _exclude_from_eq = (
        "cookCount", "lastCookTime", "lastCookTimeString", "baseTime",
        "customTags", "customTop", "customMiddle", "customBottom"
    )

    def __init__(self, session_id: int):
        super().__init__(sessionID=session_id)

    def node(self) -> Optional[hou.Node]:
        return hou.nodeBySessionId(self.sessionID)

    @QtCore.Slot(str, str, bool, bool)
    def setAttrVisualized(self, type_name: str, name: str, active: bool,
                          open_settings: bool) -> None:
        if node := self.node():
            from . import visualizers
            visualizers.setAttrVisualized(node, self.currentOutputIndex,
                                          type_name, name, active,
                                          open_settings)

    @QtCore.Slot(int, str, bool, bool)
    def setVolumeVisualized(self, prim_num: int, name: str, active: bool,
                            open_settings: bool) -> None:
        if node := self.node():
            from . import visualizers
            visualizers.setVolumeVisualized(node, self.currentOutputIndex,
                                            prim_num, name, active,
                                            open_settings)

    @QtCore.Slot(str, str, bool)
    def setGroupVisualized(self, type_name: str, name: str, active: bool
                           ) -> None:
        from . import visualizers
        visualizers.setGroupVisualized(type_name, name, active)

    def parm(self, name: str) -> Optional[hou.Parm]:
        if node := self.node():
            return node.parm(name)

    @QtCore.Slot(str, result=float)
    def floatParm(self, name: str) -> Optional[float]:
        if parm := self.parm(name):
            return parm.evalAsFloat()

    @QtCore.Slot(str, result=int)
    def intParm(self, name: str) -> Optional[int]:
        if parm := self.parm(name):
            return parm.evalAsInt()

    @QtCore.Slot(str, result=str)
    def stringParm(self, name: str) -> Optional[str]:
        if parm := self.parm(name):
            return parm.evalAsString()

    @QtCore.Slot(str)
    def setCommentText(self, text: str) -> None:
        if text != self.comment:
            self.comment = text
        if node := self.node():
            if text != node.comment():
                try:
                    node.setComment(text)
                except hou.PermissionError:
                    pass

    @QtCore.Slot(bool)
    def setCommentVisible(self, visible: bool) -> None:
        if node := self.node():
            node.setGenericFlag(hou.nodeFlag.DisplayComment, visible)

    def flagChanged(self) -> None:
        node = self.node()
        if not node:
            return
        self.displayComment = node.isGenericFlagSet(hou.nodeFlag.DisplayComment)
        if self.copInfo:
            self.copInfo.flagChanged(node)

    def stopBackgroundActivity(self) -> None:
        if self.geometry:
            self.geometry.stopBackgroundActivity()

    def setFrom(self, node: hou.Node, recook=False, output_index: int = None,
                highlight_new=True, compare_attrs=False, debug=False,
                include_help=True, include_lop_geo_details=False,
                theme: colors.Theme = None) -> None:
        t = perf_counter()
        self.stopBackgroundActivity()
        nodepath = node.path()
        nodetype = node.type()
        category = nodetype.category()
        cat_name = category.name()
        typename = nodetype.name()
        icon_name = nodetype.resolvedIcon()
        theme = theme or colors.defaultTheme()

        # output_index will be None here, if the user clicked the (i) button
        # or MMB-ed on the node body, not a specific output. Before we
        # convert that to a specific output to show, remember what it was,
        # because some things below work differently based on where the user
        # clicked (COP info shows all layers).
        output_names = node.outputNames()
        original_output_index = output_index
        if output_index is None:
            output_index = node.outputForViewFlag()
        # For some reason, if you MMB on a flag, the network editor code calls
        # the info window with output_index=98 (!?) so we have to fix that
        if output_index >= len(output_names):
            output_index = 0
        if isinstance(node, hou.TopNode):
            if recook:
                try:
                    node.cook()
                except hou.OperationFailed:
                    pass
            branches = {}
        else:
            tree = node.infoTree(force_cook=recook, output_index=output_index,
                                 debug=debug)
            branches = tree.branches()

        self.sessionID = node.sessionId()
        self.name = node.name()
        self.path = nodepath
        self.parentPath = node.parent().path() if node.parent() else ''
        self.typeName = typename
        self.typeNameWithCategory = nodetype.nameWithCategory()
        self.typeDesc = nodetype.description()
        self.iconName = icon_name
        self.className = type(node).__name__
        self.categoryName = cat_name
        self.color = QtGui.QColor.fromRgbF(*node.color().rgb())
        self.colorIsDefault = self.color == general.DEFAULT_NODE_COLOR
        self.comment = node.comment()
        self.displayComment = node.isGenericFlagSet(hou.nodeFlag.DisplayComment)
        _, self.namespace, self.basename, self.version = \
            hou.hda.componentsFromFullNodeTypeName(typename)
        self.summary = ""

        self.outputLabels = node.outputLabels()
        outputs = []
        for ix, (out_name, out_label) in \
                enumerate(zip(output_names, self.outputLabels)):
            outputs.append(
                NodeOutput(index=ix, internalName=out_name, label=out_label)
            )
        self.outputs = outputs
        self.currentOutputIndex = output_index
        self.currentOutputName = (output_names[output_index]
                                  if 0 <= output_index < len(output_names)
                                  else None)

        self.isBypassed = node.isGenericFlagSet(hou.nodeFlag.Bypass)
        cook_time = round(node.lastCookTime() / 1000, 5)
        self.lastCookTime = cook_time
        self.lastCookTimeString = formatHMS(cook_time, decimal_places=3)

        defn = nodetype.definition()
        self.isAsset = bool(defn)
        self.assetComment = defn.comment() if defn else ""
        self.matchesCurrent = node.matchesCurrentDefinition()
        self.timeDependent = node.isTimeDependent(for_last_cook=True)
        self.userData = [LabeledValue(label=k, text=v) for k, v
                         in node.userDataDict().items()]

        self.baseTime = QtCore.QDateTime(datetime.now())
        ctime = node.creationTime()
        self.creationTime = QtCore.QDateTime(ctime)
        self.creationTimeString = formatDatetime(ctime)
        mtime = node.modificationTime()
        self.modificationTime = QtCore.QDateTime(mtime)
        self.modificationTimeString = formatDatetime(mtime)

        self.definedBy = nodeDefinitionString(node)
        self.definedInHFS = self.definedBy and self.definedBy.startswith("$HFS")
        self.perfStats = PerfStats.forPath(nodepath)

        incoming_parms: set[str] = set()
        for parm in node.parmsReferencingThis():
            try:
                incoming_parms.add(parm.path())
            except hou.ObjectWasDeleted:
                pass
        self.incomingParmRefs = sorted(incoming_parms,
                                       key=refSortKeyFn(node, parm_refs=True))

        self.outgoingNodePaths = sorted(
            set(n.path() for n in node.references()), key=refSortKeyFn(node)
        )

        # Category specific info
        core.setOrUpdate(self, "objInfo", ObjInfo.forNode(node, recook))
        core.setOrUpdate(self, "geometry", GeoInfo.forNode(
            node, branches.get("SOP Info"), output_index, debug=debug,
            highlight_new=highlight_new, full_diff=compare_attrs
        ))
        core.setOrUpdate(self, "dopInfo", DopInfo.forNode(node))
        core.setOrUpdate(self, "lopInfo", LopInfo.forNode(
            node, output_index, lop_geo_details=include_lop_geo_details
        ))
        core.setOrUpdate(self, "copInfo", CopInfo.forNode(
            node, output_names, self.outputLabels, original_output_index
        ))
        core.setOrUpdate(self, "topInfo",
                         TopInfo.forNode(node, output_index))
        core.setOrUpdate(self, "chopInfo",
                         ChopInfo.forNode(node, output_index))
        core.setOrUpdate(self, "perfStats", PerfStats.forPath(node.path()))

        if include_help and (summary := nodeTypeSummary(nodetype)):
            self.summary = summary.replace("\n", " ")

        self.subnetOutputs = [out_node.path() for out_node
                              in node.subnetOutputs()]

        self.customTags = []
        self.customTop = []
        self.customMiddle = []
        self.customBottom = []

        for branch_name in branches:
            # Look for old-style node-specific data in the infoTree
            if node_specific_branch_expr.match(branch_name):
                self.nodeSpecificBranchName = branch_name
                properties = core.infoTreeToData(
                    branches[branch_name], keyify_properties=False,
                    allow_branches=False, force_dict=True
                )
                if properties:
                    middle = custom.CustomContainer()
                    grid = middle.addGrid(id="auto_info_tree_grid")
                    grid.fromInfoTree = True
                    for k, v in properties.items():
                        if k.startswith("Sub-Network Output"):
                            continue
                        ti = grid.addSimpleTextTile(k, v)
                        ti.fromInfoTree = True
                    self.customMiddle = middle.model
                break

        # Add custom user items
        custom.applyCustomInfo(self, node, theme, output_index)

        # Put these at the end in case getting the info caused them to change
        self.errors = [Message(original=m, html=linkify(node, m))
                       for m in unique(node.errors())]
        self.warnings = [Message(original=m, html=linkify(node, m))
                         for m in unique(node.warnings())]
        self.messages = [Message(original=m, html=linkify(node, m))
                         for m in unique(node.messages())]
        self.cookCount = node.cookCount()
