# 第三方声明与致谢

审查日期：2026-11-10。感谢下列项目及贡献者。FX Agent 原创部分采用 [MIT](LICENSE)；第三方代码、文档和依赖继续遵循各自的许可证。本清单不将第三方版权归属于 FX Agent。

本表根据源码、安装器、锁文件及既有来源记录整理；不构成全部代码均为原创或不存在侵权的保证。发布前应核对当前文件及历史中的第三方授权与版权声明。

## 直接复用、改写和运行依赖

| 项目 / 作者 | 固定版本或提交 | 使用方式与本项目位置 | 许可证与版权保留 |
| --- | --- | --- | --- |
| [healkeiser/fxhoudinimcp](https://github.com/healkeiser/fxhoudinimcp) / FXHoudini-MCP Contributors | `2.25.0`；`2278840713996159333df75b8202dc3fc1c35fe2` | Houdini 执行层依赖；`server.py`、`production.py` 等作宿主适配。另从上游改写启动模块 `integrations/houdini/startup/startup.py`，增加端口兼容 | MIT；Copyright (c) 2026 FXHoudini-MCP Contributors。完整 [LICENSE](integrations/houdini/startup/LICENSE) 与 [NOTICE](integrations/houdini/startup/NOTICE) 随源码保留；依赖发行包也保留其许可 |
| [JTCHE/houdini-mcp](https://github.com/JTCHE/houdini-mcp) / Capoom、John Chedeville | 经 `fxhoudinimcp==2.25.0` 间接引入；未单独锁定该仓库提交 | 上游 `geometry_handlers.py` 的体积统计、采样和场比较改编自 `src/houdinimcp/handlers/volumes.py`；FX Agent 不另行复制此模块 | MIT；Copyright (c) 2025 Capoom；Copyright (c) 2026 John Chedeville。声明及许可全文见上游随附 [NOTICE](integrations/houdini/startup/NOTICE) |
| [kleer001/houdini-mcp](https://github.com/kleer001/houdini-mcp) / Capoom | `23bc40ebafc18399ff50cfed814e636419f4defb` | 复制 `scripts/hip_parser.py` 至 `src/fx_agent/vendor/kleer_hip_parser.py`；调整文本解码为 UTF-8，其余工程适配在本项目模块中实现 | MIT；Copyright (c) 2025 Capoom。完整 [LICENSE-kleer001.txt](src/fx_agent/vendor/LICENSE-kleer001.txt) 随 vendor 源码保留 |
| [kleer001/nuke-mcp](https://github.com/kleer001/nuke-mcp) / kleer001 | `6cbed80a2f35b997263fc51c988c5bb1405657fd`；包版本 `0.1.0` | `nuke` 可选组使用连接、版本解析和五个工具模块；`scripts/install_nuke.py` 从固定提交下载 `nuke_addon/nuke_mcp_addon.py` 并校验 SHA-256，本项目以独立适配层扩展 | MIT；Copyright (c) 2026 kleer001。审查副本见 [nuke-mcp-MIT.txt](licenses/nuke-mcp-MIT.txt)；安装器同时下载 `LICENSE.nuke-mcp`，分发桥接时必须一并保留 |
| [CallMeParams/automaya-mcp](https://github.com/CallMeParams/automaya-mcp) / Adam Waters | `5363aa3e4b506183ab79cc985c5da952b7eb1525` | 选择性改写长度前缀 JSON 协议、请求 ID、Arnold 灯光映射、曝光、HDRI 连接及单帧序列渲染；位于 `src/fx_agent/maya/` 和 `integrations/maya/py2/`、`py3/` | MIT；Copyright (c) 2026 Adam Waters。[桥接许可](integrations/maya/licenses/AutoMaya-MIT.txt)、[包内许可](src/fx_agent/maya/licenses/AutoMaya-MIT.txt) 均保留 |
| [chadrik/maya-mcp-server](https://github.com/chadrik/maya-mcp-server) / Chad Dombrova | `5eaa53752fd0ac7f69403843a237c13336222d8e` | 参考会话管理、身份信息及执行结果捕获，重新实现 FX 多会话协议；不运行其 commandPort / Qt 服务 | MIT；Copyright (c) 2026 Chad Dombrova。[桥接许可](integrations/maya/licenses/chadrik-MIT.txt)、[包内许可](src/fx_agent/maya/licenses/chadrik-MIT.txt) 均保留 |
| [modelcontextprotocol/python-sdk](https://github.com/modelcontextprotocol/python-sdk) / Anthropic, PBC 及贡献者 | 声明 `mcp>=1.14.0,<2`；锁定 `1.30.0` | MCP 协议、工具注册、传输和服务生命周期的直接依赖 | MIT；Copyright (c) 2024 Anthropic, PBC。许可由依赖发行包携带；详见 [Python 依赖清单](public-docs/依赖许可证清单.md) |

2026-11-10 从上述五个直接复用仓库的固定提交下载根许可证核对：Houdini 启动模块、HIP 解析器及两套 Maya 许可与本仓库保留文本一致（忽略末尾空白）；Nuke 许可与安装器固定的 SHA-256 一致。Maya 的具体来源说明另见 [THIRD_PARTY.md](integrations/maya/THIRD_PARTY.md) 与 [包内 NOTICE](src/fx_agent/maya/NOTICE.md)。

复制单个模块、桥接文件或制作发行包时，不能只复制实现文件而遗漏对应版权、许可和 NOTICE。根目录 MIT 不覆盖或替换这些声明。

## 全部 Python 依赖及开发工具

[依赖许可证清单](public-docs/依赖许可证清单.md) 覆盖 `uv.lock` 中除本项目外的全部 **94 个包**，包括直接、传递、Nuke 可选、开发和平台条件依赖，并列出仓库及许可证证据。构建后端、安装辅助工具和嵌套组件的边界也在该文档中说明。

这些依赖的许可证并非全部 MIT。例如 certifi 声明 MPL-2.0，部分依赖采用 Apache、BSD 或组合授权。源码仓库中的致谢表不能替代离线包、wheel 或整合分发物所需携带的许可证和 NOTICE；请按实际交付内容重新检查。

## 设计思路借鉴

以下来自既有研究记录：参考工作流和机制，在本项目重新实现；记录未表明复制这些项目源码或安装其运行时。对参考项目的致谢不授予未来复制其代码的权利。

| 项目 | 借鉴内容 |
| --- | --- |
| [lecopivo/another-houdini-mcp](https://github.com/lecopivo/another-houdini-mcp/tree/68a1da075f211c1022e7f56fd03114599063bf60) | 按需读取帮助、案例化制作笔记 |
| [anshul-vashist/Houdini-Agent](https://github.com/anshul-vashist/Houdini-Agent/tree/f2ced139b463c7987271a08fa4187075a8b624eb) | 执行前的确定性参数与路径校验 |
| [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent) | 经验形成技能、使用后修订的闭环 |
| [3clyp50/autodream](https://github.com/3clyp50/autodream) | 记忆整理计划、来源保留、推断边界 |
| [itpartypattaya/hermes-dreaming](https://github.com/itpartypattaya/hermes-dreaming) | 脚本提出建议、宿主经正常工具应用 |
| [HKUDS/AutoAgent](https://github.com/HKUDS/AutoAgent) | 演进后验证的思路 |
| [HKUDS/OpenSpace → AgentPilot](https://github.com/HKUDS/AgentPilot/tree/6b84b0689ca0d9e7b1e4061188ff6b60758e0f8a) | 任务轨迹、候选演进与结果验证 |

## 仅评估、未采用的候选

这些仓库列于既有评估记录，不能据此认定为 FX Agent 的依赖或已获授权的源码：

- Houdini：[OtakuSquirrel/houdini-mcp](https://github.com/OtakuSquirrel/houdini-mcp)、[Kazama-Suichiku/Houdini-Agent](https://github.com/Kazama-Suichiku/Houdini-Agent)。此前未核实到根级许可证，未迁移源码。
- Nuke：[joshw051/nuke-mcp](https://github.com/joshw051/nuke-mcp)、[dcc-mcp/dcc-mcp-nuke](https://github.com/dcc-mcp/dcc-mcp-nuke)、[dughogan/nuke_mcp](https://github.com/dughogan/nuke_mcp)、[linchpinhq/nuke-mcp](https://github.com/linchpinhq/nuke-mcp)。仅作候选入口与文档比较。

## 文档服务与商用软件

- README 的 Star 曲线使用 [star-history/star-history](https://github.com/star-history/star-history) 提供的外部服务，未复制其源码；可用性受公开仓库状态、服务认证和限流影响。
- Houdini、Nuke、Maya、Arnold、Redshift 和 Codex 属于各自权利人的软件或服务，需另行安装并取得适用授权。其安装文件、厂商源码、资产和许可不由本项目的 MIT 授权。
- 商业软件安装目录中的源码及其本地修补副本，只有取得允许再分发的授权后才能加入公开仓库或发行包。

如发现遗漏、来源错误或授权争议，请通过 [mlinchun@icloud.com](mailto:mlinchun@icloud.com) 联系维护者，附文件路径、原始来源和许可证依据。
