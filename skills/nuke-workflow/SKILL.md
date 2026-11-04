---
name: nuke-workflow
description: 由当前宿主助手通过 FX Agent 操作 Nuke 15.2 合成，参考效果或工程，保存纠错与成功方案，并将渲染素材问题反馈 Houdini 检查。
---

# Nuke 合成与学习

当前宿主助手负责规划、制作和判断；MCP 是操作工具，不另起一个独立决策模型。使用中文。

## 任务门禁顺序

先理解目标画面、动态、素材/参考、验收和预算。只读获取fx_status、真实NK和端口、Nuke模块目录后，用fx_begin_task登记kind=production、software=nuke、project_id/shot_id、scene_path、endpoint、goal、acceptance、budget、modules及解析后的input_paths。任务记录与Houdini入口共用data_root。

读取FX Agent的规范时带project_id、fx_task_id并完整翻页/展开；以software=nuke读取模块知识、fx_search_effects、fx_search_memory，选中后展开完整来源，所有这些读取携带fx_task_id。不得跳过效果卡/模板检索直接搭建。用fx_set_task_plan保存方法、适用条件、与当前需求的差异及真实selected_sources；无适用案例写明no_match_reason。

模块补充truncated时，改为按module检索并完整展开所选经验/效果卡，在selected_sources引用；该模块实际经验检索为零时可写no_match_reason，不强制整本MD进入上下文。摘要不冒充全文，争议与疑问仍需查证。

Nuke节点修改、批量操作、保存与渲染均携带fx_task_id，复用任务放行状态。输入、目标NK、帧段、FPS、格式或色彩管理变化时，fx_update_task后重新核对并保存方案。只读调查不需放行。

完成前实际save_script（根据既有授权填写confirm；仅确认提示不算保存），带fx_task_id执行find_broken_reads并实际查看结果，归档当前NK和preview，最后在Nuke入口调用fx_finish_task，写真实verification_note。用户确认后accepted反馈和fx_finalize_learning通过FX Agent共享入口携带同一fx_task_id及对应归档；门禁不把文件存在等同视觉正确。

遇到查证后仍不明确的问题，用fx_task_question记录并集中向用户提问；回答须保存真实user_quote和source_ref。开放问题不能被省略后宣称完成。重启或续作先核对现场并更新任务，不直接复用旧验证。

1. 先调用 FX Agent 的 `fx_status` 确认共同数据目录，用 Nuke 入口 `get_nuke_connection_status`、`get_script_info` 核对版本、当前 NK、帧范围和色彩管理。没有 Nuke 连接时停止实际节点操作，不把离线模拟结果当作现场。
   多窗口先用 `list_nuke_sessions` 获取 PID、实际端口、session_id 和工程路径，再将目标记录传给 `connect_nuke(port, pid, session_id, expected_script)`；切换后重新核对真实工程与任务目标。登记是快照，身份/路径变化需重读，不能自动猜选其他窗口。基准端口默认8187，各实例使用独立端口；多个窗口时MCP可在未选择状态启动，列表与连接操作不需要制作任务放行。
2. 用 `fx_get_production_rules` 读取共同用户规范。其 `$HIP` 和 File Cache 固定规则适用 Houdini，不能直接写进 Nuke 文件旋钮。遇到软件专属新规范，保存时使用带 `nuke-` 前缀的规则键并在 statement 写清适用软件。
3. 参考工程只读检查，实际制作使用工作副本。先查 `get_node_info`、`find_broken_reads`，核实真实通道、帧段、Alpha/预乘与素材色彩空间；不足的信息不能凭名称猜测。
4. 用 `fx_get_module_catalog(software="nuke")` 确认合成、抠像、色彩等模块标签；用 `fx_get_module_knowledge(..., software="nuke", module="compositing")` 读取全局模块 MD。额外经验按需 `fx_search_memory(..., software="nuke", module=...)`，纠错轨迹也按模块过滤，不重复加载同一条全文。Houdini 的经验不默认召回。
5. 用节点、参数与批量工具完成合成；不自动排版。保存、替换工程、删除和渲染工具的 confirm 只能根据已有用户授权如实填写，不能视为追加授权。已有授权可覆盖同一任务的必要步骤。当前适配不提供任意 Python 工具。
6. 阶段进度用 `fx_save_checkpoint(..., software="nuke")`，读取也明确软件。检查点不代替 `save_script` 保存真实 NK。
7. 纠错用 `fx_record_feedback`，feedback 的 software 填 nuke，modules 明确选合成/抠像/色彩等目录中的规范 ID，记录实际改变、原因、结果和用户原话。归档时向 `fx_archive_case` 提供已保存 NK、参考、预览与必要依赖；工具复制的是磁盘文件，不保存内存工程、不自动收集整段序列。
8. 用户确认最终效果且归档 NK 与预览后，再调用 `fx_finalize_learning` 提炼实际采用的方法；全局经验将更新对应模块 MD。检查 module_guides.state，pending_rebuild 时保留已存经验并用 `fx_refresh_module_guides` 重建。直接提交经验也明确 software 和 modules。候选、推断和最终确认不能混淆。

## 发现渲染素材有问题

先区分 Read 配置、代理、色彩空间/预乘使用问题与可能的源渲染问题。只有有证据需要上游核查时，使用 `fx_report_render_issue`，写明项目、镜头、素材路径、帧段、预期与实际表现、来源和检查证据。不要断言未经检查的 Houdini 根因。

由宿主助手切换到 Houdini 工作流，重新核对真实工程与渲染设置，再用 `fx_update_render_issue(actor="houdini")` 标记 investigating。检查记录与实际修复后标记 awaiting_nuke_review，并给出素材版本和证据。问题单不会自动调用 Houdini、修改节点或重渲染。

回到 Nuke 实际重读并检查新素材，成功后由 actor=nuke 标记 closed；仍有问题则改回 open。每次使用当前 expected_revision，冲突先重读。需要截图或完整证据文件长期留存时先归档并在 evidence 中记录案例与归档文件引用；问题单里的文本本身不是自动复制的文件。

平时两个软件独立制作与学习，共用数据存储。不要为每次合成主动调用 Houdini，也不要将整本历史注入上下文。
