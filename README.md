# FX Agent

让 Codex或者其他助手工具 在本地操作 Houdini、Nuke 与 Maya，并将制作规范、纠错过程和已确认方案保存为可追溯的经验。



最近更新：
    为了满足多人共享知识库以及多人团队共用，添加了专门的数据管理服务端，使用apikey的形式访问，带简单的web管理后台，可局域网部署或者云端部署知识库数据。具体请查看[服务端说明](services\data-service\README.md)

[![Python 3.12+](https://img.shields.io/badge/Python-3.12%2B-blue)](pyproject.toml)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![GitHub stars](https://img.shields.io/github/stars/Zylvia-lin/fx-agent?style=flat)](https://github.com/Zylvia-lin/fx-agent/stargazers)

[快速开始](#快速开始) · [文档](public-docs/README.md) · [贡献](CONTRIBUTING.md) · [第三方致谢](THIRD_PARTY_NOTICES.md) · [联系作者](#联系方式)

## 项目能力

- **Houdini 制作**：复用 fxhoudinimcp 执行层，提供工程路径、缓存输出、任务方案与完成检查。
- **Nuke 合成**：独立 MCP 入口、节点与选择查询、图操作、Viewer 核验及跨软件渲染问题反馈。
- **Maya 灯光与渲染**：显式选择会话，接入 Arnold / Redshift，提供 Python 2 / 3 桥接。
- **制作记忆**：保存用户规范、纠错轨迹、检查点与已确认方案，按来源和修订召回。
- **工程学习**：只读解析 HIP / NK 模板，整理效果指示卡、案例与模块知识，支持多设备追加合并。

当前 Codex 负责规划、操作和效果判断；FX Agent 提供 MCP 工具、工作流和持久化能力。制作经验由宿主在阶段边界提炼，不会自动训练模型权重或后台抓取全部聊天。

## 快速开始

### 环境要求

主要安装流程面向 **Windows + Codex**。准备 Python 3.12+、Git、uv，以及要使用的软件和渲染器。安装器可通过 WinGet 补齐缺少的 Git / uv；商业软件和许可证需自行准备。

| 软件 / 入口 | 使用说明 |
| --- | --- |
| Houdini / `fx-agent` | 需安装 Houdini 并至少启动一次；现有现场记录包含 Houdini 20.0.751、22.0.466，其他版本按道理python3以上兼容但不代表所有版本均已验收 |
| Nuke / `fx-nuke` | 安装流程要求 Nuke 15.2+；其他版本需单独验证 |
| Maya / `fx-maya` | 独立可选安装；Python 2 / 3、Arnold / Redshift 的具体范围见 [Maya 文档](public-docs/maya.md) |

### 安装与连接
推荐直接仓库给codex让他帮忙安装即可，手动安装如下

在终端执行：

```powershell
git clone https://github.com/Zylvia-lin/fx-agent.git
cd fx-agent

# 只预览安装计划
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\Install-FXAgent.ps1 -Plan
```

保存工程并关闭对应软件和旧桥接后，双击根目录 **`Install-FX-Agent.bat`**，或在终端执行：

```powershell
.\Install-FX-Agent.bat
```

默认流程安装 Houdini / Nuke 桥接和 Codex 全局入口，并备份旧配置。Maya 的安装命令、单组件更新、自定义路径及端口见 [一键安装](public-docs/一键安装.md) 和 [Maya 接入](public-docs/maya.md)。Codex 插件清单是可选分发方式；避免重复注册同一入口。

启动需要使用的软件，重启 Codex 并新建聊天，然后发送：

> 使用 fx-agent。先检查 Houdini 和 Nuke 的连接，读取制作规范与已有记忆。项目名为“测试项目”，镜头为 sh010。

### 开始制作

提供目标、参考和工程路径，例如：

> 使用 fx-agent 制作参考图中的烟雾。项目名为“广告A”，镜头为 sh010，工程目录为 D:/FX/广告A/sh010。先分析参考并做低分辨率草稿；修改前登记任务、读取规范并保存方案。

工作流先登记目标与验收条件，读取相关规范和知识，再执行修改；完成后保存工程、检查结果并归档。最终方案经用户确认后才提炼为成功经验。续作时提供相同项目名和镜头名。

更多 [Houdini / Nuke 示例与完整使用指南](public-docs/使用指南.md)，以及 [任务门禁说明](public-docs/任务门禁与学习提问.md)。

## 数据与运行边界

默认数据根为仓库中的 `data/`，可通过绝对路径环境变量 `FX_AGENT_DATA_ROOT` 指定共享目录。数据包含制作记忆、SQLite 数据库及明确提供的归档文件。升级前备份数据；多设备合并见 [数据合并指南](public-docs/全局经验与数据合并.md)。

多台设备同时使用时，请启用 [多设备共享数据服务](public-docs/多设备共享数据服务.md)：知识与案例共用，DCC 操作、任务和进度按设备隔离，运行数据库由服务主机本地磁盘管理。不要让多台客户端直接打开 NAS 上同一个 SQLite 文件。

`data/`、`output/`、本机配置、工程与缓存不纳入源码版本控制。归档不自动查全外部依赖，也不包含未保存的软件内存状态。连接检查、单元测试和结构检查分别有验证边界，不能代替真实画面验收。

桥接仅供可信的本地客户端使用，具有软件内操作与执行代码的能力。不要直接暴露到公网；具体说明和漏洞报告方式见 [SECURITY.md](SECURITY.md)。

## 本地开发

```powershell
uv sync --frozen
uv run fx-agent --version
uv run ruff check src tests scripts
uv run pytest --basetemp=.tmp/pytest
```

Nuke 可选依赖使用 `uv sync --frozen --group nuke`。`uv run fx-agent` 启动 stdio MCP 服务。提交步骤、测试要求和第三方代码引入规范见 [贡献指南](CONTRIBUTING.md)。

## 文档导航

- [全部文档](public-docs/README.md)：安装、工作流、工程学习、数据合并与开发验证。
- [上游接入基线](public-docs/上游依赖.md)：固定版本、提交及适配范围。
- [第三方声明与致谢](THIRD_PARTY_NOTICES.md)：直接复用、间接来源、设计借鉴和评估候选。
- [依赖许可证清单](public-docs/依赖许可证清单.md)：全部 94 个锁定依赖包。

## 许可证与致谢

FX Agent 原创部分采用 [MIT License](LICENSE)。第三方代码保留原作者版权、许可证和 NOTICE，完整来源见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

特别感谢 [fxhoudinimcp](https://github.com/healkeiser/fxhoudinimcp)、[JTCHE/houdini-mcp](https://github.com/JTCHE/houdini-mcp)、[kleer001/houdini-mcp](https://github.com/kleer001/houdini-mcp)、[kleer001/nuke-mcp](https://github.com/kleer001/nuke-mcp)、[AutoMaya](https://github.com/CallMeParams/automaya-mcp)、[chadrik Maya MCP](https://github.com/chadrik/maya-mcp-server) 和 [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)，以及依赖与研究参考项目的贡献者。

商业软件、客户工程、参考素材及其他第三方内容不因本仓库采用 MIT 而取得授权。发行内容应排除未经授权的厂商源码与私人数据。

## 联系方式

- 作者：[Zylvia-lin](https://github.com/Zylvia-lin)
- 微信：`718422052`
- 邮箱：[mlinchun@icloud.com](mailto:mlinchun@icloud.com)
- 问题与建议：[GitHub Issues](https://github.com/Zylvia-lin/fx-agent/issues)

涉及安全漏洞、版权或私人材料时，请优先通过邮箱联系。

## Star History

如果项目对你有帮助，欢迎 Star 并分享使用反馈。

[![FX Agent Star History](https://api.star-history.com/chart?repos=Zylvia-lin/fx-agent&type=date&legend=top-left)](https://www.star-history.com/#Zylvia-lin/fx-agent&Date)

图表由 [Star History](https://github.com/star-history/star-history) 提供。图像不可用时可点击进入图表页面；服务认证、限流或仓库可见性变化可能影响显示，未在仓库中保存任何访问令牌。
