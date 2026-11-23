---
name: fx-learning
description: 当用户在 FX 制作对话中提出长期规范、纠正结果、要求修改、确认最终效果，或希望复用同类效果时，提炼规范、记录调整轨迹、归档成功方案并整理记忆。由当前宿主助手判断和调用 FX Agent 工具。
---

# 从对话与制作反馈持续学习

用户不需要额外说“记住”。当前宿主助手在看到明确的长期要求、纠错或最终确认时，直接按对应流程调用工具。服务不自动读取聊天记录，也不另起独立模型代理。

## 任务门禁与提问约束

- 制作与学习共用 `fx_begin_task` 登记的同一任务 ID（任务管理工具传 `task_id`，受门禁工具传 `fx_task_id`）。
- 仅当对应制作任务已通过 `fx_finish_task`、归档匹配且用户明确认可后，才提交 `accepted` 反馈与 `fx_finalize_learning`；普通 `pending`/`improved`/`failed` 反馈与用户规则记录不要求任务先完成。
- 查证后仍有疑问时，用 `fx_task_question` 登记具体问题并在对话中集中提问（工具返回 `ask_user` 只是提醒，不代表已发问）。收到真实回复后再保存 `answer`、`user_quote` 与 `source_ref`；未解问题阻止任务完成与成功入库。

## 1. 用户提出长期要求

1. 区分长期规范、仅当前镜头调整与假设；只有明确要求才入规范库。
2. 先读 `fx_get_production_rules`（有 `omitted_rules` 按 `next_offset` 翻页，`detail_required` 用 `fx_get_user_rule` 展开）。
3. 调用 `fx_record_user_rule` 保存精炼规则、真实用户原话、来源、作用域（通用选 `global`，项目专属选 `project` + `project_id`）及当前修订号。
4. 更新冲突先重读，不覆盖更晚要求，不把模型建议伪装成用户原话。若要求改变机器校验行为，须同步修改代码并通过回归测试。

## 2. 用户提出纠错或修改

1. 记录用户指出的现象，先查真实工程与参数，不猜测已做改动。
2. 调用 `fx_record_feedback` 记录 `effect_type`、唯一 `attempt_id`、`software`、`modules`（最多 4 个，按 `fx_get_module_catalog` 选择，新记录不用 `unclassified`）、问题、实际调整、原因、结果、用户原话与来源。
3. 结果状态如实标注：未执行/未验证标 `pending`，有改善未最终确认标 `improved`，失败标 `failed`；用户未反对不等于 `accepted`。
4. 同一尝试更新传 `expected_revision`；修改旧反馈会停用由旧版本派生的方案，避免重复旧错。
5. 查看轨迹用 `fx_get_learning_history`（按 `next_cursor` 翻页），需精确原文与参数时用 `fx_get_feedback` 展开。

## 3. 用户确认最终效果

1. 确认用户明确认可当前版本并保留原话，不把“渲染成功”当成用户认可。
2. 用 `fx_archive_case` 归档最终工程、必要依赖、相关图片与结果预览，获取真实 `case_id`。
3. 将本次最终反馈更新为 `accepted`，区分失败尝试与实际生效方法。
4. 调用 `fx_finalize_learning` 提炼可复用步骤、关键参数、原因、适用条件与失败注意项：
   - 全局可复用的已接受方案会自动更新 `data/modules` 下对应模块 MD 的自动区块（上游指南不动）。
   - 检查返回的 `module_guides.state`：为 `pending_rebuild` 时说明经验已落库但 MD 未同步，须报告并调用 `fx_refresh_module_guides` 修复。
   - 默认 `reuse_scope="global"`（`project_id` 记来源），仅镜头特例选 `shot`；标明哪些是观察、哪些是待验证解释。

## 4. 再次制作同类效果

