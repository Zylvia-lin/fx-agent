# 材质着色：全局工作流补充

上游准则：get_workflow_guide('shade')

跨项目纯文本通用经验，按适用条件与边界复用；具体镜头尺度、材质和参数须结合当前现场核验。

自动区块内不要手改；手写补充放在区块外，其内容需另行核实。

<!-- FX MODULE LEARNING START -->
## 从已接受结果提炼的工作流补充

### Karma XPU粒子自发光使用MaterialX和USD遮挡设置
适用条件：Solaris/USD 流程中使用 Karma XPU 渲染带颜色和速度属性的粒子自发光。

材质搭建：使用 mtlxgeomcolor（color3）读取点颜色 primvar 接入 mtlxstandard_surface 的 emission_color，将 base 与 specular 设为 0、按需调节 emission 强度，通过 Material Library 绑定目标粒子 USD Prim。遮挡与模糊：使用 Render Geometry Settings 为场景/角色遮挡对象写入 Matte，为粒子写入 Velocity Blur / Instance Velocity Blur。渲染前务必核对颜色与速度 primvars 是否正确导入 USD、相机路径是否指向当前镜头、以及渲染输出路径是否有效。

原因与注意：Karma XPU 不兼容旧 Mantra VEX 边缘光/发光着色器，须采用 MaterialX 与 USD 原生渲染几何属性。

来源：memory=04e6510bc4704a748644f146b6bd4ded

### Mantra边缘光由法线视角衰减和属性组合形成
适用条件：使用 Mantra/VEX 材质制作带法线及 Cd/Alpha 属性的几何边缘发光；不可直接套用到 Karma XPU。

核心思路：用视角法线衰减（normalfalloff，设置 invert=on 与合适指数如 exp=4）形成边缘遮罩，与 Alpha 及可选的 Cd、glowColor、emission 相乘输出自发光。独立渲染对象绑定边缘光材质并单独出层（如 raytrace ROP 按需开关运动模糊）。注意区分轮廓视角遮罩、材质自发光、几何灯和后期 Glow，不要用盲目打灯代替角度遮罩；Gallery Glow 发射模式可能产生 RGB 有值而 Alpha 为 0，合成端须核对加色（Plus/Screen）而非依赖 Alpha 预乘；普通 Glow（invert=off）不可与边缘反转模式混用。

原因与注意：明确区分边缘光材质原理与后期发光，避免引擎不兼容或 Alpha 通道丢失导致合成异常。

来源：memory=9e5dde186e3f49beb5a8dfd6bea978a6

### 不同首帧贴图序列映射至统一时间线起点的表达式写法
适用条件：多个材质或节点读取不同起始帧号的连续四位帧序列（原速播放、范围外保持首末帧），需统一对齐到指定目标起始帧；此方法不控制显隐。

先读取实际磁盘目录确认每个序列的真实首帧 source_start 与末帧 source_end，定位材质暴露的贴图文件参数；在 $HIP 相对路径中将 $F4 替换为 `padzero(4, clamp($F - target + source_start, source_start, source_end))`，保留原有通道引用。修改后逐帧 evalAtFrame 并验证目标帧段内所有解析路径在磁盘上真实存在。

原因与注意：直接使用 $F4 会按全局时间线帧号读取，无法适配起始帧不统一的多组序列贴图。

来源：memory=e94151d0d76942cdb8e7b7f9b822a5fe

### 程序化力场光丝效果：管面双尺度各向异性噪声与独立Alpha分层控制
适用条件：制作能量力场、气浪光丝、条带光幕等需要高可控性且无需流体/粒子解算器的程序化光效。

核心骨架：高细分管面/曲面 → Mountain 做小幅轮廓扰动 → 细分/多副本形成空间层次。纹理与动态：用各向异性拉伸的高低频两套噪声（如 onoise × snoise）相乘写入 Cd，制造细长断续与疏密变化；通过对噪声采样坐标 offset 沿轴向做时间偏移驱动流动。透明度与分层：用相对包围盒配合 Ramp 独立计算 Alpha 轮廓（与明暗纹理分离，便于收束边缘）；按职责拆出细密主光丝、粗亮束、暗部/柔化层分别出层，并把角色/场景加入 matte_objects 提供遮挡。具体尺寸、频率与强度按当前镜头尺度重新调节，不照搬单镜头绝对数值。

原因与注意：纯 SOP + 属性着色方案无解算开销、迭代极快，且纹理、流动与透明轮廓完全解耦。

来源：memory=71d43fd7fd0447e08e88c64ce146f466

<!-- FX MODULE LEARNING END -->