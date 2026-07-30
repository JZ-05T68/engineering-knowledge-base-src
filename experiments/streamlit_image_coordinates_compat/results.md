# streamlit-image-coordinates 0.4.0 兼容性实验结果

实验日期：2026-07-30。实验分支：`kimi/v0.3.0-image-coordinates-compat`（基线 `2520e85`）。
环境：独立 venv（Python 3.11.9），wheelhouse 离线安装验证通过。

## 结论：PASS（含一项已确认绕过的组件缺陷，见「缺陷与限制」）

## 精确依赖版本

- streamlit 1.59.1、streamlit-image-coordinates 0.4.0、pillow 12.3.0
- 完整清单见实验时 `pip freeze` 记录（numpy 2.4.6、pandas 3.0.5、altair 6.2.2 等 43 项，均可由 wheelhouse 离线安装）

## 离线验证

- `pip install --no-index --find-links <wheelhouse>` 在全新空白 venv 安装成功。
- 组件前端为包内本地静态文件（`frontend/index.html + main.js + streamlit-component-lib.js`），无 CDN/远程字体/远程脚本引用（仅有的两个 URL 是注释与示例字符串）。
- 真实浏览器会话抓取全部 31–32 个网络请求：非本地请求数 = 0。
- 启动命令：`python -m streamlit run app.py --server.address 127.0.0.1 --server.port 8520 --server.headless true --browser.gatherUsageStats false`

## click_and_drag 实际返回值结构

```json
{"x1": 100, "y1": 100, "x2": 250, "y2": 200, "width": 300, "height": 450, "unix_time": 1785378615193}
```

- `x1,y1` = mousedown 相对图像的显示像素；`x2,y2` = mouseup 相对图像的显示像素。
- `width,height` = 图像实际显示尺寸（**换算必须以该返回值为准**，不要用请求宽度）。
- `x2,y2` 可能为负或超出图像（拖出边界时原样返回，如 y2=570 > height=450）。
- 组件**不做**角点排序（反向拖拽原样返回）；零面积点击也会返回一个值。

## 坐标换算与边界处理规则（coordinate_mapping.py，已冻结）

- 缩放比 = 原始尺寸 / 返回值中的显示尺寸；四舍五入（round-half-away）取整。
- 角点自动排序（任意拖拽方向）。
- 越界裁剪到原始图像边界（不拒绝、不放行越界值）。
- 零面积或换算后 < 1px 的矩形一律拒绝（ValueError，UI 显示拒绝原因）。

## 十二项门禁结果

| # | 门禁 | 结果 |
|---|---|---|
| 1 | Python 3.11 | 通过（3.11.9 安装/导入/运行） |
| 2 | Streamlit 1.59.1 | 通过（精确版本，未升降级） |
| 3 | Windows 本地运行 | 通过（Edge 141 headless CDP 实测） |
| 4 | 完全离线 | 通过（wheelhouse 安装 + 零非本地请求） |
| 5 | 多页面 | 通过（主页→实验页→主页→再进入，组件均正常） |
| 6 | tab 内运行 | 通过（切换 tab 往返后组件与返回值保持） |
| 7 | 本地 PNG | 通过（路径与 PIL Image 两种输入均可拖拽；**推荐本地文件路径**，少一次 PNG 重编码） |
| 8 | 缩放换算 | 通过（300/600/1000 三种宽度真实拖拽，同一逻辑矩形换算结果完全一致 (80,120)-(400,600)） |
| 9 | 旋转/横向 | 通过（1600x900 横向、1200x800 已旋转图，坐标以存储 PNG 像素空间为准，无需旋转补偿） |
| 10 | 异常拖拽 | 通过（越界返回值被裁剪；零面积被拒绝；反向被排序；缺字段/非数字在纯函数层拒绝） |
| 11 | rerun 行为 | 已实测记录（见下） |
| 12 | 页码/key 隔离 | 通过（见下） |

## rerun 行为（实测）

- 触发普通 rerun（点按钮）且 key 不变：**返回值保留**。
- 切换 tab 往返：**保留**。
- 切换到其他页（key 改变）再返回：**重置为 None**（组件值随 widget 卸载丢失）。
- 浏览器刷新（新 session）：**重置为 None**，session_state 历史计数清零。
- 设计含义：**组件返回值必须视为瞬时量**——服务端校验通过后应立即写入 session_state/数据库，正式 UI 不得依赖组件保留值。

## 页码与 key 隔离（实测）

- 进入第 2/3 页时 key 变化（`sic_region_doc999_pg2_v0_w600`），组件初始值为 None，**无串页**。
- 各页拖拽互不影响；坐标换算各自正确。
- **正式 key 规则**：`sic_{mode}_doc{document_id}_pg{page_id}_v{anchor_version}_w{display_width}`。
  `make_component_key()` 生成主体，显示宽度令牌在调用处追加（见实验页实现）。
  重新绑定时 bump `anchor_version` 可强制组件回到干净状态。

## 缺陷与限制（重要）

1. **width 参数缺陷**：图片不变、仅 `width` 参数变化时，组件前端不重新缩放（`resizeImage` 只挂在 `img.onload` 与 window resize 上）。
   **已验证绕过**：key 中加入显示宽度令牌，宽度变化即重建 iframe，实测 300/1000/600 均正确生效。正式阅读页有缩放滑块，**必须**使用该 key 规则。
2. 组件值是瞬时的（见 rerun 行为），且**拖拽结束即触发一次 rerun**——正式 UI 需在每次返回值非空时立即规范化+暂存，再让用户确认。
3. 拖拽出图像边界时返回值含越界坐标，必须服务端裁剪（已实现并测试）。
4. 组件无框选视觉反馈（无拖拽中的矩形高亮）——可接受，数值与结果在服务端回显；不因此更换组件。
5. headless CDP 无法覆盖真实人工手感，发布前人工测试门禁中保留一次真实拖拽确认。

## 自动化测试

- `test_coordinate_mapping.py`：19 个用例全部通过（换算、非整数缩放、反向归一、越界裁剪、零面积拒绝、尺寸有效性、矩形约束、key 生成/稳定性/隔离性）。
- 运行：`python -m unittest test_coordinate_mapping -v`

## 浏览器实测

- 全部拖拽实验在 Edge 141（headless, CDP Input 域真实鼠标事件）完成，非人工手测。
- 复现脚本在实验期间存放于仓库外临时目录，已随环境清理删除；如需复查可按 README 手工操作。

## 隔离确认

- 正式服务 127.0.0.1:8501（PID 4928，v0.2.4）全程运行、零接触。
- 正式数据目录零接触；测试图片均为 `generate_assets.py` 合成。
- 实验端口 8520 已停止，无遗留进程。
