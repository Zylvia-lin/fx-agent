# FX Agent 文档索引

本项目已全面采用 **纯文本通用经验（Git 跟踪 `data/`）+ 外部 FX Template 模板库直连** 架构，不再维护独立数据服务端、工程归档或重型效果卡。

## 核心文档

- [使用指南](使用指南.md)：纯文本经验沉淀规则、任务门禁流程、Houdini / Nuke / Maya 实战用法及外部模板库直连。
- [Windows 一键安装](一键安装.md)：通过 `Install-FX-Agent.bat` / PowerShell 安装 Houdini、Nuke 桥接与宿主配置。
- [Nuke 接入说明](Nuke接入与独立学习.md)：Nuke 15.2 多窗口会话、节点与 Viewer 工具、`.nk` 工具集导入及跨软件渲染问题反馈。
- [Maya 接入说明](maya.md)：Maya Python 2 / 3 共享 `.mod` 安装、Arnold / Redshift 灯光与单帧渲染。
- [Render View 单帧核验接口](RenderView单帧核验接口.md)：Houdini 原生 Render View 启动、轮询与像素核验接口。

## 合规与依赖

- [上游依赖基线](上游依赖.md)：固定版本、提交哈希与适配边界。
- [依赖许可证清单](依赖许可证清单.md)：全部锁定依赖包及其许可证。
- [第三方声明与致谢](../THIRD_PARTY_NOTICES.md) · [安全说明](../SECURITY.md) · [贡献指南](../CONTRIBUTING.md)
