# FX Agent

供编程助手调用的本地 FX Agent：整合 Houdini 工具、制作记忆与工程案例归档，并提供 Nuke 15.2 独立合成入口。

当前宿主助手负责规划、操作与效果判断，Agent 提供 MCP 能力、工作流与共享记忆，不另建模型调度器。Codex 插件是可选分发方式，不再是安装前提。

开发设计见 [docs/开发方案.md](docs/开发方案.md)，复用基线见 [docs/上游依赖.md](docs/上游依赖.md)。

首次安装与升级见 [Windows Codex 全局安装](docs/全局安装与首次连接.md)。当前仓库路径作为代码入口，固定数据目录作为所有客户端共用的状态源。

Windows 可直接双击根目录 `Install-FX-Agent.bat`，一键安装 Houdini/Nuke 桥接与 Codex 全局入口；PowerShell 参数和预览方式见 [一键安装](docs/一键安装.md)。安装前保存工程并关闭对应软件/桥接。

## 在 Codex 中使用

### 1. 首次安装

将整个工程放在长期使用的位置，预先安装 Houdini、Nuke15.2+ 和 Codex。Houdini 至少启动过一次以生成偏好目录。保存工程并关闭软件/桥接后，双击根目录 `Install-FX-Agent.bat`，它会安装依赖和桥接、配置当前 Codex 用户的全局入口，并保留旧配置备份。

也可以在工程目录运行：

```powershell
# 只预览，不下载或改配置
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\Install-FXAgent.ps1 -Plan

# 安装 Houdini 和 Nuke 桥接
.\Install-FX-Agent.bat
```

缺少 Git/uv 时会通过 WinGet 补齐，软件主程序和许可证仍由用户安装。自定义目录、端口和单组件更新参数见 [一键安装](docs/一键安装.md)。

### 2. 打开软件并刷新 Codex

启动 Houdini 和 Nuke；只做某一软件的任务时打开对应软件即可。安装或更新后重启 Codex，再新建聊天，以加载全局 MCP 与工作流。

两个入口分别是 `fx-agent`（Houdini 与共同记忆）和 `fx-nuke`（Nuke 操作），默认桥接端口为18186、8187。不需要另外启动独立模型或 Agent 程序，由当前 Codex 负责规划、操作和效果判断。

首次聊天可以直接发送：

> 使用 fx-agent。先检查 Houdini 和 Nuke 的连接，读取制作规范和已有记忆。项目名为“测试项目”，镜头为 sh010，后续制作按对应工作流执行。

### 3. Houdini 制作示例

附上参考图片、视频或工程路径，然后说明目标：

> 使用 fx-agent 制作参考图中的烟雾。项目名为“广告A”，镜头为 sh010，工程目录为 D:/FX/广告A/sh010。先分析参考并做低分辨率草稿；文件读取遵守 $HIP 规范，缓存按既定 File Cache 路径保存。

明确指定工作流时，可以用 `$fx-workflow`。参考工程先只读学习，实际试验使用工作副本。需要更高精度或最终渲染时，继续说明目标与允许的计算范围。

### 4. Nuke 合成示例

> 使用 fx-agent 的 Nuke 工作流。项目名为“广告A”，镜头为 sh010，参考这个最终效果，用我提供的 EXR 和 NK 模板完成合成。先检查素材、帧段、通道、色彩空间和预乘，再开始调整。

明确指定合成工作流时，可以用 `$nuke-workflow`。Nuke 与 Houdini 分别召回经验和保存进度，共用同一个数据根。只有 Nuke 发现可能的源渲染问题时，才记录问题和证据，转交 Houdini 检查；修复后回到 Nuke 复查，不自动重渲染。

### 5. 纠错、记忆与续作

制作过程中直接提出修改，例如“烟雾边缘太亮，背景不要一起提亮”，不需要额外说“记住”。Codex 按工作流提炼有价值的信息：

- 明确的长期要求保存为带原话和版本的规范。
- 纠错保存问题、实际调整、原因和结果；没有结论的尝试不冒充成功方法。
- 最终效果经确认，且已保存工程和预览并归档后，提炼为可复用经验。跨项目的同类效果可先检索，再核对适用条件。

