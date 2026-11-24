---
name: maya-workflow
description: 使用 fx-maya 在指定 Maya 会话中完成 Arnold/Redshift 灯光与渲染任务，读取纯文本规范与灯光经验，并沉淀纯文本通用经验。
---

# Maya 灯光与渲染

这是独立的 Maya 工作流入口，直接调用 `fx-maya` 的制作、纯文本知识与任务工具。全程使用中文沟通，与 Houdini/Nuke 共用 `FX_AGENT_DATA_ROOT`，不保存 `.ma`/`.mb`、贴图或渲染图到 `data/`。

## 架构与启动说明

- **统一模块与桥接**：Maya 端通过共享 `Documents/maya/modules/fx_maya.mod` 加载 `integrations/maya/shared` 下的统一 Python 2/3 兼容桥接与插件（延迟加载不改写已有用户脚本）。
- **GUI 菜单**：Maya 内可通过 **FX Maya MCP** 菜单查看状态、启停当前窗口桥接；重启桥接可能更换端口，须重新发现并显式选择。
- **按需启动新会话**：无可用会话且需要新开 Maya 时，调用 `start_maya(executable="已核实的 maya.exe 绝对路径")`（不附加 `-command`/`-script` 等启动参数，由共享模块自动加载）。等待就绪后重新 `list_maya_sessions` 并用完整身份字段 `connect_maya`。

## 任务门禁与执行步骤

> 参数名区分：任务管理工具使用 `task_id`；受门禁的读取、修改、校验、保存、渲染与 Python 工具使用 `fx_task_id`。

1. **列出会话与核对场景（只读）**
   - 调用 `list_maya_sessions` 展示版本、PID、`session_id`、端口、场景与插件状态；无法唯一确定时向用户确认。
   - 用完整身份字段调用 `connect_maya`，再读取 `get_maya_scene_info` 与 `get_maya_renderer_status`。渲染器插件未加载时明确报告并请用户启用，不静默加载。
2. **登记任务与收集纯文本证据**
   - 调用 `fx_begin_task` 登记 `software="maya"`、真实 `endpoint`、`scene_path`、`modules` 与 `input_paths`（正式布光渲染用 `kind="production"`；单灯微调用 `kind="quick_tweak"`）。由助手从 `fx_get_module_catalog` 按制作内容选择，例如建模 `modeling`、动画 `animation`、绑定 `rigging`、灯光 `lighting`。总结时按每条方法写入相应手册。
   - **优先调用 `fx_prepare_task_brief(task_id=...)`** 一次读取 `production_rules(software="maya")`、`context`、纯文本模块手册与记忆，自动记齐门禁凭据；需要展开全文时再带 `fx_task_id` 调用 `fx_get_memory` / `fx_get_user_rule`。
   - `production` 调用 `fx_set_task_plan` 保存 `method`、`applies_when`、`changes`、`selected_sources`（`memory:` IDs）及 `source_adaptations`（无适用记录写清 `no_match_reason`）；`quick_tweak` 读完简报后即可微调。
3. **场景加载、灯光与渲染操作（全部携带 `fx_task_id`）**
   - **打开工程**：优先使用 `load_scene` 切换工程；切换后先 `fx_update_task` 更新 `scene_path` 并刷新方案。
   - **灯光检查与编辑**：`list_maya_lights` / `inspect_maya_light` 获取真实节点类型与可读属性（使用完整 DAG 路径）；`create_maya_light` / `update_maya_light` 保留各渲染器原生参数单位。
   - **HDRI 绑定**：`set_maya_hdri` 仅接受已存在的绝对路径，先核对色彩空间名称。
   - **照明连接**：`set_maya_light_link` 修改后核对返回的实际连接并渲染确认。
   - **单帧渲染与视觉审片**：`render_maya_frame` 输出单帧，仅认可返回的非空图像文件，并**按配套 `fx-visual-critique`（及其 `references/maya_lighting_critique.md`）做灯光审片**（黑白灰层级、Key/Fill/Rim 光比、冷暖色温对比、接触阴影与材质高光粗糙度）。
   - **断连与超时**：超时或断连后禁止自动重试；先检查现场，重新连接后用 `get_maya_render_status` 查询 `job_id`。
   - **Python 兜底**：`execute_maya_python` 仅作最后手段，不可绕过门禁。
4. **保存、校验与纯文本学习（免工程归档）**
   - `save_scene` 保存当前工程；`verify_network` 检查 unknown 节点。
   - 阶段进度用 `fx_save_checkpoint(..., software="maya")`；续作先核对真实场景，条件变化先 `fx_update_task`。
   - **无需归档 `.ma`/`.mb` 或渲染图到 `data/`**；直接调用 `fx_finish_task` 提交当前 `revision` 与包含审片结论的真实 `verification_note`（`case_id` 留空）。
   - 若提炼出跨项目通用的灯光/材质/渲染经验，按 `fx-learning` 以脱敏纯文本方式调用 `fx_submit_experience` 或在用户认可后执行 `accepted` 反馈与 `fx_finalize_learning`。

## 当前能力边界

- 当前覆盖灯光与单帧渲染；Render View 窗口控制尚未实现。
- 未验证的插件/版本、许可证错误与缺失属性必须如实报告。
