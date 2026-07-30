"""框选实验页：验证 streamlit-image-coordinates 0.4.0 的 click_and_drag 行为。

覆盖门禁：多页面、tab 内运行、本地 PNG 两种输入、三种显示宽度、
异常拖拽处理、rerun 行为观察、页码/key 隔离。
"""

from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st
from PIL import Image
from streamlit_image_coordinates import streamlit_image_coordinates

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from coordinate_mapping import display_to_original, make_component_key  # noqa: E402

ASSETS = Path(__file__).resolve().parent.parent / "assets"

PAGES = {  # 模拟三个不同页面
    "第1页 纵向 800x1200": ("portrait_test.png", 800, 1200),
    "第2页 横向 1600x900": ("landscape_test.png", 1600, 900),
    "第3页 旋转 1200x800": ("rotated_test.png", 1200, 800),
}
WIDTHS = (300, 600, 1000)  # 实验覆盖的三种显示宽度
DOC_ID = 999  # 模拟文档 id，与正式数据无关

st.set_page_config(page_title="框选实验", layout="wide")
st.title("图片区域框选兼容性实验")

# 用按钮切换模拟页面/显示宽度（同 session rerun，便于真实浏览器驱动验证）
if "sim_page" not in st.session_state:
    st.session_state.sim_page = 1
if "sim_width" not in st.session_state:
    st.session_state.sim_width = 600

pc1, pc2, pc3 = st.columns(3)
for i, col in enumerate((pc1, pc2, pc3), start=1):
    if col.button(f"切换到第{i}页", key=f"btn_page_{i}"):
        st.session_state.sim_page = i
        st.rerun()
wc1, wc2, wc3 = st.columns(3)
for col, w in zip((wc1, wc2, wc3), (300, 600, 1000), strict=True):
    if col.button(f"宽度 {w}", key=f"btn_width_{w}"):
        st.session_state.sim_width = w
        st.rerun()

page_id = st.session_state.sim_page
page_label = list(PAGES)[page_id - 1]
filename, orig_w, orig_h = PAGES[page_label]
disp_w = st.session_state.sim_width
input_mode = st.radio("组件输入方式", ["本地文件路径", "Pillow Image 对象"], horizontal=True)

image_path = ASSETS / filename
st.caption(f"当前图片: {filename}，原始尺寸 {orig_w}x{orig_h}，显示宽度 {disp_w}px")

tab_drag, tab_note = st.tabs(["框选实验", "对照说明"])

with tab_drag:
    # key 含显示宽度令牌：组件缺陷 —— width 参数变化但图片不变时前端不重新缩放，
    # 换 key 强制重建 iframe 才能使新宽度生效（详见 results.md）
    component_key = f"{make_component_key(DOC_ID, page_id, mode='region', anchor_version=0)}_w{disp_w}"
    st.code(f"component key = {component_key}")

    source = str(image_path) if input_mode == "本地文件路径" else Image.open(image_path)
    try:
        value = streamlit_image_coordinates(
            source,
            width=disp_w,
            key=component_key,
            click_and_drag=True,
            cursor="crosshair",
        )
    except TypeError as exc:
        st.error(f"组件调用失败（参数不兼容？）：{exc}")
        value = None
        st.stop()

    st.subheader("组件原始返回值")
    st.json(value if value is not None else "None")

    # rerun 行为观察：记录每次返回值变化
    history: list = st.session_state.setdefault("value_history", [])
    if value is not None and (not history or history[-1] != value):
        history.append(value)
    st.caption(f"本 session 已记录 {len(history)} 次非空返回值")

    if st.button("触发一次普通 rerun（不清状态）"):
        st.rerun()
    if st.button("清空返回值历史"):
        st.session_state["value_history"] = []
        st.rerun()

    if value is not None:
        st.subheader("服务端规范化与坐标换算")
        try:
            reported_w = int(value.get("width", disp_w))
            reported_h = int(value.get("height", int(disp_w * orig_h / orig_w)))
            rect = display_to_original(
                value.get("x1"), value.get("y1"),
                value.get("x2"), value.get("y2"),
                reported_w, reported_h, orig_w, orig_h,
            )
            st.success(f"原始 PNG 坐标: {rect}")
        except (ValueError, AttributeError) as exc:
            st.warning(f"拖拽已拒绝：{exc}")

    with st.expander("数值坐标兜底（调试对照）"):
        c1, c2 = st.columns(2)
        nx0 = c1.number_input("x0", 0, orig_w, 100)
        ny0 = c2.number_input("y0", 0, orig_h, 100)
        nx1 = c1.number_input("x1", 0, orig_w, 300)
        ny1 = c2.number_input("y1", 0, orig_h, 250)
        st.caption(f"兜底矩形: ({nx0},{ny0})-({nx1},{ny1})")

with tab_note:
    st.markdown(
        """
        ### 手工验证清单
        1. 在图片上**拖拽**矩形，观察返回值结构（应为 x1/y1/x2/y2/width/height）。
        2. 切换显示宽度（小/中/大），同一区域换算结果应一致。
        3. 反向拖拽（右下→左上）应自动排序。
        4. 零面积点击应被服务端拒绝。
        5. 拖出图片边界，观察返回值与裁剪行为。
        6. 切换模拟页面，旧页坐标不得串到新页。
        7. 点击 rerun 按钮/修改宽度，观察返回值是否保留。
        8. 切换 tab 再返回，组件应正常显示。
        """
    )