当前是 Codex 在阶段边界主动调用记忆工具，不是后台监听所有聊天。完整工程和图片归档仅覆盖明确提供的文件，不自动查全所有外部依赖。

### 模块准则与总结经验

原 Houdini MCP 的模块工作流准则已经保留：旧安装与当前依赖的36份Markdown比对无缺失、无内容变化，其中31份是工作流指南。这些是制作基线，不计作个人学习记忆。

总结经验按功能模块分类，支持多个标签，例如：

| 模块 | 标签 | 对应原准则主题 |
| --- | --- | --- |
| SOP 建模、属性、变形 | `sop` | `model` |
| 粒子 / POP | `particles` | `dopparticles` |
| 烟雾、火焰、爆炸 | `pyro` | `pyro` |
| 流体 / FLIP | `flip` | `fluid` |
| 破碎 / 刚体 | `rbd` | `destruction` |
| 海洋与海浪 | `ocean` | `ocean` |

其余模块可通过 `fx_get_module_catalog` 查询。比如程序化卷浪可标记 `sop + ocean`，不因为外观像水就归为 FLIP；已有无标签记录保持“未分类”，不猜测归类。

最终方案经用户确认、工程及预览归档并完成经验提炼后，**会自动更新对应模块的 MD 补充文档**：

```text
data/modules/global/houdini/sop.md
data/modules/global/houdini/ocean.md
```

文档写入精炼方法、适用条件、原因和证据ID，不重复复制完整聊天。后续工作流读取“原模块准则＋全局模块补充”，必要时才展开单条经验。反馈撤销旧方案时，模块MD也会移除旧总结；区块外的手写备注保留。

原依赖内的MD不直接修改，避免重装或升级丢失学习成果。补充文档与记忆库一起迁移，经验全局共享，项目名记录来源；参数是否适用由尺度、版本、输入等条件决定。未分类、仅当前镜头适用、未确认或尚未提炼的方案不写成通用模块准则。

若工具返回 `module_guides.state=pending_rebuild`，说明经验已保存但MD未同步；调用 `fx_refresh_module_guides` 重建，不要把它当作经验丢失。读取模块补充用 `fx_get_module_knowledge`，默认总预算4000字符；检索经验和纠错也可按模块过滤。实际调用细节见 [模块工作流与学习同步](docs/模块工作流与学习同步.md)。

模块补充会显示经验争议，返回的 `has_conflict` 与冲突ID在正文截断后仍保留。提炼结果缩小模块标签时，其他模块仍可召回原经验。读取未变化的模块会复用已校验文档；数据库或文件变化后重新同步。本轮修复与性能验证见 [知识链路优化记录](docs/testing/12-knowledge-optimization.tdd.md)。

本功能增加了MCP工具和参数；当前制作结束后重启Codex加载新版。`fx_status` 返回 `module_taxonomy_version=1` 时表示新版本已加载，不需要重启或改动Houdini工程。

续作时提供相同项目名和镜头名：

> 继续“广告A”的 sh010。先读取上次进度和相关记忆，再检查当前工程现场，说明上次做到哪里，然后继续。

记忆、规范、纠错和案例位于当前工程 `data/`，不会推送到 Git。多个客户端必须指向同一个数据目录。换设备时停止写入后整体复制工程与 data，重建环境并重新安装入口；不要复用旧电脑的 `.venv`，工程内外部路径仍需核对。

### 6. Nuke 的 MCP Server 菜单

Nuke 启动时自动 Start，不再显示独立 NukeMCP 浮窗。顶部 **MCP Server** 菜单提供：

- 状态与端口：Running、Stopped；启动/停止过程中显示 Starting/Stopping，异常显示 Error。
- **Start**：启动桥接；已运行时不会重复启动。
- **Stop**：停止监听并断开 MCP 连接，不关闭 Nuke 或修改工程。

日志输出到 Nuke 启动时附带的控制台，使用 `[FX Agent Nuke]` 前缀，不重定向到 Script Editor。没有附带控制台时不会另外创建日志窗口。若其他插件已使用同名菜单，本项目的操作位于其 FX Agent 子菜单中。

