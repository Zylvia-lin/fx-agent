# Maya selective adaptation

本模块不是两套上游服务的拼装，不依赖它们在 Maya 内运行。Python 2.7/3 桥接由本项目适配；Redshift 适配新增于本项目，运行时核对属性。

- [AutoMaya](https://github.com/CallMeParams/automaya-mcp/tree/5363aa3e4b506183ab79cc985c5da952b7eb1525)，commit `5363aa3e4b506183ab79cc985c5da952b7eb1525`，MIT，Copyright (c) 2026 Adam Waters。参考并选择性改写 `protocol.py` / `connection.py` 的长度前缀 JSON 与请求 ID、Arnold/light handlers 的节点映射、曝光差异、HDRI 连接及单帧序列渲染。许可全文见 [AutoMaya-MIT.txt](licenses/AutoMaya-MIT.txt)。host/bridge protocol.py 保持字节一致。
- [chadrik Maya MCP](https://github.com/chadrik/maya-mcp-server/tree/5eaa53752fd0ac7f69403843a237c13336222d8e)，commit `5eaa53752fd0ac7f69403843a237c13336222d8e`，MIT，Copyright (c) 2026 Chad Dombrova。参考 session_manager.py / client.py / maya_mcp_helper.py / utils.py 的多会话管理、身份信息和执行结果/stdout/stderr 思路，重新实现为独立 FX 协议与逐请求身份校验。未引入其 commandPort/Qt 服务或现代 Python 代码。许可全文见 [chadrik-MIT.txt](licenses/chadrik-MIT.txt)。

本机 Redshift MEL 模板仅用于识别已安装版本的节点与属性线索，没有复制或分发其脚本。两套上游均不被宣称原生兼容 Python 2 Maya。
