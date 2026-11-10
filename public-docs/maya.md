# Maya 接入

正式工程为 `D:/MyScripts/fx-agent`。外部 Python 3.12 运行 `fx-maya` stdio MCP；`integrations/maya/py2`、`py3` 分别维护 Maya 端运行时代码，各自包含 scripts、plug-ins、fx_maya_bridge。当前制作优先使用 Maya 2020。

## 安装为共享 .mod

```powershell
.venv/Scripts/python.exe scripts/install_maya.py --modules-dir C:/Users/admin/Documents/maya/modules
.venv/Scripts/python.exe scripts/install_maya.py --modules-dir C:/Users/admin/Documents/maya/modules --apply
```

安装一次共享 .mod，参照用户 L_Tools.mod 的 MAYAVERSION/PLATFORM 分段配置：2017–2020 指向 py2，2022–2027 指向 py3。每个分支以 scripts: scripts 和 PYTHONPATH 注册自带 userSetup.py，GUI 启动后延迟加载 FX 插件并建立菜单。无需逐版本/语言修改 pluginPrefs.mel，不覆盖用户或其他插件的启动脚本。Maya 2020 原生 executeUserSetup 会遍历搜索路径执行各自的 userSetup.py。

默认先预览，`--apply` 注册 fx-maya MCP 和共享 fx_maya.mod，备份原文件并保留其他 DCC 配置。模块指向本工程独立 py2/py3 分支的绝对路径，搬迁工程后重新安装。不安装或替换 RS/MtoA。

正常启动 Maya 即由模块启动脚本加载 fx_maya_plugin.py，加载插件启动服务及菜单，卸载插件关闭服务。启动失败会在 Script Editor 输出 traceback 和警告，不静默隐藏错误。fxMayaBridgeStatus 返回当前身份。安装器同时链接 maya-workflow 技能。

GUI Maya 顶部新增 **FX Maya MCP** 菜单，打开菜单时读取实际运行状态、端口和 PID，并提供启动桥接、停止桥接、查看完整会话信息。按钮随实际状态启用/禁用；卸载插件移除菜单，batch Maya 不创建菜单。启停只控制当前 Maya 的 MCP 桥接。重启桥接可能改用其他空闲端口，客户端应重新发现并显式选择，其他 Maya 会话的桥接保持独立。

更新源码后，已有窗口各在 MEL 中执行一次 `unloadPlugin "fx_maya_plugin.py"; loadPlugin "fx_maya_plugin.py";`，刷新当前插件对象及菜单生命周期。随后日常启停直接使用菜单。

`--data-root` 必须与现有 FX 服务共用同一绝对目录。未指定时继承已配置 fx-agent 的 data root，随后使用 `data_root()`。会话广告位于共享根的 `maya_sessions/`；它只是端口线索，发现与选择始终实时探测，注册文件不证明身份。

默认桥接绑定 loopback 的空闲 `18770..18789`；显式设置 `FX_MAYA_PORT` 或安装器 `--bridge-port` 时冲突直接失败。外部 MCP `--ports` 可配置发现端口。未启动 HTTP 服务。Houdini 18186 与 Nuke 8187 保持原样。

## 会话与安全执行

### 由 Agent 新开 Maya

`start_maya(executable="C:/Program Files/Autodesk/Maya2020/bin/maya.exe")` 的参数列表只有 maya.exe，不传 -command、-script 等参数，不覆盖环境，不通过电脑界面操作。当前小屋制作选用 Maya 2020；插件由共享模块自带启动脚本加载。

此工具只启动一次，返回 status=launched、PID 和 bridge_ready=false，不宣称已经连接。随后 list_maya_sessions 并显式 connect_maya。会话身份附带 menu_exists 和 user_pref_dir，供只读检查菜单及实际语言目录。启动失败先检查错误，不自动重复启动。普通快捷方式与工具使用同一模块启动链路。模拟测试不启动软件；真实启动验收单独记录。