Stop 后再次 Start，若 Codex 报原连接已断开，重启 Codex 以建立新连接。更新桥接代码后重启 Nuke 加载最新版。连接失败时先核对软件是否打开、菜单是否 Running、端口是否冲突。

需要命令行只读检查时，在工程目录执行：

```powershell
.venv\Scripts\python.exe scripts/check_houdini.py
.venv\Scripts\python.exe scripts/check_nuke.py
```

这些检查只证明连接与信息查询成功，不代表已完成特效、合成或画面验收。

## 本地开发与运行

需要 Python 3.12+、`uv`，以及已安装并运行 Houdini 端桥接插件的 Houdini。当前固定复用 `fxhoudinimcp==2.25.0`；安装器会备份并替换旧 MCP 注册，保留旧虚拟环境供回退。

```powershell
uv sync --frozen
uv run fx-agent --version
uv run fx-agent
```

最后一条命令启动 stdio MCP 服务，不是交互式命令行。插件 MCP 配置通过 `${PLUGIN_ROOT}` 定位项目，并使用锁文件启动。首次运行需要安装依赖，正式离线分发前需要准备依赖。

```powershell
uv run python scripts/check_houdini.py
New-Item -ItemType Directory -Path .tmp -Force | Out-Null
uv run pytest --basetemp=.tmp/pytest
uv run ruff check src tests scripts
```

只读冒烟检查固定连接 `127.0.0.1:18186`；可通过 `HOUDINI_PORT` 指定其他端口。它会实际启动本项目 MCP、握手、列工具并读取场景，不创建或修改节点。

默认数据目录为当前工程内的 `data/`（运行数据已被 Git 忽略），可用绝对路径环境变量 `FX_AGENT_DATA_ROOT` 覆盖。默认值跟随代码位置，不依赖启动工作目录。建议安装时显式配置固定绝对目录。`fx_status` 不创建该目录，持久化操作会在其中创建 SQLite 数据库或案例文件。

## 接入状态

已提供根目录 `plugin.json`、`mcp.json` 与 `skills/fx-workflow/SKILL.md`。支持通过 `scripts/install_codex.py` 将当前工程全局注册到 Codex，替换旧 `fxhoudini` 注册和 Houdini 启动包，并将四个全局工作流目录链接至仓库，包含工程学习 `fx-project-learn`。根目录插件清单作为可选适配保留；请勿同时启用重复入口。其他宿主的安装器尚未实现，不能据此宣称已兼容所有客户端。

已提供 `fx_save_checkpoint`、`fx_get_checkpoint`、`fx_get_context`：按项目和镜头保存进度，使用修订号防止旧状态覆盖，摘要按 JSON 字符预算返回并支持内容哈希去重。检查点只记录工程路径，不等于工程副本或实时验证。

已提供 `fx_archive_case`、`fx_get_case`、`fx_validate_case`：复制明确指定的已保存 HIP、图片与依赖文件，按项目隔离案例，生成 SHA-256 清单，默认只返回摘要。复制失败或及时取消时清理暂存目录；读取时拒绝无效清单。

归档默认单批上限 512 MiB，可显式设置 `max_total_bytes`。当前只验证提供的文件副本，不自动发现所有依赖，不包含未保存的 Houdini 内存修改，也不声明工程可打开或视觉可复现。取消不能撤销取消请求前已完成发布的案例。

已提供 `fx_submit_experience`、`fx_search_memory`、`fx_get_memory`：阶段候选由 Codex 提炼，服务按规则筛选、去重和持久化。推测默认不召回；明确用户陈述与有归档输出的观察有独立来源标记，均不冒充视觉验证。工程相关经验需要归档 HIP。经验默认全局检索，project_id 只提供当前任务上下文；旧镜头限定条目仍保留原范围。先返回受字符预算限制的摘要，支持中文与英文关键词。

用户纠正通过 `supersedes` 建立新修订；旧聊天、跨镜头替代和并发重复纠正受到保护。历史内容可以作为新修订恢复，相同请求重试保持幂等。

