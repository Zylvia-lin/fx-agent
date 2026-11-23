---
name: maya-workflow
description: 使用 fx-maya 在指定 Maya 会话中完成 Arnold/Redshift 灯光与渲染任务。适用于布光、HDRI、照明连接、单帧输出和 Maya 制作续作。
---

# Maya 灯光与渲染

这是独立的 Maya 工作流入口，直接调用 `fx-maya` 的制作、知识和任务工具。全程使用中文沟通，与 Houdini/Nuke 共用 `FX_AGENT_DATA_ROOT`，但不使用 Houdini/Nuke 端口发送 Maya 命令。

## 架构与启动说明

- **统一模块与桥接**：Maya 端通过共享 `Documents/maya/modules/fx_maya.mod` 加载 `integrations/maya/shared` 下的统一 Python 2/3 兼容桥接与插件（覆盖 Maya 2017–2027 路由，延迟加载不改写已有用户脚本；未实测版本或 Python 3.14+ 不承诺兼容性）。
- **GUI 菜单**：Maya 内可通过 **FX Maya MCP** 菜单查看状态、启停当前窗口桥接；重启桥接可能更换端口，须重新发现并显式选择。
- **按需启动新会话**：无可用会话且需要新开 Maya 时，调用 `start_maya(executable="已核实的 maya.exe 绝对路径")`（不附加 `-command`/`-script` 等启动参数，由共享模块 `userSetup.py` 自动加载）。返回 PID 仅表示进程已创建，等待就绪后重新 `list_maya_sessions` 并用完整身份字段 `connect_maya`，不能凭 PID 猜选或反复新开。

## 任务门禁与执行步骤

> 参数名区分：任务管理工具使用 `task_id`；受门禁的读取、修改、校验、保存、渲染与 Python 工具使用 `fx_task_id`。

1. **列出会话与核对场景（只读）**
   - 调用 `list_maya_sessions` 展示版本、PID、`session_id`、端口、场景与插件状态；依据用户目标匹配会话，无法唯一确定时向用户确认。
   - 用列表里的全部身份字段调用 `connect_maya`，再读取 `get_maya_scene_info` 与 `get_maya_renderer_status`。渲染器插件未加载时明确报告并请用户启用，不自动安装或静默加载。
2. **登记任务与收集证据**
   - 明确目标、参考与素材、项目/镜头、验收要求和预算后，调用 `fx_begin_task` 登记 `software="maya"`、真实 `endpoint`、`scene_path`、`modules`（从 `lighting` / `rendering` / `materials` / `io` / `troubleshooting` / `general` 选择）与 `input_paths`。
   - 携带同一 `fx_task_id` 读取 `fx_get_production_rules(project_id=..., software="maya", offset=0)`（翻页并展开 `detail_required`）、`fx_get_module_knowledge(..., software="maya")`、`fx_search_effects(software="maya", ...)`、`fx_search_memory(software="maya", ...)`，并完整展开所选来源。
   - 调用 `fx_set_task_plan` 保存 `method`、`applies_when`、`changes`、`selected_sources` 及逐来源 `source_adaptations`（无适用记录写清 `no_match_reason`）。
3. **场景加载、灯光与渲染操作（全部携带 `fx_task_id`）**
   - **打开工程**：优先使用专用 `load_scene` 工具切换工程，不靠任意 Python 打开文件；切换工程后须先 `fx_update_task` 更新 `scene_path` 并刷新方案。
   - **灯光检查与编辑**：`list_maya_lights` / `inspect_maya_light` 获取真实节点类型与可读属性（使用完整 DAG 路径消除重名）；`create_maya_light` 支持 `area`/`point`/`spot`/`directional`/`environment`；`update_maya_light` 调整位移/旋转/缩放与原生属性，保留各渲染器原生参数单位。
   - **HDRI 绑定**：`set_maya_hdri` 仅接受已存在的绝对路径。Arnold SkyDome 连接 `file.outColor` 到 `color`（已有连接先调查，不强制覆盖）；Redshift Dome 使用运行时确认存在的 `tex0`/`exposure0`/`multiplier`，并核对色彩空间。
   - **照明连接**：`set_maya_light_link` 修改或解除照明连接后，核对返回的实际连接并渲染确认。
   - **单帧渲染**：`render_maya_frame` 在新目录输出单帧，仅认可返回的非空图像文件并实际查看构图、光比与噪点；原生完成回执到达前桥接会阻止新的场景写入。
   - **断连与超时**：超时或断连后操作可能已执行，禁止自动重试；先在 Maya 检查，刷新 `list_maya_sessions` 重新连接，渲染用 `get_maya_render_status` 按 `job_id` 查询。
   - **Python 兜底**：`execute_maya_python` 仅作最后手段，不可用来绕过门禁或替代专用工具的 Redshift/Arnold 检查。
4. **保存、校验、归档与学习**
   - `save_scene` 保存当前命名工程（未命名工程可首次保存到任务指定且尚不存在的路径）。
   - `verify_network` 仅检查 unknown 节点，不替代渲染与视觉检查。
   - 阶段进度用 `fx_save_checkpoint(..., software="maya")`；续作先读 `fx_get_context`/`fx_get_checkpoint` 并核对真实场景，条件变化时先 `fx_update_task`。
   - 完成后用 `fx_archive_case` 归档 `.ma`/`.mb` 与渲染预览，调用 `fx_finish_task` 提交当前 `revision`、`case_id` 与真实 `verification_note`。获得用户真实认可后再按 `fx-learning` 提交 `accepted` 反馈与 `fx_finalize_learning`。

## 当前能力边界

- 当前覆盖灯光与单帧渲染；Render View 窗口控制与静态 `.ma`/`.mb` 工程模板解析尚未实现（不可把 `.ma`/`.mb` 送入 Nuke 解析器）。
- 未验证的插件/版本、许可证错误与缺失属性必须如实报告；测试场景、模拟测试与真实渲染证据分开记录。
