# FX Agent

让 Codex、Antigravity 或其他 MCP 助手在本地操作 **Houdini、Nuke 与 Maya**，并将制作规范、排错避坑和可迁移工艺沉淀为 **KB 级纯文本通用经验**（直接随仓库 `data/` 进行 Git 版本管理与共享）。

> **核心分工**：**经验归 FX Agent（纯文本），资产归模板库（FX Template）。**
> 本项目不在 `data/` 中保存任何 `.hip` / `.nk` / `.ma` 工程、贴图、渲染序列、截图或效果卡；需要查找和复用现成工程资产时，通过 `fx_search_published_templates` 直接连接外部 [FX Template 特效模板库](https://github.com/Zylvia-lin/FX-Template) 搜索。

[![Python 3.12+](https://img.shields.io/badge/Python-3.12%2B-blue)](pyproject.toml)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![GitHub stars](https://img.shields.io/github/stars/Zylvia-lin/fx-agent?style=flat)](https://github.com/Zylvia-lin/fx-agent/stargazers)

[快速开始](#快速开始) · [文档导航](public-docs/README.md) · [贡献指南](CONTRIBUTING.md) · [第三方致谢](THIRD_PARTY_NOTICES.md) · [联系作者](#联系方式)

## 核心能力

- **Houdini 制作（`fx-agent`）**：复用 `fxhoudinimcp` 执行层，提供 `$HIP` 路径与 File Cache 规范校验、Render View 单帧核验与任务门禁。
- **Nuke 合成（`fx-nuke`）**：独立 stdio MCP 入口，支持多窗口会话发现/切换、节点与树状布局工具、Viewer 核验与跨软件渲染问题反馈。
- **Maya 灯光与渲染（`fx-maya`）**：显式会话选择，接入 Arnold / Redshift 灯光、HDRI、照明连接与单帧输出，支持 Python 2 / 3 桥接。
- **纯文本通用经验库（`data/`）**：仅保存版本化用户规范（`user_rules`）、去项目化脱敏的通用工艺/排错经验（`memories`）、自动编译的纯文本模块手册（`data/modules/global/`）和官方基线（`data/baselines/`）。整库仅几百 KB，直接由 Git 跟踪共享，无需额外部署服务端。
- **外部特效模板库直连**：通过 `fx_search_published_templates` 直连本机 FX Template 桌面端按名称与标签搜索现成工程模板。

## 快速开始

### 环境要求

主要安装流程面向 **Windows + Codex / MCP 宿主**。需准备 Python 3.12+、Git、uv，以及商业 DCC 软件与许可证：

| 软件 / 入口 | 默认桥接端口 | 使用说明 |
| --- | --- | --- |
| Houdini / `fx-agent` | `18186` | 需安装 Houdini 并至少启动一次（已验证 20.0.751 / 22.0.466） |
| Nuke / `fx-nuke` | `8187` | 要求 Nuke 15.2+（多窗口自动递增独占端口） |
| Maya / `fx-maya` | `18770..18789` | 独立可选安装；支持 Python 2 / 3 与 Arnold / Redshift，详见 [Maya 接入](public-docs/maya.md) |

### 一键安装与连接

```powershell
git clone https://github.com/Zylvia-lin/fx-agent.git
cd fx-agent

# 仅预览安装计划
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\Install-FXAgent.ps1 -Plan
```

保存工程并关闭对应软件后，双击根目录 **`Install-FX-Agent.bat`**（或执行 `.\scripts\Install-FXAgent.ps1`）。安装完成后重启软件与 MCP 宿主，新建对话即可使用：

> 使用 fx-agent。先检查 Houdini 和 Nuke 的连接，读取制作规范与已有纯文本经验。项目名为“测试项目”，镜头为 sh010。

### 搭配外部特效模板库（推荐）

如需管理与复用现成效果工程、HDA、贴图和视频预览，请配合安装 [特效模板库（FX Template）](https://github.com/Zylvia-lin/FX-Template)：

1. 按 FX Template 仓库 README 安装桌面端并配置素材目录与 PostgreSQL，保持桌面端运行（可最小化到托盘）。
2. 在对话中直接通过 `fx_search_published_templates` 按关键词或标签搜索已发布模板，无需将任何工程复制到 `fx-agent/data`。

### 纯文本经验库与 Git 共享

- 默认数据根为仓库内 `data/`（也可通过绝对路径 `FX_AGENT_DATA_ROOT` 覆盖），包含：
  - `data/memory.sqlite3`：纯文本规范与通用经验数据库（`< 1 MB`）
  - `data/modules/global/`：按软件与功能模块自动编译的纯文本 Markdown 手册
  - `data/baselines/`：官方工作流与 Skills 基线
- **默认工具面（`FX_AGENT_TOOL_PROFILE=text_only`）**：自动隐藏旧版重型工程归档与效果卡工具，减少宿主 Schema Token 消耗；提交经验时自动拦截本机/网络绝对路径，确保 `data/` 干净适合 Git 提交与团队共享。
- 本地会话注册文件（`data/nuke_sessions/`、`data/maya_sessions/`）、锁文件、临时 WAL 文件及任何工程/贴图均已由 `.gitignore` 排除，不会进入版本库。

## 本地开发与验证

```powershell
uv sync --frozen
uv run fx-agent --version
uv run ruff check src tests scripts
uv run pytest --basetemp=.tmp/pytest
```

Nuke 可选依赖使用 `uv sync --frozen --group nuke`。提交规范与第三方代码要求见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 文档导航

- [全部文档索引](public-docs/README.md)
- [使用指南（纯文本经验与工作流）](public-docs/使用指南.md)
- [Windows 一键安装](public-docs/一键安装.md)
- [Nuke 接入说明](public-docs/Nuke接入与独立学习.md)
- [Maya 接入说明](public-docs/maya.md)
- [Render View 单帧核验接口](public-docs/RenderView单帧核验接口.md)
- [上游依赖基线](public-docs/上游依赖.md) · [依赖许可证清单](public-docs/依赖许可证清单.md)

## 许可证与致谢

FX Agent 原创部分采用 [MIT License](LICENSE)。第三方代码保留原作者版权、许可证与 NOTICE，详见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

特别感谢 [fxhoudinimcp](https://github.com/healkeiser/fxhoudinimcp)、[JTCHE/houdini-mcp](https://github.com/JTCHE/houdini-mcp)、[kleer001/houdini-mcp](https://github.com/kleer001/houdini-mcp)、[kleer001/nuke-mcp](https://github.com/kleer001/nuke-mcp)、[AutoMaya](https://github.com/CallMeParams/automaya-mcp)、[chadrik Maya MCP](https://github.com/chadrik/maya-mcp-server) 和 [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)。

## 联系方式

- 作者：[Zylvia-lin](https://github.com/Zylvia-lin)
- 微信：`718422052`
- 邮箱：[mlinchun@icloud.com](mailto:mlinchun@icloud.com)
- 问题与建议：[GitHub Issues](https://github.com/Zylvia-lin/fx-agent/issues)
