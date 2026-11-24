---
name: fx-learning
description: 当用户在 FX 制作对话中提出长期规范、纠正结果、排错解决或确认有效方法时，以纯文本形式提炼跨项目可复用的通用经验并自动同步到模块手册。不保存工程、贴图或截图等项目文件。
---

# 纯文本通用经验沉淀指南

用户不需要额外说“记住”。当前宿主助手在看到明确的长期要求、排错解决、纠错或最终确认时，直接按本流程提纯**纯文本通用经验**。

> **核心边界**：**资产归外部特效模板库（FX Template），经验归 FX Agent。**
> 绝不把 `.hip` / `.nk` / `.ma`、贴图、渲染序列、预览视频、截图或客户绝对路径写入 `data/`；只沉淀跨项目可迁移的规范、工艺配方、节点骨架与排错避坑。

## 入库质量红线（脱敏与高信噪比）

写入任何经验前必须完成脱敏与精炼：
1. **禁止写入项目私货**：去除客户项目名、镜头号、本机盘符绝对路径（`C:/...`、`F:/...`、`//Desktop-...`）与具体工程文件名；统一使用 `$HIP/...` 或通用节点/角色描述。
2. **禁止固化单镜头魔数**：记录调节原理、组合平衡思路与验证方法，不把单个镜头的绝对增益/位移/出生率写死为通用预设。
3. **单条精炼**：正文控制在 150–600 字，写清 `applies_when`（适用与不适用边界）、`method`（核心步骤/节点骨架/表达式）和 `reason`（底层原理与注意项）。
4. **不凑数入库**：若本轮只是普通镜头交付、未产生新的跨项目通用经验，无需强行写入；若成品工程本身值得原样复用，提醒用户直接发布到外部 FX Template 模板库。

## 1. 用户提出长期规范（Rule）

1. 区分长期规范与仅当前镜头的临时调整；只有明确要求才入规范库。
2. 先读 `fx_get_production_rules`（有 `omitted_rules` 按 `next_offset` 翻页，`detail_required` 用 `fx_get_user_rule` 展开）。
3. 调用 `fx_record_user_rule` 保存精炼规则、真实 `user_quote`、来源、作用域（默认 `global`）及 `expected_revision`。
4. 不覆盖更晚要求，不把模型建议伪装成用户原话。

## 2. 排错解决或现场验证出通用工艺（Recipe / Pitfall）

当在当前 DCC 会话中定位并解决了一个通用问题，或验证了一套可迁移的制作思路时：
1. 调用 `fx_submit_experience`（默认 `project_dependent=False`、`project_id="__global__"`、`case_id=""`、`reuse_scope="global"`）：
   - 明确填写 `software`（`houdini` / `nuke` / `maya`）与 `modules`（从 `fx_get_module_catalog` 选择 1–4 个，不用 `unclassified`）。
   - 选对 `kind`：`workflow`（通用工艺配方）、`troubleshooting`（排错避坑）、`compositing` / `lighting` / `render`（视觉平衡与管线策略）、`preference`（操作偏好）。
   - 如实标注 `source_type`：用户明确提出用 `user`，现场实测验证用 `observed`（直接进入 `active` 并自动刷新 `data/modules/global/<software>/<module>.md`），未经实测的推测用 `inferred`（保持 `candidate`，不直接污染正式召回）。
2. 检查返回的 `module_guides.state`，如出现 `pending_rebuild` 须报告并调用 `fx_refresh_module_guides` 修复。

## 3. 制作过程中的纠错轨迹与最终确认

1. **过程纠错**：调用 `fx_record_feedback` 记录 `effect_type`、`attempt_id`、`software`、`modules`、问题、实际调整、原因、结果与用户原话；未确认保持 `pending` / `improved` / `failed`。
2. **用户明确认可**：
   - 确认当前任务已通过 `fx_finish_task`（纯文本模式无需归档工程，直接凭保存、结构检查与真实画面观察完成验收）。
   - 携带同一 `fx_task_id` 将反馈更新为 `outcome="accepted"`（`case_id` 留空，不保存工程或截图）。
   - 调用 `fx_finalize_learning` 提纯跨项目通用的 `method`、`reason`、`applies_when`，自动写入全局经验并刷新对应纯文本模块手册。
3. 修改旧反馈会自动停用由旧版本派生的经验，防止重复旧错。

## 4. 再次制作时召回知识与检索模板

- **纯文本经验召回**：开工优先调用 `fx_prepare_task_brief(task_id=...)` 一次读取 `production_rules`、纯文本模块手册 `module_knowledge` 与 `memory_matches`；遇 `reference_hint.has_matches=true` 时按同条件以 `include_candidates=true` 查看候选参考。
- **现成工程模板检索**：需要复用现成效果工程、HDA 或节点资产时，直接调用 `fx_search_published_templates` 搜索外部 FX Template 特效模板库，不在 `fx-agent/data` 维护重型效果卡。

## 5. 阶段整理纯文本记忆（Dream）

有新经验积累或发现重复/冲突条目时执行纯文本整理：
1. `fx_dream_memory(mode="preview", software=..., module=..., cursor=...)` 分页预览；用 `fx_get_memory` 核对全文与冲突。
2. 提交 `merge` / `refine` / `split` / `conflict` / `supersede` 动作（带当前 `snapshot` 与 `mode="apply"`），保持每条经验精炼无冗余、无盘符路径残留。
3. 确认返回的 `module_guides.state` 同步完成；必要时可用 `fx_undo_dream(run_id)` 回滚。
