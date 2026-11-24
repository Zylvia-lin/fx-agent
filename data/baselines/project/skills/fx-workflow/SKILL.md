---
name: fx-workflow
description: 使用 FX Agent 操作 Houdini，读取纯文本规范与模块经验、直连外部模板库搜索模板、制作特效并完成网络与视觉验收。
---

# Houdini FX 制作工作流

由当前宿主助手规划和判断，调用 FX Agent 提供的 Houdini 工具。全程使用中文沟通。

> **架构分工**：`fx-agent/data` 仅沉淀**纯文本通用规范与经验**，不保存 `.hip`、贴图、渲染序列或截图；需要查找现成工程模板时，直接调用 `fx_search_published_templates` 检索外部特效模板库。

## 任务门禁顺序

> 参数名区分：任务管理工具（`fx_begin_task` / `fx_get_task` / `fx_update_task` / `fx_set_task_plan` / `fx_task_question` / `fx_finish_task`）使用 `task_id`；受门禁的读写、验证与渲染工具使用 `fx_task_id`。

1. **只读核对与登记任务（`fx_begin_task`）**
   - 明确目标外观、运动、输入/参考、验收标准与预算；只读核对 `fx_status`、真实会话/工程（多会话不猜目标）与 `fx_get_module_catalog`。
   - 调用 `fx_begin_task(spec)` 登记：`software="houdini"`、`project_id`、`shot_id`、真实 `scene_path`、`endpoint`（`127.0.0.1:实际端口`）、`goal`、`acceptance`、`budget`、`modules` 及 `input_paths`：
     - **正式制作 / 新建系统**：`kind="production"`；
     - **同场景局部调参 / 排查**：`kind="quick_tweak"`（保留端口锁、场景路径锁与 `$HIP`/File Cache 护栏，禁重型构建且上限 15 次编辑；需扩展时用 `fx_update_task` 升级 `production`）。

2. **收集纯文本知识与检索模板（优先 `fx_prepare_task_brief`）**
   - **优先调用 `fx_prepare_task_brief(task_id=...)`**：一次聚合读取 `production_rules`（自动展开 `detail_required`）、`context`、所选模块的纯文本手册 `module_knowledge`、`memory_matches`（含 `reference_hint` 自动补查），以及 Houdini 必需的 `get_workflow_guide("discipline")` 和模块上游指南，并自动记齐门禁凭据。
   - 需要展开全文时携带同一 `fx_task_id` 调用 `fx_get_memory` / `fx_get_user_rule`。
   - 需要复用现成效果工程时，直接调用 `fx_search_published_templates` 检索外部特效模板库。

3. **保存制作方案（`production` 必做：`fx_set_task_plan`）**
   - `quick_tweak` 读完简报后即可局部调整；`production` 须提交 `method`、`applies_when`、`changes`、实际读过的 `selected_sources`（`memory:` IDs）及对应的 `source_adaptations`（无适用记录写清真实 `no_match_reason`）。
   - `missing` 非空、存在未解问题或未放行时，只能继续只读调查或登记问题，禁止修改工程、缓存、渲染和保存。

4. **受门禁制作与变更维护**
   - 放行后携带同一 `fx_task_id` 执行节点修改、参数、缓存、渲染与保存；批量操作复用同一任务的有效证据。
   - 优先专用工具与原生节点，不用任意 Python、shell 或其他 MCP 绕过门禁。
   - 需求、输入、工程路径、端口或 FPS 变化时，先 `fx_update_task`（带当前 `revision`），补读受影响证据并更新方案；另存新工程先更新 `scene_path` 再保存。
   - 查证后仍不理解时，调用 `fx_task_question` 登记具体问题并在对话中向用户提问，仅用真实答复解除。

5. **验证、视觉审片与完成（`fx_finish_task`，免工程归档）**
   - 修改后携带 `fx_task_id` 执行 `verify_network`；保存真实 HIP；实际查看多时间点截图或 Render View 预览，**并按配套 `fx-visual-critique`（及其 `references/houdini_fx_critique.md`）做结构化审片**（大中小尺度、剪影、密度梯度与动态节奏）。
   - **无需归档工程或预览到 `data/`**；直接调用 `fx_finish_task` 提交当前 `revision` 和真实 `verification_note`（须包含审片观察结论，`case_id` 留空）。`fx_save_checkpoint` 仅用于记录阶段进度，不替代保存。

6. **纯文本经验回写（配套 `fx-learning`）**
   - 用户提出长期要求 → 及时 `fx_record_user_rule`；
   - 排错解决或提炼出跨项目通用工艺 → 调用 `fx_submit_experience` 写入脱敏后的纯文本经验（无盘符路径、无单镜头死参数、不存工程）；
   - 用户明确认可且已完成 `fx_finish_task` 后，如提炼出新的通用方法，再携带同一 `fx_task_id` 提交 `accepted` 反馈与 `fx_finalize_learning`，自动更新 `data/modules` 纯文本手册。

## Houdini 制作与路径规范

- **路径与缓存**：所有外部文件读取使用 `$HIP/...`；外部素材先复制到当前工程目录再引用。File Cache 统一 `$HIP/geo/$OS/$OS.$F4.bgeo.sc`（Explicit/file 模式，不改派生 `sopoutput`）。预检路径可用 `fx_check_production_path`。
- **版本号与命名**：保留用户原有主版本号，助手修改另存追加两位迭代序号（如 `v02_01.hip`）；一般保留默认节点名，主要对 File Cache、渲染输出等使用 `$OS` 的必要节点按用途命名。
- **造型可编辑**：核心造型与渐变参数暴露为命名参数与可编辑 Ramp/曲线，禁止硬编码定型；优先 VEX 精简网络。
- **Render View 交互预览**：使用受门禁的 `fx_render_view_start` / `status` / `save` / `stop`（携带 `fx_task_id`），并实际查看保存出的图像文件。
- **尺度与迭代原则**：先低成本验证大轮廓、运动、连续性与主次，再加细节与采样；用户要求停止时立即停止。

## 阶段汇报要求

每次阶段汇报列出：`task_id`/`revision`、已读规范/模块/来源 ID、方案放行状态、工程保存与网络/视觉验证证据、纯文本规范/经验写入结果及未完成项；未执行项如实写“未执行/待处理”及原因。