- **正式与候选**：正式经验为空不代表无资料；收到 `reference_hint.has_matches=true` 时按同作用域以 `include_candidates=true` 查候选，保持未验证状态。
- **效果卡与模板**：按近义词/构成词调用 `fx_search_effects` 与 `fx_search_templates`（分页查看）；选中总卡须展开组成卡及相关模板节点、连接、参数与动画。模板来源候选用 `fx_learn_template_experience` 保存（默认 `inferred`，不自动转正）。
- **逐来源记录应用**：制作方案中每条 `selected_sources` 必须填写 `source_adaptations`（`source`/`source_method`/`application`/`verification`）；复用与反馈保留 `effect_card_id` 与 `template_study` 来源引用。
- **预算阅读**：先读规范、模块原指南与 `fx_get_module_knowledge`；已在 MD 看到的完整条目不重复读全文，按需 `fx_get_memory` 核对适用条件与案例。

## 5. 阶段结束时整理记忆（Dream）

有新有效经验、完成外部导入或用户要求整理时执行（若需修订/合并效果卡且无已放行任务，先按 `fx-project-learn` 建立学习任务并携带 `fx_task_id`，不借 Dream 绕过门禁）：

1. **预览候选**：`fx_dream_memory(mode="preview", software=..., module=..., cursor=...)` 分页读取全局候选（`fx_get_dream_runs` 查历史进度）。
2. **展开核对**：用 `fx_get_memory` 展开完整来源与适用条件；遇到 `has_conflict` 时展开 `conflict_memory_ids`，未解决前不作为无争议方案套用。
3. **提交动作**：支持 `merge`、`refine`、`split`、`conflict`、`supersede`，每条填齐 `source_ids`、完整 `title`/`method`/`reason`/`applies_when`、`rationale`（最多 20 条/轮）。候选不能借整理升级为已验证，原始文本不删除。
4. **应用与游标**：`apply` 携带当前 `snapshot`；精确去重省略 `actions` 并沿用同页参数；`deferred_actions > 0` 时先继续处理同一页再推进游标。检查 `module_guides.state`，`pending_rebuild` 时调用 `fx_refresh_module_guides`。可用 `fx_undo_dream(run_id)` 撤销未受后续影响的整理。
5. **同步复核效果卡**：检查 `effect_learning` 及关联 `effect_card_ids`（可用 `fx_get_effect_learning_queue` 处理存量）：
   - 有新证据使效果更明确时，用 `fx_refine_effect(card_id, updates, sources, reason)` 补充名称、类别、外观、运动、别名与证据（不抹历史，不同表现建独立 `effect_key`）。
   - 检查同工程内因对象编号/渲染层拆得过细的卡片：分页 `fx_search_effects` 并逐张 `fx_get_effect` 核对，确认属同一效果后再用 `fx_merge_effect_cards(card_ids, card, reason)` 先预览后 `apply=true`（保留各层差异、节点、输入、预览、证据及 `component_modules`，不合并独立变体、不跨工程混合、不提升验证等级）。合并后重新核对复核状态。

## 6. 导入另一设备的数据

1. 先用 `fx_import_data(source=...)` 预览，确认源归档停止写入后再 `apply=true`（相同记录幂等跳过，分叉修订保留）。
2. 用 `fx_get_import_records` 展开冲突双方全文；冲突经验暂停默认召回，结合当前 Dream `snapshot` 调用 `fx_resolve_import_memory` 选择 `keep_local` / `keep_incoming` / `keep_both`。
3. 用户规范分歧通过 `fx_record_user_rule` 版本化保存决定；检查点须核对本机路径后重新保存。
4. 导入与解冲突完成后执行全局 Dream；原始导入材料保留在 `data/imports` 下。

---

软件作用域：Houdini 使用 `software="houdini"`（默认），Nuke 使用 `software="nuke"`，Maya 使用 `software="maya"`。发现上游渲染问题时保存版本化问题单，Houdini 核查后由 Nuke 复查关闭，不自动重渲染。
