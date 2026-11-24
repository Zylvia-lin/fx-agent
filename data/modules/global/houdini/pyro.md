# 烟雾、火焰、爆炸：全局工作流补充

上游准则：get_workflow_guide('pyro')

跨项目纯文本通用经验，按适用条件与边界复用；具体镜头尺度、材质和参数须结合当前现场核验。

自动区块内不要手改；手写补充放在区块外，其内容需另行核实。

<!-- FX MODULE LEARNING START -->
## 从已接受结果提炼的工作流补充

### 自建SOP HDA视口正常而ROP写出原始缓存：同时核对Display与Render旗标
适用条件：SOP HDA 或子网在视口中显示正确（如变速/后处理生效），但 File Cache / ROP 写盘读回却仍是未处理的原始输入。

不要只凭视口显示或无报错就判断写盘生效。先对比 live 节点与磁盘缓存的几何类型、体素大小和属性统计；重点查询 HDA 内部的 displayNode 与 renderNode，常见根因是 Display 指向最终 output 节点而 Render 仍留在上游输入节点。将最终输出节点同时设为 Display 与 Render 旗标，保存 HDA 定义和 HIP，再用短帧段实际写盘读回验证与 live 一致后再跑全段缓存。

原因与注意：Houdini ROP/File Cache 默认读取 Render 旗标而非 Display 旗标，旗标分离会导致看似参数/K帧失效。

来源：memory=21f4396142634688af5c5a809eb5c432

<!-- FX MODULE LEARNING END -->