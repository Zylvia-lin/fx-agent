# Render View 单帧核验接口

新增 `fx_render_view_start`、`fx_render_view_status`、`fx_render_view_save` 和 `fx_render_view_stop`。
接口使用原生 `hou.IPRViewer`，通过上游 bridge 在 Houdini 主线程执行，不改写 ROP 参数、不绕过节点与缓存工具。
默认关闭自动更新和 IPR 草稿预览，按选定 ROP 渲染当前单帧。运行中的渲染不会被后续 start 隐式替换。

先 start，记录 token；通过 status 确认渲染停止后再 save。save 会拒绝未完成渲染、过期 token、已改变的帧或 ROP，以及未经明确允许的同名文件覆盖。
保存 EXR 的同时统计 RGB 与 Alpha 非零像素及非有限数，不能只依据 RGB 有图或文件存在判断检查通过。
全黑的预期空帧应由镜头时序解释；非空效果帧必须另外核验 Alpha 与实际画面。

前端 MCP 进程需要重连后才会发现新工具，Houdini 无需重启。当前运行中的 MCP 工具列表不会因 reload_plugin 自动增加前端工具。
当前任务可由同一受限桥接实现直接调用这四个接口，不需要重启正在制作的 Houdini。