当前依靠配套 Skill 在阶段边界调用，尚未实现全部 Houdini 操作的自动事件记录、语义向量检索或自动查全依赖。已提供只读模板导入、静态提取、检索、候选经验与原工程工作副本导出；用 `fx_plan_template_import` 盘点，`fx_learn_templates` 学习，所有数据仍在 data。混合 HIP/NK 目录、精简归档、修订与迁移合并详见 [工程模板学习与迁移](docs/工程模板学习与迁移.md)。Nuke 15.2 接入与验证范围见 [Nuke接入与独立学习](docs/Nuke接入与独立学习.md)。

工程导入后还需逐张标注实际效果：`fx_annotate_template_effects` 保存对象、外观、动态、参数/动画/渲染差异及复用步骤；同一制作方案的不同效果使用独立卡。用户用“角色身体能量粒子”等描述时，通过 `fx_search_effects` 找相似卡，再追溯原工程。基础规范在data/baselines中仅维护当前内容，可随data手动迁移；使用 `fx_sync_module_baselines` 同步新增、修改和删除，不再生成project-2等历史目录。详见 [效果指示卡与基础规范](docs/效果指示卡与基础规范.md)。

## 对话与制作反馈学习

配套 `fx-learning` Skill 在用户提出长期规范、纠错和最终确认时触发相应流程：

- `fx_record_user_rule`：带原话与版本的全局/项目规范；规则卡可翻页，`fx_get_user_rule` 展开全文。
- `fx_record_feedback`：问题、调整、原因、结果与用户反馈，保留修订历史。
- `fx_get_learning_history` / `fx_get_feedback`：先摘要和游标，再单条完整轨迹。
- `fx_finalize_learning`：当前 accepted 结果关联 HIP 与预览后，提炼带适用条件的方案。默认 reuse_scope=global，跨项目复用；工程特例可限定 shot。旧 project 参数作为 global 别名兼容。
- `fx_dream_memory`：全局分页读取；由当前助手展开来源后提交 merge/refine/split/conflict/supersede 整理方案，或应用精确重复索引。保留来源、校验快照，支持按 run_id 撤销。

该流程由 Codex 提炼和调用，服务不直接抓取所有聊天，不启动第二个模型或自动训练权重。规则文本持久化不代表程序校验自动改写。Dream 按游标分批整理，不再因超过 500 条而停用；语义判断由当前助手完成。

合成对照中重复卡片从 2 降到 1、返回字符从 539 降到 300，保留两个来源；这不证明真实 FX 制作耗时下降。开源候选取舍见 [评估报告](docs/开源MCP候选评估.md)，完整机制见 [自我迭代方案](docs/自我迭代与开源借鉴方案.md)。

工程读取使用 `$HIP/...`；File Cache 使用 `$HIP/geo/$OS/$OS.$F4.bgeo.sc`。新增桥接前置检查与缓存输出核对，详见 [工程制作规范](docs/工程制作规范.md)。上游 `import_file` 因路径语义不兼容暂拒绝，使用 `build_network` 创建相应读取节点。该检查不构成任意 Python、HDA 或手动操作的全局沙箱。

运行验证和未完成项见 [docs/开发记录.md](docs/开发记录.md) 与 [docs/testing](docs/testing)。

运行数据、工程、缓存和本机配置不提交至代码仓库。


## 多设备经验合并

使用 `fx_import_data(source="D:/FX-Import/device-B/data")` 预览，确认来源后传 `apply=true` 追加导入；也可运行：

```powershell
.venv\Scripts\python.exe scripts/import_data.py "D:/FX-Import/device-B/data" "D:/MyScripts/fx-agent/data"
# 实际追加
.venv\Scripts\python.exe scripts/import_data.py "D:/FX-Import/device-B/data" "D:/MyScripts/fx-agent/data" --apply
```

停止写入源归档文件的进程后执行。完全相同记录跳过；分歧保留本地与完整来源快照，并可通过 `fx_get_import_records` 展开。冲突经验暂停默认召回，由当前助手核对来源后用 `fx_resolve_import_memory` 选择版本；不会按设备时间覆盖。随后由助手执行 Dream，并刷新全局模块知识。

完整行为、旧数据兼容和回退见 [全局经验与数据合并](docs/全局经验与数据合并.md)。