每个 MCP 进程独立维护目标。没有自动选窗。列表含软件/service 标记、协议版本、端口、PID、进程稳定 session_id、Maya/Python 版本、当前场景和已加载渲染器版本。`connect_maya` 复制全部身份字段并实时核对，失败保留原目标；每次操作在 Maya 主线程重新检查身份和场景。场景切换后重新选择和更新任务。

所有 Maya API，包括身份读取与插件查询，都通过主线程。GUI Maya 提供事件循环；batch/standalone 需要主线程事件泵，桥接检测到错误线程会拒绝，不会在线程中强行执行场景操作。桥接仅适合受信任的本地工作站；持有本机进程访问权限的客户端能调用门禁之外的底层桥接，MCP 制作门禁不是操作系统安全沙箱。

4字节大端长度 + UTF-8 JSON，带请求 ID，8 MiB 上限。禁止自动重放；已超时但尚未调度的请求过期拒绝，已开始的渲染无法因客户端超时回滚。运输失败后目标锁定为需重新选择；先检查实际场景状态。

## 工具与范围

见 [maya-workflow](../skills/maya-workflow/SKILL.md)。Arnold 与 RS 有独立原生节点/属性映射，支持创建、查询实际属性、编辑、HDRI、照明连接、单帧渲染。常用灯型：面、点、聚光、方向、环境。布局使用这些灯光工具组合完成；未引入原项目建模/角色/资产服务。

`parameters` 允许原生标量属性，及 `position` / `rotation` / `scale`，`exposure` 按节点区分。RS Dome 的 `intensity` 别名映射 `multiplier`；PhysicalLight 映射 `intensity`。不同渲染器的单位和效果不等价。缺失插件/属性/节点类型明确返回错误，修改失败可能已经产生部分场景改动，应检查后修正。

Arnold 使用 `arnoldRender(batch=True,seq=...)`；RS 在 batch Maya 使用 batch 路径，在 GUI Maya 使用原生 animation 路径且范围严格限制为所请求的一帧。RS 临时追加并保留既有 post-render MEL 回调，用原生完成回执结束作业；Arnold 等待同步 batch 命令返回。原生完成后恢复设置，随后使用 Maya MImage 实际解码图像，外部 MCP 才报告成功并返回路径。支持 EXR/IFF/PNG/TIFF/JPEG/TGA/BMP，不用短暂文件大小稳定推断完成。

等待发生在外部，不阻塞 Maya 收尾，不重发渲染。未收到完成回执时，桥接独立阻止所有场景写入，包含其他客户端。超时或监控异常锁定客户端；显式重新选择同一会话后，用 `get_maya_render_status` 列出作业或指定 job_id 查询，先查清结果再决定后续操作。既有 post-render MEL 报错可能阻断追加回执，此时保持未知结果和禁止写入，需要在 Maya 中人工检查恢复，不能自动宣称成功。

渲染设置预检可写，临时修改后逐项尝试恢复；恢复失败保留原始错误并报告。许可证、GPU、AOV、色彩管理和实际图像仍须检查，文件稳定不等于美术质量认可。

共享 SQLite、规范、反馈、纠错、进度、任务、Dream、模块 MD 与案例归档。经验按 `software=maya` 和功能模块筛选；Maya 工程归档至 `projects/maya`。现有 Houdini/Nuke 记录不迁移或重标。静态 Maya 工程模板解析明确不支持。

## 验证

单元测试：`python -m pytest tests/test_maya_client.py tests/test_maya_knowledge.py`。网络模拟不证明 Maya 或渲染器兼容。

`integrations/maya/verify_runtime.py` 是明确创建独立空场景的 mayapy 验证器；只能在单独测试进程运行。它会显式加载已安装的两个渲染器，测试灯型、修改、HDRI、连接和64x64单帧，将真实结果写入调用者指定目录。不要在用户工程里运行它。

版本分段是入口路由，不能证明所有版本兼容；未来版本需追加路由并验证。py2、py3 初始保留相同行为，后续可独立修复，不强制共享实现；Python 3.14+ 兼容性需实际测试。协议副本对照外部客户端分别测试。上游来源和许可证见 [THIRD_PARTY.md](../integrations/maya/THIRD_PARTY.md)。
