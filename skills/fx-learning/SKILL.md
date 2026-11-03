---
name: fx-learning
description: 当用户在 FX 制作对话中提出长期规范、纠正结果、要求修改、确认最终效果，或希望复用同类效果时，提炼规范、记录调整轨迹、归档成功方案并整理记忆。由 当前宿主助手（如 Codex） 判断和调用 FX Agent 工具。
---

# 从对话与制作反馈持续学习

用户不需要额外说“记住”。当前宿主助手（如 Codex） 在看到有价值的明确要求、纠错和最终确认时，执行下面的对应流程。服务不自行读取所有 当前宿主助手（如 Codex） 聊天，不另起一个模型代理。

## 用户提出长期要求

1. 区分长期规范、仅当前镜头的调整和假设；只有明确要求进入规范库。
2. 读取 `fx_get_production_rules`；有 `omitted_rules` 时通过 `next_offset` 翻页，`detail_required` 的规则用 `fx_get_user_rule` 展开后再行动。
3. 调用 `fx_record_user_rule`，保存精炼规则、真实用户原话、来源、适用范围与当前修订号。通用要求明确选择 global；仅项目适用时选择 project 并填写项目 ID。
4. 规则更新使用当前修订号，冲突先重读，不覆盖更晚要求。不可把模型建议伪装成用户原话。
5. 用户文本规范立即持久化并纳入下一轮制作。若要求改变机器检查行为，当前宿主助手（如 Codex） 还需同步修改对应校验、完成回归后提交；保存一段文字本身不会改变程序。

## 用户提出纠错或修改

1. 保存用户指出的问题表现，查询真实工程与参数，不猜测已执行了什么。
2. 用 `fx_record_feedback` 记录 effect_type、唯一 attempt_id、问题、实际调整、原因、结果、用户原话与来源。
   同时明确 modules（最多4个），如 sop、particles、pyro、flip、rbd；不确定时读 `fx_get_module_catalog`，不能用 unclassified 代替新记录的认真分类。旧记录未归类则保持未分类，除非有事实支持更正。
3. 未执行或未验证的结果标 pending；有所改善但未最终确认标 improved；失败标 failed。用户没有反对不等于 accepted。
4. 同一尝试更新用 expected_revision。新的观察不覆盖历史，修改旧反馈会停用由其旧版本派生的方案，避免下次重复旧错。
5. 阅读纠错轨迹用 `fx_get_learning_history`，游标按 `next_cursor` 翻页；需要精确参数、原话或完整结果时用 `fx_get_feedback`，不能用截短卡片代替原文提炼。

## 用户确认最终效果

1. 明确用户确实认可当前版本，保存原话；不能用“渲染成功”替代认可。
2. 归档最终 HIP、必要依赖、相关图片及结果预览，用实际 case_id 关联。只有工程文件或只有参考图不够，尚未复制的内容不能声称已归档。
3. 将本次最终反馈更新为 accepted；从完整轨迹中区分失败尝试与实际采用的有效方案。
4. 调用 `fx_finalize_learning`，提炼可复用步骤、关键参数、原因、适用条件与失败注意项；由服务检查当前反馈、范围与归档，并关联来源。
   反馈中的 modules 会继承到最终经验。全局可复用的已接受方案会自动更新 data/modules 下对应模块 MD 的自动区块；上游指南不修改。它写入提炼方法、条件与证据 ID，不复制整段聊天。未分类、镜头限定、未最终提炼的记录不推广成项目模块准则。
   检查返回 module_guides.state：pending_rebuild 表示经验已经保存、MD 未同步，需要说明原因并调用 `fx_refresh_module_guides(project_id)` 修复；不能当作整次事务回滚重写历史。撤销或纠正已提炼方案也会更新文档。
5. 默认 reuse_scope=global，跨项目检索；project_id 记录来源。仅工程特例选择 shot。旧 reuse_scope=project 兼容为 global。提炼时保留版本、尺度、输入等适用条件，不机械推广具体参数。
6. 用户接受画面不代表每条因果解释都已证明；标明哪些是观察、哪些是待验证解释。归档文件校验也不等于完整可复现。

