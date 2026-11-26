# FX Agent

面向 **Houdini、Nuke 与 Maya** 的本地 MCP 制作助手，让 Codex、Antigravity 等支持 MCP 的 AI 助手直接操作本地 DCC 软件，并在制作过程中持续沉淀可复用的规范与经验。

[![Python 3.12+](https://img.shields.io/badge/Python-3.12%2B-blue)](pyproject.toml)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![GitHub stars](https://img.shields.io/github/stars/Zylvia-lin/fx-agent?style=flat)](https://github.com/Zylvia-lin/fx-agent/stargazers)

[核心特性](#核心特性) · [快速开始](#快速开始) · [文档导航](#文档导航) · [贡献指南](CONTRIBUTING.md) · [联系作者](#联系方式)

## 核心特性

- **Houdini 制作（`fx-agent`）**：基于 `fxhoudinimcp` 提供节点构建、参数调整、`$HIP` 路径与 File Cache 规范校验，以及原生 Render View 单帧核验。
- **Nuke 合成（`fx-nuke`）**：独立 MCP 服务入口，支持多窗口会话发现与切换、节点与树状布局整理、Viewer 检查，以及跨软件渲染问题反馈。
- **Maya 灯光与渲染（`fx-maya`）**：支持显式会话选择，接入 Arnold / Redshift 灯光、HDRI、照明连接与单帧渲染，兼容 Python 2 / 3 桥接。
- **受控制作流程**：通过任务门禁锁定目标工程与会话端口，要求先核对规范与方案再执行修改，并在交付前完成结构与画面核验。
- **轻量经验沉淀**：将个人制作规范、常用工艺与排错经验沉淀为轻量的纯文本知识，并自动整理为模块手册，便于跨项目复用。
- **外部模板库联动（可选）**：支持直连 [FX Template](https://github.com/Zylvia-lin/FX-Template)，在对话中按关键词或标签检索现成工程与效果模板。

## 支持软件

主要安装与使用面向 **Windows + MCP 宿主**，需预先准备 Python 3.12+、Git、uv 及对应 DCC 软件与许可证：

| 软件 / 入口 | 默认桥接端口 | 说明 |
| --- | --- | --- |
| Houdini / `fx-agent` | `18186` | 需安装 Houdini 并至少启动一次（已验证 20.0.751 / 22.0.466） |
| Nuke / `fx-nuke` | `8187` | 需 Nuke 11.0+，多窗口自动分配端口 |
| Maya / `fx-maya` | `18770..18789` | 独立可选安装，支持 Arnold / Redshift 与 Python 2 / 3，详见 [Maya 接入说明](public-docs/maya.md) |

## 快速开始

### 1. 克隆仓库并预览安装计划

```powershell
git clone https://github.com/Zylvia-lin/fx-agent.git
cd fx-agent

# 可选：仅预览安装路径与变更，不修改本机配置
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\Install-FXAgent.ps1 -Plan
```

### 2. 一键安装

保存当前工程并关闭 Houdini / Nuke 后，双击根目录 **`Install-FX-Agent.bat`**（或在终端运行 `.\scripts\Install-FXAgent.ps1`）。

### 3. 启动并开始对话

重启对应 DCC 软件与 MCP 宿主，新建对话即可开始协作，例如：

> 使用 fx-agent。先检查 Houdini 和 Nuke 的连接，读取制作规范与已有经验。项目名为“测试项目”，镜头为 sh010。

FX Agent 启动时使用本机已安装的版本。升级时手动更新仓库并按需重新运行 **`Install-FX-Agent.bat`**，再重启宿主与对应的 DCC。详见 [仓库更新后的升级流程](public-docs/一键安装.md#仓库更新后的升级流程)。

### 可选：搭配 FX Template 模板库

如果需要管理和复用现成的特效工程、HDA、贴图与视频预览，可配合安装 [FX Template](https://github.com/Zylvia-lin/FX-Template)。按其文档启动本地桌面端后，即可在对话中直接按名称或标签检索已发布的工程模板。

## 本地开发

运行数据库与生成的模块手册默认位于 `~/.fx-agent/cache/`。Git 共享经审阅的工艺摘要和基础规范。安装或启动时会把共享摘要载入本机缓存。[数据与共享说明](public-docs/使用指南.md)

```powershell
uv sync --frozen
uv run fx-agent --version
uv run ruff check src tests scripts
uv run pytest --basetemp=.tmp/pytest
```

如需同时安装 Nuke 相关依赖，可使用 `uv sync --frozen --group nuke`。代码规范与贡献方式见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 文档导航

- [全部文档索引](public-docs/README.md)
- [使用指南](public-docs/使用指南.md)
- [Windows 一键安装](public-docs/一键安装.md)
- [Nuke 接入说明](public-docs/Nuke接入与独立学习.md)
- [Maya 接入说明](public-docs/maya.md)
- [Render View 单帧核验接口](public-docs/RenderView单帧核验接口.md)
- [上游依赖基线](public-docs/上游依赖.md) · [依赖许可证清单](public-docs/依赖许可证清单.md)

## 许可证与致谢

FX Agent 原创代码采用 [MIT License](LICENSE)。集成或引用的第三方代码保留原作者版权、许可证与声明，详见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

特别感谢以下开源项目：
[fxhoudinimcp](https://github.com/healkeiser/fxhoudinimcp)、[JTCHE/houdini-mcp](https://github.com/JTCHE/houdini-mcp)、[kleer001/houdini-mcp](https://github.com/kleer001/houdini-mcp)、[kleer001/nuke-mcp](https://github.com/kleer001/nuke-mcp)、[AutoMaya](https://github.com/CallMeParams/automaya-mcp)、[chadrik Maya MCP](https://github.com/chadrik/maya-mcp-server) 与 [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)。

## 联系方式

- 作者：[Zylvia-lin](https://github.com/Zylvia-lin)
- 微信：`718422052`
- 邮箱：[mlinchun@icloud.com](mailto:mlinchun@icloud.com)
- 问题与建议：[GitHub Issues](https://github.com/Zylvia-lin/fx-agent/issues)
