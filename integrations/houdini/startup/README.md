# Houdini 多会话端口修复

`startup.py` 基于固定依赖 fxhoudinimcp 2.25.0 的同名模块，保留其 MIT LICENSE 和 NOTICE。
改动只涉及启动端口选择：同时检查健康响应与真实 socket 绑定能力；跳过 Nuke 等
非 Houdini 服务；绑定竞争失败时在基础端口起最多 16 个端口内继续尝试。
保留当前进程重入、localhost 默认绑定、前台/后台启动和异步就绪检查。

安装器在 `--apply` 时将依赖插件复制到工程 `.runtime/houdini/<摘要>/`，仅替换本模块。
预览不写文件；已发布运行目录不覆盖；不修改 `.venv/site-packages`。
升级上游版本须重新核对本适配模块并运行测试。

客户端仍按原有 `HOUDINI_PORT` 固定目标，不会静默切换到其他工程。
新实例启动成功后，从日志 `Server ready on port ...` 获取实际端口，使用
`connect_houdini(port=...)` 显式切换。无需更改 Nuke 的监听端口。

更新部署：`git pull --ff-only` 后，使用工程虚拟环境运行
`scripts/install_codex.py --houdini-pref <Houdini偏好目录> --apply`；每个已安装的
Houdini 版本分别执行。端口、自定义环境和现有生产目录按安装器原规则保留。
重启需要修复的新会话后生效；不要关闭或替换尚未保存的镜头工程。
