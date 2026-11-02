# FX Agent

供 Codex 操作的本地 FX 制作插件：整合 Houdini 工具、制作记忆与工程案例归档，后续接入 Nuke。

Codex 负责规划、操作与效果判断，插件提供 MCP 能力与配套 Skill，不另建模型调度器。

开发设计见 [docs/开发方案.md](docs/开发方案.md)，复用基线见 [docs/上游依赖.md](docs/上游依赖.md)。

## 本地开发与运行

需要 Python 3.12+、`uv`，以及已安装并运行 Houdini 端桥接插件的 Houdini。当前固定复用 `fxhoudinimcp==2.25.0`；旧 MCP 环境不会被修改。

```powershell
uv sync --frozen
uv run fx-agent --version
uv run fx-agent
```

最后一条命令启动 stdio MCP 服务，不是交互式命令行。插件 MCP 配置通过 `${PLUGIN_ROOT}` 定位项目，并使用锁文件启动。首次运行需要安装依赖，正式离线分发前需要准备依赖。

```powershell
uv run python scripts/check_houdini.py
uv run pytest --basetemp=.tmp/pytest
uv run ruff check src tests scripts
```

只读冒烟检查固定连接 `127.0.0.1:8100`；可通过 `HOUDINI_PORT` 指定其他端口。它会实际启动本项目 MCP、握手、列工具并读取场景，不创建或修改节点。

默认数据目录为 `%LOCALAPPDATA%/fx-agent`，可用绝对路径环境变量 `FX_AGENT_DATA_ROOT` 覆盖。状态查询不创建该目录，持久化功能后续接入。

## 插件状态

已提供根目录 `plugin.json`、`mcp.json` 与 `skills/fx-workflow/SKILL.md`。当前验证范围为本机代码、MCP 协议和只读连接；桌面端插件安装与新聊天激活尚未验证。暂不替换旧的 `fxhoudini` 配置，也不要同时启用两份重复工具。

记忆、检查点、工程与素材归档正在后续迭代中实现。Nuke 尚未接入。

运行数据、工程、缓存和本机配置不提交至代码仓库。
