# FX Agent

供编程助手调用的本地 FX Agent：整合 Houdini 工具、制作记忆与工程案例归档，后续接入 Nuke。

当前宿主助手负责规划、操作与效果判断，Agent 提供 MCP 能力、工作流与共享记忆，不另建模型调度器。Codex 插件是可选分发方式，不再是安装前提。

开发设计见 [docs/开发方案.md](docs/开发方案.md)，复用基线见 [docs/上游依赖.md](docs/上游依赖.md)。

首次安装与升级见 [Windows Codex 全局安装](docs/全局安装与首次连接.md)。当前仓库路径作为代码入口，固定数据目录作为所有客户端共用的状态源。

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

只读冒烟检查固定连接 `127.0.0.1:8186`；可通过 `HOUDINI_PORT` 指定其他端口。它会实际启动本项目 MCP、握手、列工具并读取场景，不创建或修改节点。

默认数据目录为用户主目录下的 `.fx-agent`，可用绝对路径环境变量 `FX_AGENT_DATA_ROOT` 覆盖。默认值不使用可能被沙箱逐次替换的 `LOCALAPPDATA`。建议安装时显式配置固定绝对目录。`fx_status` 不创建该目录，持久化操作会在其中创建 SQLite 数据库或案例文件。

## 接入状态

已提供根目录 `plugin.json`、`mcp.json` 与 `skills/fx-workflow/SKILL.md`。支持通过 `scripts/install_codex.py` 将当前工程全局注册到 Codex，替换旧 `fxhoudini` 注册和 Houdini 启动包，并将两个全局工作流目录链接至仓库。根目录插件清单作为可选适配保留；请勿同时启用重复入口。其他宿主的安装器尚未实现，不能据此宣称已兼容所有客户端。

已提供 `fx_save_checkpoint`、`fx_get_checkpoint`、`fx_get_context`：按项目和镜头保存进度，使用修订号防止旧状态覆盖，摘要按 JSON 字符预算返回并支持内容哈希去重。检查点只记录工程路径，不等于工程副本或实时验证。

已提供 `fx_archive_case`、`fx_get_case`、`fx_validate_case`：复制明确指定的已保存 HIP、图片与依赖文件，按项目隔离案例，生成 SHA-256 清单，默认只返回摘要。复制失败或及时取消时清理暂存目录；读取时拒绝无效清单。

归档默认单批上限 512 MiB，可显式设置 `max_total_bytes`。当前只验证提供的文件副本，不自动发现所有依赖，不包含未保存的 Houdini 内存修改，也不声明工程可打开或视觉可复现。取消不能撤销取消请求前已完成发布的案例。

已提供 `fx_submit_experience`、`fx_search_memory`、`fx_get_memory`：阶段候选由 Codex 提炼，服务按规则筛选、去重和持久化。推测默认不召回；明确用户陈述与有归档输出的观察有独立来源标记，均不冒充视觉验证。工程相关经验需要归档 HIP。检索按项目和可选镜头隔离，先返回受字符预算限制的摘要，支持中文与英文关键词。

用户纠正通过 `supersedes` 建立新修订；旧聊天、跨镜头替代和并发重复纠正受到保护。历史内容可以作为新修订恢复，相同请求重试保持幂等。

当前依靠配套 Skill 在阶段边界调用，尚未实现全部 Houdini 操作的自动事件记录、全局经验共享、语义向量检索、自动查全依赖或模板自动学习。Nuke 尚未接入。

## 对话与制作反馈学习

配套 `fx-learning` Skill 在用户提出长期规范、纠错和最终确认时触发相应流程：

- `fx_record_user_rule`：带原话与版本的全局/项目规范；规则卡可翻页，`fx_get_user_rule` 展开全文。
- `fx_record_feedback`：问题、调整、原因、结果与用户反馈，保留修订历史。
- `fx_get_learning_history` / `fx_get_feedback`：先摘要和游标，再单条完整轨迹。
- `fx_finalize_learning`：当前 accepted 结果关联 HIP 与预览后，提炼带适用条件的方案。默认项目内跨镜头复用，也可限定当前镜头；不自动跨项目扩散。
- `fx_dream_memory`：预览和快照一致后整理完全重复检索卡，原文、来源与案例保留，可 reset。

该流程由 Codex 提炼和调用，服务不直接抓取所有聊天，不启动第二个模型或自动训练权重。规则文本持久化不代表程序校验自动改写。超过 500 条活动记忆时首版暂停新的 Dream 合并，仍可预览与重置。

合成对照中重复卡片从 2 降到 1、返回字符从 539 降到 300，保留两个来源；这不证明真实 FX 制作耗时下降。开源候选取舍见 [评估报告](docs/开源MCP候选评估.md)，完整机制见 [自我迭代方案](docs/自我迭代与开源借鉴方案.md)。

工程读取使用 `$HIP/...`；File Cache 使用 `$HIP/geo/$OS/$OS.$F4.bgeo.sc`。新增桥接前置检查与缓存输出核对，详见 [工程制作规范](docs/工程制作规范.md)。上游 `import_file` 因路径语义不兼容暂拒绝，使用 `build_network` 创建相应读取节点。该检查不构成任意 Python、HDA 或手动操作的全局沙箱。

运行验证和未完成项见 [docs/开发记录.md](docs/开发记录.md) 与 [docs/testing](docs/testing)。

运行数据、工程、缓存和本机配置不提交至代码仓库。
