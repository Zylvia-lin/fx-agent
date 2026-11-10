---
name: maya-workflow
description: 使用 fx-maya 在指定 Maya 会话中完成 Arnold/Redshift 灯光与渲染任务。适用于布光、HDRI、照明连接、单帧输出和 Maya 制作续作。
---

# Maya 灯光与渲染

这是独立的 Maya 工作流入口，直接调用 `fx-maya` 的制作、知识和任务工具。全程使用中文沟通，按用户已提供的目标推进。

可以显式使用 `$maya-workflow`，例如：“使用 $maya-workflow，在指定的 Maya 2020 工程中完成 Redshift 三点布光，先做低分辨率单帧检查。”多窗口时先列出会话，依据用户已明确的目标选择；无法唯一确定时询问目标窗口。

使用 `fx-maya`，不使用 Houdini/Nuke 端口发送 Maya 命令。Maya 端统一安装到共享 `Documents/maya/modules/fx_maya.mod`，不向各版本首选项安装。参照用户 L_Tools.mod，以 MAYAVERSION/PLATFORM 分段路由：2017–2020 到 py2，2022–2027 到 py3；每个运行时有独立 scripts/userSetup.py、插件和桥接实现。启动脚本延迟加载插件、启动服务和菜单，已有用户脚本不改写。路由配置不等于全部版本兼容性已验证；未来 Maya 版本需扩展路由并测试，Python 3.14+ 不承诺未经实测的兼容性。

没有可连接会话且需要新开 Maya 时，使用 `start_maya(executable="已核实的 maya.exe 绝对路径")`。当前小屋制作选用 Maya 2020；启动不附加 -command、-script、批处理等参数，不注入插件加载代码，也不操作电脑界面。插件由共享模块自带的启动脚本加载，不依赖某版本/语言的 pluginPrefs.mel。用户反馈额外参数启动时出现 Autodesk Error Report，但尚未证明参数是原因。

启动返回 PID 仅证明已创建进程，不代表桥接就绪。等待启动后重新 `list_maya_sessions`，再复制真实身份字段 `connect_maya`；不能凭 PID 自动选择。连接失败先查启动错误，不反复新开 Maya。用户仅要求修改启动功能时，只修改与测试代码，不实际启动或操作软件。

GUI 中可通过 **FX Maya MCP** 菜单查看状态、启动或停止当前窗口的桥接。停止后重新启动可能更换端口，重新发现并显式选择。连接工具不可用时报告缺少 `fx-maya` 服务；不以其他软件的执行工具代替。

1. 调用 `list_maya_sessions`，展示版本、PID、session_id、端口、场景和插件状态。由用户目标或已明确的工程匹配会话；不随意选第一个。使用列表里的全部身份字段调用 `connect_maya`，再读 `get_maya_scene_info`。
2. 共用 `FX_AGENT_DATA_ROOT`。任务、进度、经验、纠错均指定 `software="maya"`，模块选 `lighting` / `rendering` / `materials` / `io` / `troubleshooting` / `general`。不重标已有软件记录。
3. 修改前明确目标、参考与素材、项目/镜头、验收要求和计算预算，再用 `fx_begin_task` 登记 `software="maya"`、实际 endpoint、scene_path、modules 和 input_paths。携带同一 `fx_task_id` 调用 `fx_get_production_rules`、`fx_get_module_knowledge`、`fx_search_effects`、`fx_search_memory`；软件筛选显式传 `software="maya"`。生产规则带 project_id，从 offset=0 按 next_offset 连续读取，detail_required 条目用 `fx_get_user_rule` 展开。读取匹配经验/效果全文后，用 `fx_set_task_plan` 登记方法、适用条件、改动和实际读取的 selected_sources；无适用记录时保存真实 no_match_reason。后续修改、Python、保存和渲染也传入该 `fx_task_id`。
4. 读 `get_maya_renderer_status`。插件未加载时明确报告，让用户在现有安装中启用；不自动安装、替换或静默加载渲染器。任意 Python 只作最后手段，不能代替专用工具的 RS 验证。
5. `list_maya_lights` / `inspect_maya_light` 获取真实节点类型及可读属性。用完整 DAG 路径消除重名。`create_maya_light` 支持 area/point/spot/directional/environment。`update_maya_light` 支持位置、旋转、缩放和原生属性；尺寸以实际插件属性或缩放表达。参数单位随渲染器保留，不假设同名属性等效。
6. HDRI 使用已存在的绝对路径。Arnold SkyDome 使用 file.outColor 到 color；已连接则先调查，不强制覆盖。RS Dome 使用运行时确认存在的 tex0、exposure0、multiplier，PhysicalLight 使用实际 intensity/exposure。明确核对色彩空间。
7. `set_maya_light_link` 修改/解除照明连接，读取返回的实际连接并渲染检查。`render_maya_frame` 在新目录渲染一帧，只认可返回的实际非空图像文件；查看图像确认构图、光比、噪点。文件存在不代表视觉验收。
8. 超时或断连后操作可能已经完成，不自动重试。先在 Maya 中检查，刷新会话并显式重新选择；渲染可用 `get_maya_render_status` 列出已有作业并按 job_id 检查。原生完成回执到达之前，所有客户端的场景写入被桥接阻止。切换场景也须重新选择和更新任务。
9. `save_scene` 保存当前命名工程；未命名工程可首次保存到任务指定且尚不存在的路径。`verify_network` 仅检查 unknown 节点，不代替渲染和视觉检查。用共享 `fx_archive_case` 归档 .ma/.mb 与预览，`fx_finish_task` 完成验证；获得真实用户接受后再反馈和提炼成功经验。

续作时用 `fx_get_context` / `fx_get_checkpoint` 读取 `software="maya"` 的进度，再核对真实场景。目标、输入或验收变化时用 `fx_update_task` 更新完整 spec 和当前 revision，重新取得相关证据与计划。阶段进度用 `fx_save_checkpoint` 保存。纠错使用 `fx_record_feedback`，保留用户原话、实际改动和结果；用户接受且制作任务已验证后，才使用 `fx_finalize_learning` 提炼成功经验。

当前范围是灯光与渲染。Render View 窗口控制和静态 Maya 工程模板解析尚未实现；不能将 .ma/.mb 送入 Nuke 解析器。未验证的插件/版本、许可证错误、缺失属性必须如实报告。测试场景、模拟网络测试和真实渲染证据分开记录。
