# 渲染：全局工作流补充

上游准则：get_workflow_guide('render')

跨项目共享，按条目中的条件复用。归档在提炼时校验，复用前重新核验案例；不代表因果解释全部证实。

自动区块内不要手改；手写补充放在区块外，其内容需另行核实。

<!-- FX MODULE LEARNING START -->
## 从已接受结果提炼的工作流补充

### Karma XPU粒子自发光使用MaterialX和USD遮挡设置
适用条件：Solaris/USD 流程中使用 Karma XPU 渲染带颜色和速度属性的粒子自发光。

材质搭建：使用 mtlxgeomcolor（color3）读取点颜色 primvar 接入 mtlxstandard_surface 的 emission_color，将 base 与 specular 设为 0、按需调节 emission 强度，通过 Material Library 绑定目标粒子 USD Prim。遮挡与模糊：使用 Render Geometry Settings 为场景/角色遮挡对象写入 Matte，为粒子写入 Velocity Blur / Instance Velocity Blur。渲染前务必核对颜色与速度 primvars 是否正确导入 USD、相机路径是否指向当前镜头、以及渲染输出路径是否有效。

原因与注意：Karma XPU 不兼容旧 Mantra VEX 边缘光/发光着色器，须采用 MaterialX 与 USD 原生渲染几何属性。

来源：memory=04e6510bc4704a748644f146b6bd4ded

### Mantra分层粒子与能量渲染按层匹配引擎采样与运动模糊
适用条件：Mantra 多层特效（轮廓光、细粒、高速尾迹等）分层渲染优化画质与速度。

不要对所有粒子/能量层套用单一全局渲染预设。按层的几何形态与运动特征分别配置：几何边缘光/网格层可用 raytrace 较低采样且按需关闭模糊；高密度细粒或需要拉丝动态的层使用 micropolygon 配合较高采样及运动模糊。逐层核对 forceobject、matte_objects、对象级 geo_velocityblur 与 ROP 级 allowmotionblur 的组合，先用少量关键帧识别噪点来源再逐步拉高采样。

原因与注意：统一高采样会显著拖慢渲染，而模糊开关错配会导致拖尾缺失或多余涂抹。

来源：memory=c0af2c4f21684f8b9763b31b94dbb2c8

### Mantra边缘光由法线视角衰减和属性组合形成
适用条件：使用 Mantra/VEX 材质制作带法线及 Cd/Alpha 属性的几何边缘发光；不可直接套用到 Karma XPU。

核心思路：用视角法线衰减（normalfalloff，设置 invert=on 与合适指数如 exp=4）形成边缘遮罩，与 Alpha 及可选的 Cd、glowColor、emission 相乘输出自发光。独立渲染对象绑定边缘光材质并单独出层（如 raytrace ROP 按需开关运动模糊）。注意区分轮廓视角遮罩、材质自发光、几何灯和后期 Glow，不要用盲目打灯代替角度遮罩；Gallery Glow 发射模式可能产生 RGB 有值而 Alpha 为 0，合成端须核对加色（Plus/Screen）而非依赖 Alpha 预乘；普通 Glow（invert=off）不可与边缘反转模式混用。

原因与注意：明确区分边缘光材质原理与后期发光，避免引擎不兼容或 Alpha 通道丢失导致合成异常。

来源：memory=9e5dde186e3f49beb5a8dfd6bea978a6

### 复用模板或旧工程残留Karma网络时先核对镜头相机与有效输出
适用条件：打开历史工程或复用模板网络，发现 /stage 下存在多个 Karma/Solaris 渲染分支时。

不要默认场景中存在的 /stage Karma 网络就是当前有效渲染通路。优先检查输出 ROP 的实际引用对象、绑定相机路径（常残留其他旧镜头相机名）和磁盘输出路径，确认哪些分支真正启用、哪些属于历史模板残留，再决定更新相机还是新建独立渲染链。

原因与注意：复用工程常遗留旧镜头相机或未启用的 Solaris 分支，未核对直接渲染会导致视角错乱或输出错层。

来源：memory=144dd17dbe974aabb52c0fbcf230dbf8

### 程序化力场光丝效果：管面双尺度各向异性噪声与独立Alpha分层控制
适用条件：制作能量力场、气浪光丝、条带光幕等需要高可控性且无需流体/粒子解算器的程序化光效。

核心骨架：高细分管面/曲面 → Mountain 做小幅轮廓扰动 → 细分/多副本形成空间层次。纹理与动态：用各向异性拉伸的高低频两套噪声（如 onoise × snoise）相乘写入 Cd，制造细长断续与疏密变化；通过对噪声采样坐标 offset 沿轴向做时间偏移驱动流动。透明度与分层：用相对包围盒配合 Ramp 独立计算 Alpha 轮廓（与明暗纹理分离，便于收束边缘）；按职责拆出细密主光丝、粗亮束、暗部/柔化层分别出层，并把角色/场景加入 matte_objects 提供遮挡。具体尺寸、频率与强度按当前镜头尺度重新调节，不照搬单镜头绝对数值。

原因与注意：纯 SOP + 属性着色方案无解算开销、迭代极快，且纹理、流动与透明轮廓完全解耦。

来源：memory=71d43fd7fd0447e08e88c64ce146f466

<!-- FX MODULE LEARNING END -->