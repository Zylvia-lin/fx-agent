# Nuke 合成真实感与审美检查表

合成显假的 80% 问题藏在默认曝光下不易察觉的暗部、边缘与光学细节里。审片前先用 `set_nuke_viewer_state` + `capture_nuke_viewer` 执行标准诊断，再逐项排雷。

## 一、标准 Viewer 诊断套组（必做）

1. **默认曝光主视图（`gain=1.0, gamma=1.0, channels="rgb"`）**：看整体构图、光影融合与视觉重心。
2. **暗部诊断图（`gain` 上拉至 `4.0~8.0` 或 `gamma` 上拉至 `1.8~2.2`）**：暴露前景 CG 与实拍背景的**黑场高度、暗部色偏（偏绿/偏洋红/死黑）**与隐藏噪点。
3. **高光诊断图（`gain` 下压至 `0.15~0.25`）**：检查高光是否硬裁切（变成一片死白/色块）、能量核心是否有亮度梯度。
4. **单通道检查（`channels="alpha"` 及必要时 `red`/`green`/`blue`）**：检查 Alpha 是否过硬/发虚/带杂点，三通道噪点颗粒是否与实拍底板一致。
5. 诊断完毕后将 Viewer 复位回默认状态（`gain=1.0, gamma=1.0, channels="rgb"`）。

## 二、10 项“防显假”工业检查表

| 序号 | 检查项 | 典型穿帮表现（显假信号） | 常见 Nuke 修复节点与方向 |
|---|---|---|---|
| 1 | **黑场与暗部色偏（Black Level & Tint）** | CG 暗部比背景最黑处更黑（发死）或浮灰；暗部偏色与环境光相反 | 在 `Premult` 前对 CG 用 `Grade` 调 `lift` / `blackpoint`，或 `Toe` 轻抬暗部对齐实拍阴影 RGB |
| 2 | **高光动态范围与滚降（Highlight Roll-off）** | 发光特效或高光处直接死白断层，边缘锯齿发硬 | 检查是否过早 `Clamp`；用 `SoftClip` / 多层指数衰减 `Glow`（宽半径低强度 + 窄半径高强度）保留核芯层次 |
| 3 | **Premult / Unpremult 边界** | 调色后主体外圈出现黑边、亮边或半透明脏边 | 所有 `Grade`/`ColorCorrect` 必须放在 `Unpremult` → 调色 → `Premult` 之间，或启用节点的 `unpremult` 开关 |
| 4 | **抠像溢色（Despill）** | 边缘残留绿/蓝幕反射光，或去溢色过度导致边缘发灰发暗 | 调整 `Keylight`/`HueCorrect` 去溢色，并用背景色微补边缘亮度（Spill Replace） |
| 5 | **边缘锐度与融合（Edge Integration）** | CG 边缘像贴纸一样比实拍主体更锐利 | 核对实拍镜头清晰度，在局部加细微 `EdgeBlur` / `Blur`（0.3–1.2px）匹配底板锐度 |
| 6 | **光线包裹（Light Wrap）** | 强背光下主体边缘死硬不透光，或 Light Wrap 过猛导致主体发灰失去轮廓 | 仅对背景高光区提取亮度做克制的 `LightWrap`（低 mix + 柔和扩散），保持主体暗部剪影清晰 |
| 7 | **光学一致性（Defocus / Motion Blur / Lens）** | CG 永远全清晰、无动态模糊，或与实拍镜头畸变/色散/暗角脱节 | 按景深加 `ZDefocus`/`Defocus`、补 `VectorBlur`/`MotionBlur`，并匹配实拍的轻微色散（Chromatic Aberration）与畸变 |
| 8 | **颗粒匹配（Grain / Noise）** | 实拍底板有胶片/CMOS 噪点，唯独 CG 表面异常光滑干净 | 在最终合成链末段仅对 CG Alpha 区域叠加 `Grain2` / 采样 `ScannedGrain`，并在 R/G/B 单通道对比颗粒大小与强度 |
| 9 | **前后景空气透视（Atmospheric Depth）** | 远处烟雾/CG 对比度与近景一样高，缺乏空间纵深 | 利用 Z-depth 轻微压低远景对比度并混入环境雾色（抬升远景黑场、降低饱和度） |
| 10 | **视觉焦点与色彩和谐（Hierarchy & Palette）** | 全屏高饱和特效喧宾夺主，把主体淹没 | 收敛次要区域饱和度与亮度，确保最高对比度与冷暖冲突集中在叙事焦点 |
