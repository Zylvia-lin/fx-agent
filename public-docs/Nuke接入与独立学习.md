# Nuke 15.2 接入与独立学习

## 多窗口会话（2026-11-04）

同一个 fx-nuke MCP 现在支持列出并切换多个 Nuke 窗口。Nuke 启动时从配置基准端口起最多尝试16个端口，各实例独占实际端口；默认依次为8187、8188等。菜单显示实际监听端口，不再让两个实例共享同一个端口。`start(port=8187, auto_port=False)` 可要求严格固定端口，冲突时报 Error。

各实例在共同 `data/nuke_sessions/` 下登记 PID、实际端口、随机会话标识、当前工程路径和版本；每秒更新，停止时移除，超过30秒未更新的登记不作为可选窗口。渲染或模态操作长时间阻塞主线程时，列表可能暂时不显示该窗口；已经建立的连接不因此切换。登记仅是发现快照，选择时仍验证真实握手和工程路径。

- `list_nuke_sessions`：列出窗口快照，标明当前目标；不会探测并占用其他窗口的命令连接。
- `connect_nuke(port, pid, session_id, expected_script)`：从列表复制身份字段，明确切换；身份或工程变化时拒绝，健康原连接保持不变。
- `get_nuke_connection_status`：返回当前真实握手、工程和会话列表。多个窗口或没有窗口时，MCP仍可启动，等待明确选择；只有一条有效登记时尝试核验后自动连接，失败后仍保留发现/切换工具。

切换、任务目标检查和节点操作使用同一调用串行锁；当前连接发生通信超时后不再复用无请求ID的响应队列，需重新选择并建立连接。原任务绑定其他端口/工程时，切换不会让其自动获准修改新工程。

本机已热更新两个运行中的窗口，并通过同一个真实 stdio MCP 往返选择：sc232（PID73436，8187，679节点）→ sc184（PID84904，8188，174节点）→ sc232（679节点）。验证只读，不保存或修改NK。可复验命令：

```powershell
.venv\Scripts\python.exe scripts/check_nuke_sessions.py --final-script EP05_seq20_sc232_v02.nk
```

运行中的旧MCP进程需重新连接一次，才能加载新增工具；更新 Nuke 端桥接后重启对应软件窗口以加载新版。

目标版本为用户已安装的 Nuke 15.2v1。本机已完成全局接入、真实握手和当前工程只读查询；真实建图、保存、渲染与视觉检查尚未完成。现有旧版本未配置为本项目操作目标。

## 结构和职责

当前宿主助手操作两个软件的工具集；“Houdini Agent / Nuke Agent”指两套制作工作流，不是两个另行启动的模型调度器。

- `fx-agent`：原有 Houdini 操作及共同的记忆、规范、案例、学习和渲染问题记录。
- `fx-nuke`：独立 Nuke 操作入口，通过 stdio MCP 调用本机 Nuke 桥接。
- 所有客户端连接同一工程，共用 `data/memory.sqlite3` 和 `data/cases/`。实际存储被 Git 忽略，代码推送不会同步或公开生产资料。
- 经验、纠错与检查点指定 `software=houdini` 或 `nuke`，默认 Houdini 以兼容旧调用；同项目、同镜头的两套进度和调整不会互相覆盖。旧数据视为 Houdini。Dream 不把不同软件的方案合并。
- 共用用户规范保留真实来源，软件专属规范需明确写出软件。Houdini 的 `$HIP` 与 File Cache 约束不能直接用于 Nuke 文件旋钮。
- 案例支持 `.nk`，复制到 `projects/nuke/`；HIP 仍在 `projects/houdini/`。同一案例可明确提供两种工程，但每条经验都必须有对应软件的工程证据。

## 上游复用取舍

