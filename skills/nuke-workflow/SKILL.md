---
name: nuke-workflow
description: 由当前宿主助手通过 FX Agent 操作 Nuke 15.2 合成，参考效果或工程，保存纠错与成功方案，并将渲染素材问题反馈 Houdini 检查。
---

# Nuke 合成与学习

当前宿主助手负责规划、制作与判断；`fx-nuke` 是操作工具，不另起独立决策模型。全程使用中文，不提供任意 Python 执行工具。

## 任务门禁顺序

> 参数名区分：任务管理工具使用 `task_id`；受门禁的读取、修改、校验、保存与渲染工具使用 `fx_task_id`。

1. **只读核对与建任务**：明确目标画面、动态、素材/参考、验收和预算；只读调用 `fx_status`、`list_nuke_sessions` / `get_nuke_connection_status`、`get_script_info`、`fx_get_module_catalog(software="nuke")`，再用 `fx_begin_task` 登记 `kind="production"`、`software="nuke"`、真实 `scene_path`、`endpoint`、`modules` 与 `input_paths`（与 Houdini/Maya 共用 `data_root`）。
2. **收集证据（携带 `fx_task_id`）**：
   - `fx_get_production_rules(project_id=..., software="nuke", offset=0)` 按 `next_offset` 翻页（`detail_required` 用 `fx_get_user_rule` 展开）；软件专属规范已独立返回，切勿把 Houdini `$HIP` 规则写入 Nuke 旋钮。
   - 读取 `fx_get_module_knowledge(..., software="nuke", module=...)`、`fx_search_effects(software="nuke", ...)` 与 `fx_search_memory(software="nuke", ...)`，完整展开选中来源。模块补充 `truncated=true` 时改为按该 `module` 检索并展开选中项，零命中写清 `no_match_reason`。
3. **保存方案（`fx_set_task_plan`）**：填写 `method`、`applies_when`、`changes`、实际读过的 `selected_sources` 及逐来源 `source_adaptations`（效果卡另填 `source_node_paths`），或真实 `no_match_reason`。
4. **受门禁制作**：节点修改、批量操作、保存与渲染均携带同一 `fx_task_id`。输入、目标 NK、帧段、FPS、格式或色彩管理变化时，先 `fx_update_task` 再重查证据并更新方案。只读调查无需放行。
5. **验收与完成**：
   - 实际调用 `save_script`（按既有用户授权填写 `confirm`；仅确认提示不算保存）。
   - 携带 `fx_task_id` 执行 `find_broken_reads` 并实际查看输出或 Viewer 截图。
   - 调用 `fx_archive_case` 归档已保存 NK、预览及必要依赖，在 `fx-nuke` 入口调用 `fx_finish_task`（提交当前 `revision`、`case_id` 和真实 `verification_note`）。
   - 用户确认后再通过共享入口带同一 `fx_task_id` 提交 `accepted` 反馈和 `fx_finalize_learning`。
6. **疑问处理**：查证后仍不明确处用 `fx_task_question` 登记并向用户集中发问，仅用真实答复（`answer`、`user_quote`、`source_ref`）解除。重启或续作须重新核对现场并更新任务。

## Nuke 连接与专用工具

1. **多会话连接（只读，不需任务放行）**：
   - 调用 `list_nuke_sessions` 获取各实例的 PID、端口、session_id 与工程路径，再将目标实例字段完整传入 `connect_nuke(port, pid, session_id, expected_script)`。默认基准端口 `8187`，不自动猜选其他窗口。
   - **无自动启动工具**：`fx-nuke` 不提供自动启动 Nuke 的工具；无可用会话时请提示用户启动 Nuke 15.2 并打开目标工程后再刷新会话连接，切勿用离线猜测替代现场核查。
   - 桥接脚本更新后可用 `reload_nuke_tools` 热重载 Nuke 端自定义工具模块。
2. **只读核查与节点图工具**：
   - 核查节点与素材：`get_script_info`、`get_node_info`、`find_nodes_by_type`、`find_broken_reads`、`get_selected_nodes`；不凭节点名猜测通道、帧段、Alpha/预乘或色彩空间。
   - 节点构建与编辑：`create_node`、`duplicate_nuke_node`、`modify_node`、`connect_nodes`、`delete_node`、`position_node`、`batch_set_knob`、`batch_reconnect`、`import_nuke_toolset`，以及常用快捷构建 `setup_keyer` / `setup_despill` / `setup_basic_comp` / `setup_grade_chain` / `setup_light_wrap` / `setup_edge_blur` / `setup_write_node`。
   - 图面整理与界面截图：`collapse_nuke_dots`、`layout_nuke_blocks`、`layout_nuke_nodes`、`frame_nuke_graph`、`capture_nuke_interface`（仅在明确需要时整理指定区块，不擅自打乱用户原有布局）。
3. **Viewer 控制与视觉检查**：
   - 使用 `get_nuke_viewer_state`、`set_nuke_viewer_state`、`connect_nuke_viewer`、`fit_nuke_viewer`、`capture_nuke_viewer` 检查不同节点、通道、帧号与曝光下的真实画面。
   - 保存、替换工程、删除和渲染工具的 `confirm` 按既有用户授权如实填写，不视作追加授权。
4. **进度与学习**：
   - 阶段进度使用 `fx_save_checkpoint(..., software="nuke")`，不替代 `save_script`。
   - 纠错 `fx_record_feedback`、经验 `fx_submit_experience` 与终结提炼 `fx_finalize_learning` 均明确 `software="nuke"` 与 `fx_get_module_catalog(software="nuke")` 中的模块 ID；软件专属用户规则使用 `nuke-` 前缀并在 `statement` 标明适用软件。检查 `module_guides.state`，遇 `pending_rebuild` 调用 `fx_refresh_module_guides`。

## 发现上游渲染素材问题

1. 先排除 Read 配置、代理、色彩空间/预乘等合成端原因；确有证据指向源渲染时调用 `fx_report_render_issue` 记录项目、镜头、素材路径、帧段、预期与实际表现及检查证据，不臆断 Houdini 根因。
2. 切回 Houdini 工作流核对真实工程与渲染设置，用 `fx_update_render_issue(actor="houdini")` 标记 `investigating`，修复并给出新素材版本与证据后标记 `awaiting_nuke_review`（问题单不会自动触发 Houdini 修改或重渲染）。
3. 回到 Nuke 重读并验证新素材：通过后以 `actor="nuke"` 标记 `closed`，仍有问题则改回 `open`（每次带当前 `expected_revision`）。截图等长期证据先 `fx_archive_case` 再引用。
