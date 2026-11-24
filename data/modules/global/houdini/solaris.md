# USD / Solaris：全局工作流补充

上游准则：get_workflow_guide('solaris')

跨项目共享，按条目中的条件复用。归档在提炼时校验，复用前重新核验案例；不代表因果解释全部证实。

自动区块内不要手改；手写补充放在区块外，其内容需另行核实。

<!-- FX MODULE LEARNING START -->
## 从已接受结果提炼的工作流补充

### Karma XPU粒子自发光使用MaterialX和USD遮挡设置
适用条件：Solaris/USD 流程中使用 Karma XPU 渲染带颜色和速度属性的粒子自发光。

材质搭建：使用 mtlxgeomcolor（color3）读取点颜色 primvar 接入 mtlxstandard_surface 的 emission_color，将 base 与 specular 设为 0、按需调节 emission 强度，通过 Material Library 绑定目标粒子 USD Prim。遮挡与模糊：使用 Render Geometry Settings 为场景/角色遮挡对象写入 Matte，为粒子写入 Velocity Blur / Instance Velocity Blur。渲染前务必核对颜色与速度 primvars 是否正确导入 USD、相机路径是否指向当前镜头、以及渲染输出路径是否有效。

原因与注意：Karma XPU 不兼容旧 Mantra VEX 边缘光/发光着色器，须采用 MaterialX 与 USD 原生渲染几何属性。

来源：memory=04e6510bc4704a748644f146b6bd4ded

### 复用模板或旧工程残留Karma网络时先核对镜头相机与有效输出
适用条件：打开历史工程或复用模板网络，发现 /stage 下存在多个 Karma/Solaris 渲染分支时。

不要默认场景中存在的 /stage Karma 网络就是当前有效渲染通路。优先检查输出 ROP 的实际引用对象、绑定相机路径（常残留其他旧镜头相机名）和磁盘输出路径，确认哪些分支真正启用、哪些属于历史模板残留，再决定更新相机还是新建独立渲染链。

原因与注意：复用工程常遗留旧镜头相机或未启用的 Solaris 分支，未核对直接渲染会导致视角错乱或输出错层。

来源：memory=144dd17dbe974aabb52c0fbcf230dbf8

<!-- FX MODULE LEARNING END -->