本轮选择 [kleer001/nuke-mcp](https://github.com/kleer001/nuke-mcp)，锁定提交 `6cbed80a2f35b997263fc51c988c5bb1405657fd`。使用其 `connection`、版本解析和 `graph/script/comp/render/batch` 五个工具模块；本项目继续使用现有 MCP Python SDK 注册接口，独立 stdio 进程隔离 Houdini 服务生命周期。

保留 25 项工具：基础节点与连线、工程读写和设置、常用合成链、批量调整、坏 Read 检查、Write/渲染，以及新增连接检查。只注册已接入的模块，未一次性搬入所有 3D、跟踪、Deep、ML 等能力。

上游自身的记忆、RAG、自动项目记忆、事件注册与设施插件加载没有调用，避免第二份数据库及不可控插件导入。任意 Python 与自动排版工具从注册表移除。Socket 原协议没有请求 ID，本项目额外串行化调用；通信超时后不自动重试，需先确认上次操作是否完成，再重启连接，防止重复建图或错配响应。这个限制不能阻止其他独立客户端并发修改同一工程，仍需宿主协调。

可核实的收益是：从无 Nuke 操作入口到上述合成工具可通过协议调用；离线测试验证节点与批量旋钮修改、工具筛选、版本拒绝、工程证据和软件隔离。真实握手已通过；仍未证明真实合成效率、画质或全部 25 项工具在 Nuke 15.2 均可用，不能用工具数量作为制作优化的唯一依据。

用户提供的其他候选保留为评估材料：[joshw051/nuke-mcp](https://github.com/joshw051/nuke-mcp)、[dcc-mcp-nuke](https://github.com/dcc-mcp/dcc-mcp-nuke)、[dughogan/nuke_mcp](https://github.com/dughogan/nuke_mcp)、[linchpinhq/nuke-mcp](https://github.com/linchpinhq/nuke-mcp)。本轮只做这些候选的入口/文档核对，没有运行其安装器或宣称多版本实测；后续发现当前适配缺少已证实必要的能力时再逐项比较，不叠加多套工具和记忆服务。

## 安装与端口

Houdini 默认保留用户指定的 `18186`，新 Nuke 桥接默认 `8187`。不使用旧 Houdini 的 `8100` 或候选 Nuke 的 `54321`；两个客户端入口均使用 stdio，桥接端口不是 HTTP MCP URL。

首次准备：

```powershell
Set-Location D:\MyScripts\fx-agent
uv sync --frozen --group nuke
.venv\Scripts\python.exe scripts/install_nuke.py --prepare
```

`--prepare` 只下载固定提交的桥接与 MIT 许可到 `integrations/nuke/vendor/`，验证 Git 原始文件的 SHA-256，不修改用户启动文件或运行 Nuke。该目录被 Git 忽略，完整工程包复制时可以一并带走；Git clone 后需重新准备。可选依赖组不改变普通 Houdini 安装的默认依赖；执行普通 `uv sync` 会移除未选的 Nuke 组，使用 Nuke 时需继续带 `--group nuke`。

Nuke 15.2 尚未安装时，可以 `--register-disabled` 只预注册 `enabled=false` 的 Codex MCP 入口，保留旧软件启动配置不变。软件安装后先关闭相关桥接，再执行：

```powershell
.venv\Scripts\python.exe scripts/install_nuke.py --port 8187
.venv\Scripts\python.exe scripts/install_nuke.py --port 8187 --apply
```

第一条只预览，第二条更新 `~/.codex/config.toml` 的 `fx-nuke` 与 `~/.nuke/menu.py` 内受管理区块，原配置备份在 `~/.fx-agent-install-backups/`。区块只在 Nuke 15.2+ 启动桥接，旧版本跳过。现有菜单代码保留；异常标记、无效 Python/TOML 会停止，不能安全迁移时不覆盖。

安装时配置的基准端口须与 FX Houdini 配置不同；多窗口启动时按上述规则分配实际端口。可以统一用 `--port` 选择其他基准端口，安装器将同时更新桥接启动和客户端参数。不得猜测目标实例；使用会话列表和显式选择。桥接只监听 `127.0.0.1`；随机会话标识用于识别实例，不是身份认证，也没有开放远程连接。

保存原工程后启动 Nuke 15.2，再新建 Codex 聊天；运行：

```powershell
.venv\Scripts\python.exe scripts/check_nuke.py
```

它读取握手和真实当前工程，不创建节点。后续还需在工作副本测试 Read/Grade/Merge/Write、保存重开、参考画面与实际 EXR 通道；有许可证限制时如实记录，不能用 mock 代替。

离线协议检查可运行 `scripts/check_nuke.py --mock`，其输出固定标注 `synthetic: true`、`live_verified: false`。模拟服务使用临时空闲端口，不占用正式 8187。

卸载 Nuke 入口使用 `codex mcp remove fx-nuke`，从 menu.py 移除 `BEGIN FX AGENT NUKE` 到 `END FX AGENT NUKE` 的实际注释区块，保留其他启动代码；需要回退则按本轮备份清单恢复文件，恢复前比较之后的新修改。数据目录与学习记录不删除。

## 唯一必要的跨软件反馈

日常任务独立，发现源渲染问题才交接：

1. Nuke 先检查自身 Read、代理、色彩空间和预乘使用，保留事实与推测的区别。
2. `fx_report_render_issue` 记录镜头、路径、帧段、预期/实际、来源和证据，状态 open。
3. 宿主按 Houdini 工作流核对现场，用 actor=houdini 更新 investigating；完成检查或修复后更新 awaiting_nuke_review。
4. Nuke 实际重读新版素材并复查后，用 actor=nuke 关闭；仍有问题则重新 open。更新必须提供证据和当前修订号。

问题单只保存事实声明与历史，不调用另一个软件、不自动重渲染、不将调用方填写的证据视作服务已验证。需要长期保留的图片、NK、HIP 与日志先归档，证据字段记录案例和文件引用。通过摘要列表按需展开，不将所有往返历史塞进上下文。

同一操作者可以切换工作流完成闭环；该 actor 字段是职责记录，不是用户身份认证。大规模后台自动调度、全过程视觉判定与所有外部序列依赖自动复制不包含在本轮。

## 本机实测记录

2026-11-02，已安装到当前工程并启用全局 fx-nuke，实际监听127.0.0.1:8187，握手Nuke15.2v1（PID69076），列出25项工具并成功读取当前工程信息。只读查询显示帧段101–162、25fps、120节点；未修改或保存该工程。

首次真实握手暴露上游 GUI 调度调用 executeInMainThread 不返回结果，导致连接关闭。依据本机 Foundry 安装自带 plugins/nuke_internal/executeInMain.py，兼容层改为 executeInMainThreadWithResult，并封装异常以保留错误回传；固定上游文件未修改。新增回归先失败后通过，真实握手随后通过。这是本轮实测确认的必要适配，不能由 mock 的成功推断原上游开箱即用。

当前上游 get_script_info 的 format 字段仍是格式对象字符串，不能据此确认分辨率。正式制作前还需验证实际尺寸、AOV/Alpha、色彩配置和视觉结果。

## 菜单与启动控制台

### .nk 工具集导入

`create_node(node_class="L_RealGlow", fx_task_id=...)` 默认先创建原生/插件类，只有未知类错误且没有产生节点时，才在 Nuke 的 `pluginPath()` 与 `~/.nuke/ToolSets` 中递归查找同名 `.nk` 并导入。许可证、回调、参数错误不会触发导入。同名文件有多个时必须通过 `toolset_path` 指定绝对路径；没有匹配时也可使用该参数指定其他目录的工具集。不执行菜单 Python 命令。

`import_nuke_toolset(path="D:/tools/L_RealGlow.nk", fx_task_id=...)` 可直接导入一个或多个节点，不替换当前工程。两种入口都支持 `name`、`knobs`、`position`；名称和参数只适用于单个顶层节点，位置用于平移整个导入块。自动回退要求工具集只有一个顶层节点（通常为 Group）。返回真实节点类、名称及来源路径；导入失败清理新增节点并恢复原选择。`.nk` 中的依赖与回调仍按 Nuke 原生导入规则执行，使用已信任的工具集。

更新后可调用 `reload_nuke_tools` 热加载 Nuke 端回退逻辑；新增工具及 `toolset_path` 参数需重新加载 Codex 的 fx-nuke MCP 服务（或重启 Codex）后出现。

不再创建独立 NukeMCP 面板。Nuke 默认自动 Start，顶部 MCP Server 菜单显示运行状态与端口，提供 Start/Stop；约一秒刷新一次，仅状态改变时更新菜单。若同名菜单已有其他插件内容，本项目放入其 FX Agent 子菜单，保留其他项目。

日志使用 Python 原始 stdout/stderr 写入 Nuke 启动控制台，前缀为 `[FX Agent Nuke]`；不改全局输出流，不调用 Script Editor 的输出接口，没有原始控制台时也不创建新窗口。

Stop 关闭监听与当前连接，已排队但尚未执行的操作拒绝继续；已经开始执行的软件操作不被描述为可撤销。停止期间不会在 GUI 线程等待工作线程。再次 Start 后旧客户端可能需要重启 Codex 以重新连接；更新代码后重启 Nuke 加载最新兼容层。