## 再次制作同类效果

有工程模板资料时并行检索 fx_search_templates，按 study_id 展开节点与原始参数。模板来源候选用 fx_learn_template_experience 保存；其默认是推断，不能因有工程归档就改为已验证观察。实际使用后的纠错和最终方案继续走本技能现有反馈与提炼流程，source_ref 保留 template_study 与节点证据。

用户以自然语言描述目标效果时先 fx_search_effects，展开效果指示卡并比较同方案的不同变体。复用与反馈保留 effect_card_id；同一制作方案只因参数、动态或渲染不同而形成的效果卡必须分别保留。Dream可以整理共用方法与经验，但不能把不同效果卡删除、覆盖或按节点相同强行合并。

先读当前规范、对应模块的上游指南与 `fx_get_module_knowledge` 返回的全局模块补充。按需以效果类型和关键词调用 `fx_search_memory(module=...)`；已在 MD 看到的完整条目不重复全文读取。选中后需要更多细节才 `fx_get_memory`，再核对归档案例。比较尺度、时长、渲染器和输入条件，不机械套用旧数值。所有读取保持预算；truncated 或 detail_required 时按条目展开，不读整本历史。

## 阶段结束时整理记忆

有新有效经验、完成外部数据导入或用户要求整理时，由当前宿主助手执行 Dream，不额外启动模型服务。

1. `fx_dream_memory(mode="preview", software=..., module=..., cursor=...)` 分页读取全局候选。project_id 不限制经验范围；next_cursor 翻页，最后一页的游标可作为下次增量起点。用 fx_get_dream_runs 查询已提交进度。
2. 用 fx_get_memory 展开相关完整来源、适用条件与案例；摘要不够时不要合并。判断同义、互补、不同方法、工程特例和矛盾。has_conflict 时展开 conflict_memory_ids，未解决前不能当作无争议方案套用。
3. 提交 actions：merge 合并同义、refine 补充方法、split 用多个动作从同一来源分别提炼、conflict 记录争议、supersede 提炼替代结论。每个动作填写 source_ids、完整 title/method/reason/applies_when、rationale，可明确修正 modules。候选不能借整理升级为已验证；原始来源和文本不删除。
4. apply 携带当前 snapshot。使用精确去重时省略 actions 并重复预览的 cursor/limit/software/module；语义整理则显式提交 actions。快照变化重读，不强行覆盖。每轮最多20个动作，可分批继续。精确建议 deferred_actions>0 时先用同一页参数继续预览和应用，处理完再推进游标。
5. 检查 module_guides；pending_rebuild 时用 fx_refresh_module_guides 修复。fx_undo_dream(run_id) 可撤销未被后续相关修改影响的整理；reset 仅重置精确索引，不撤销语义总结。

## 导入另一设备的数据

1. 使用 fx_import_data(source=...) 预览，停止源归档写入后 apply=true。相同记录幂等跳过，独立同名尝试或分叉修订不自动接续。
2. fx_get_import_records 展开冲突完整双方内容。冲突经验暂停默认召回；用当前 Dream snapshot 和判断理由调用 fx_resolve_import_memory，选择 keep_local/keep_incoming/keep_both。选 incoming/both 会保存独立来源分支，保留旧文本。案例或反馈来源有冲突时先处理证据，工具不会强行通过。
3. 用户规范分歧通过原有版本化规则接口保存决定及原话；工程检查点须核对本机路径再保存，不能用 Dream 混合进度。不能解决的矛盾保留并明确报告。
4. 对新增和已解决的经验执行全局 Dream；原始快照与手写材料保存在 data/imports 下，不直接拼进模块准则。

Houdini 检查点、经验和纠错使用 software=houdini（兼容默认值）；Nuke 使用 software=nuke。发现上游渲染问题时保存版本化问题单，Houdini 核查后由 Nuke 复查关闭，不自动重渲染。
