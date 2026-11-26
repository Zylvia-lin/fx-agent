---
name: nuke-workflow
description: 由当前宿主助手通过 FX Agent 操作 Nuke 合成，读取纯文本规范与合成经验、完成多帧视觉审片，并沉淀纯文本通用经验与上游渲染问题反馈。
---

# Nuke 合成与纯文本学习

当前宿主助手负责规划、制作与判断；`fx-nuke` 是操作工具，不另起独立决策模型。全程使用中文，不提供任意 Python 执行工具。

> **架构分工**：`fx-agent/data` 仅保存**纯文本通用规范与合成经验**，不保存 `.nk`、EXR 序列或截图归档；复用现成模板时通过 `fx-agent` 调用 `fx_search_published_templates` 检索外部模板库。

## 任务门禁顺序

> 参数名区分：任务管理工具使用 `task_id`；受门禁的读取、修改、校验、保存与渲染工具使用 `fx_task_id`。

1. **只读核对与建任务**：明确目标画面、动态、素材/参考、验收和预算；只读调用 `fx_status`、`list_nuke_sessions` / `get_nuke_connection_status`、`get_script_info`、`fx_get_module_catalog(software="nuke")`，再用 `fx_begin_task` 登记 `software="nuke"`、真实 `scene_path`、`endpoint`、`modules` 与 `input_paths`（正式合成用 `kind="production"`；局部调色/参数微调用 `kind="quick_tweak"`，保留端口与场景锁，上限 15 次编辑，可随时 `fx_update_task` 升级 `production`）。
   - 由助手按操作选择模块。重打光用 `lighting`，AOV 或通道分层用 `layers`，调色用 `color`。总结时只写到该条方法对应的手册。
2. **收集纯文本证据（优先 `fx_prepare_task_brief`）**：
   - 调用 `fx_prepare_task_brief(task_id=...)` 一次聚合读取 `production_rules(software="nuke")`（软件专属规范，切勿把 Houdini `$HIP` 写进 Nuke 旋钮）、`context`、纯文本模块手册与历史合成经验，并记录已返回的门禁凭据；根据 `deferred_reads`、`task_state.missing` 和 `next_offset` 补读。需要展开全文时再携带 `fx_task_id` 调用 `fx_get_memory` / `fx_get_user_rule`。
3. **保存方案（`production` 必做：`fx_set_task_plan`）**：`quick_tweak` 读完简报后即可局部微调；`production` 填写 `method`、`applies_when`、`changes`、实际读过的 `selected_sources`（`memory:` IDs）及逐来源 `source_adaptations`，或真实 `no_match_reason`。
4. **受门禁制作**：节点修改、批量操作、保存与渲染均携带同一 `fx_task_id`。输入、目标 NK、帧段、FPS、格式或色彩管理变化时，先 `fx_update_task` 再重查证据并更新方案。只读调查无需放行。
5. **验收、视觉审片与完成（免工程归档）**：
   - 实际调用 `save_script`（按既有用户授权填写 `confirm`；保留用户主版本号并追加两位迭代序号如 `v02_01.nk`）。
   - 携带 `fx_task_id` 执行 `find_broken_reads`，并**按配套 `fx-visual-critique`（及其 `references/nuke_comp_critique.md`）做多帧合成诊断审片**：检查前中后三帧与短暂峰值帧，用 `set_nuke_viewer_state` 切帧，携带同一 `fx_task_id` 调用 `capture_nuke_viewer`，实际查看并核对默认曝光、黑场、高光滚降、Alpha/RGB 边缘与颗粒，审完复位 Viewer（默认单 A，不改动用户 B 参考）。
   - **无需归档 `.nk` 或预览到 `data/`**；直接在 `fx-nuke` 调用 `fx_finish_task` 提交当前 `revision` 和包含审片结论的真实 `verification_note`（`case_id` 留空）。
   - 仅当用户确认且提炼出新的跨项目通用经验时，按 `fx-learning` 提交纯文本 `accepted` 反馈与 `fx_finalize_learning` 或 `fx_submit_experience`（脱敏去除盘符路径与单镜头数值）。
6. **疑问处理**：查证后仍不明确处用 `fx_task_question` 登记并向用户集中发问，仅用真实答复解除。重启或续作须重新核对现场并更新任务。

## Nuke 连接与专用工具

1. **多会话连接（只读，不需任务放行）**：
   - 调用 `list_nuke_sessions` 获取各实例的 PID、端口、`session_id` 与工程路径，再完整传入 `connect_nuke(port, pid, session_id, expected_script)`。默认基准端口 `8187`，不自动猜选其他窗口。
   - 无自动启动工具；无可用会话时请提示用户启动 Nuke 11.0 或更新版本并打开目标工程后再连接。
   - 桥接更新后可用 `reload_nuke_tools` 热重载 Nuke 端自定义工具模块。
2. **只读核查与节点图工具**：
   - 核查节点与素材：`get_script_info`、`get_node_info`、`find_nodes_by_type`、`find_broken_reads`、`get_selected_nodes`。
   - 节点构建与编辑：`create_node`、`duplicate_nuke_node`、`modify_node`、`connect_nodes`、`delete_node`、`position_node`、`batch_set_knob`、`batch_reconnect`、`import_nuke_toolset`，以及常用构建 `setup_keyer` / `setup_despill` / `setup_basic_comp` / `setup_grade_chain` / `setup_light_wrap` / `setup_edge_blur` / `setup_write_node`。
   - 规范约束：封装节点（如 `L_RealGlow`）只调暴露参数不展开内部；扭曲等优先蓝宝石 Sapphire 节点；保留默认节点名，不逐节点写注释，大 Backdrop 用中文标题；仅整理用户指定的效果块，保持中央垂直总线、背景置顶、左右错层完整效果块 Read 置顶向下汇合。
3. **Viewer 控制与视觉检查**：
   - 使用 `get_nuke_viewer_state`、`set_nuke_viewer_state`、`connect_nuke_viewer`、`fit_nuke_viewer`、`capture_nuke_viewer` 检查画面，不干扰用户桌面鼠标键盘。
4. **进度与学习**：
   - 阶段进度使用 `fx_save_checkpoint(..., software="nuke")`，不替代 `save_script`。
   - 反馈、经验与规则均明确 `software="nuke"` 与对应模块 ID，全部以纯文本脱敏形式沉淀。

## 发现上游渲染素材问题

1. 先排除 Read 配置、代理、色彩空间/预乘等合成端原因；确有证据指向源渲染时调用 `fx_report_render_issue` 记录项目、镜头、素材路径、帧段、预期与实际表现及检查证据。
2. 切回 Houdini 核对真实工程与渲染设置，用 `fx_update_render_issue(actor="houdini")` 标记 `investigating`，修复后标记 `awaiting_nuke_review`。
3. 回到 Nuke 重读并验证新素材：通过后以 `actor="nuke"` 标记 `closed`，仍有问题则改回 `open`。问题单仅记录纯文本核查证据，不在 `data/` 拷贝工程或序列。
