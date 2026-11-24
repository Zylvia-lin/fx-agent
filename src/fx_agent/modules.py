"""Functional tags map to upstream guides without duplicating them as memories."""

TAXONOMY_VERSION = 2

HOUDINI = {
    "sop": ("SOP 建模、属性与变形", "model"),
    "vop": ("VOP 节点编程与属性运算", None),
    "dop": ("DOP 求解网络与模拟数据", "dyno"),
    "particles": ("粒子 / POP", "dopparticles"),
    "pyro": ("烟雾、火焰、爆炸", "pyro"),
    "flip": ("流体 / FLIP", "fluid"),
    "rbd": ("破碎、刚体", "destruction"),
    "vellum": ("布料、软体", "vellum"),
    "mpm": ("MPM 材料模拟", "mpm"),
    "ocean": ("海洋与海浪", "ocean"),
    "terrain": ("地形 / HeightFields", "heightfields"),
    "terrain_cops": ("地形图像处理", "heightfields_cop"),
    "solaris": ("USD / Solaris", "solaris"),
    "materials": ("材质着色", "shade"),
    "lighting": ("灯光", "render"),
    "rendering": ("渲染", "render"),
    "cops": ("Copernicus 图像处理", "copernicus"),
    "pdg": ("PDG / TOPs", "tops"),
    "hda": ("HDA 资产", "assets"),
    "animation": ("动画", "anim"),
    "character": ("角色", "character"),
    "crowds": ("群集", "crowds"),
    "groom": ("毛发", "fur"),
    "feathers": ("羽毛", "feathers"),
    "grains": ("颗粒", "grains"),
    "fem": ("有限元", "finiteelements"),
    "muscles": ("肌肉", "muscles"),
    "ml": ("机器学习", "ml"),
    "io": ("文件与缓存", "io"),
    "copy": ("复制与实例", "copy"),
    "props": ("道具", "props"),
    "compositing": ("Houdini 合成", "composite"),
    "dyno": ("通用动力学", "dyno"),
    "troubleshooting": ("故障排查", "troubleshooting"),
    "general": ("通用制作纪律", "discipline"),
    "unclassified": ("未分类旧记录", None),
}
NUKE = {
    key: (label, None)
    for key, label in (
        ("compositing", "合成"),
        ("keying", "抠像"),
        ("tracking", "跟踪"),
        ("deep", "Deep 合成"),
        ("lighting", "补光、重打光与光效"),
        ("layers", "分层、通道与 AOV 合成"),
        ("color", "色彩与调色"),
        ("io", "素材输入输出"),
        ("rendering", "输出渲染"),
        ("general", "通用"),
        ("unclassified", "未分类旧记录"),
    )
}
MAYA = {key: (label, None) for key, label in (
    ("modeling", "建模与拓扑"), ("animation", "动画与关键帧"), ("rigging", "绑定与蒙皮"),
    ("lighting", "灯光"), ("rendering", "渲染"), ("materials", "材质"),
    ("io", "工程与素材"), ("troubleshooting", "故障排查"),
    ("general", "通用"), ("unclassified", "未分类旧记录"),
)}
CATALOGS = {"houdini": HOUDINI, "nuke": NUKE, "maya": MAYA}
WORKFLOW_SKILLS = {"houdini": "fx-workflow", "nuke": "nuke-workflow", "maya": "maya-workflow"}

ALIASES = {
    "pop": "particles",
    "dopparticles": "particles",
    "smoke": "pyro",
    "fire": "pyro",
    "fluid": "flip",
    "liquid": "flip",
    "destruction": "rbd",
    "fracture": "rbd",
    "model": "sop",
    "shade": "materials",
    "render": "rendering",
    "tops": "pdg",
    "assets": "hda",
    "heightfields": "terrain",
    "copernicus": "cops",
}

SOFTWARE_ALIASES = {
    "houdini": {"dops": "dop", "vops": "vop", "粒子": "particles", "烟火": "pyro"},
    "nuke": {"relighting": "lighting", "分层": "layers", "色彩": "color", "灯光": "lighting"},
    "maya": {"model": "modeling", "建模": "modeling", "动画": "animation", "绑定": "rigging"},
}
MODULE_MATCH_SQL = "EXISTS (SELECT 1 FROM json_each(COALESCE(json_extract(payload,'$.modules'),'[\"unclassified\"]')) AS module_tags WHERE module_tags.value=?)"


def normalize_modules(values: list[str], software: str) -> list[str]:
    catalog = CATALOGS.get(software)
    if catalog is None:
        raise ValueError("Invalid software")
    aliases = ALIASES | SOFTWARE_ALIASES[software]
    keys = (value.strip().casefold() for value in values)
    normalized = sorted({aliases.get(key, key) for key in keys})
    if not normalized or len(normalized) > 4 or any(value not in catalog for value in normalized):
        raise ValueError("Unknown module or too many modules; read fx_get_module_catalog")
    if "unclassified" in normalized and len(normalized) > 1:
        raise ValueError("Unclassified cannot be mixed with known modules")
    return normalized


def require_classified_modules(modules: list[str]) -> None:
    if "unclassified" in modules:
        raise ValueError("Choose a module for this work using fx_get_module_catalog")
    if "general" in modules and len(modules) > 1:
        raise ValueError("Use general alone for software-wide experience; otherwise select specific modules")


def module_catalog(software: str = "houdini") -> dict:
    normalize_modules(["general"], software)
    catalog = CATALOGS[software]
    return {
        "version": TAXONOMY_VERSION,
        "software": software,
        "guidelines_are_learned_experience": False,
        "guide_tool": "get_workflow_guide" if software == "houdini" else None,
        "learned_guide_tool": "fx_get_module_knowledge",
        "update_policy": "verified_global_text_experiences",
        "classification": {
            "owner": "host_assistant",
            "rule": "按实际操作和经验能复用的场景选择最具体的模块；一条经验只标真正适用的模块。",
            "general": "仅用于对整个软件都适用的经验，单独使用。",
            "unclassified": "保留给旧记录。",
        },
        "workflow_skill": WORKFLOW_SKILLS[software],
        "items": [
            {"module": key, "label": label, "guide_topic": topic}
            for key, (label, topic) in catalog.items()
        ],
    }
