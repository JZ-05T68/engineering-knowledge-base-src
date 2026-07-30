"""Compat experiment entry app (multipage). Run:

    streamlit run app.py --server.address 127.0.0.1 --server.port 8520
"""

import streamlit as st

st.set_page_config(page_title="SIC 兼容性实验", layout="wide")
st.title("streamlit-image-coordinates 0.4.0 兼容性实验")
st.markdown(
    """
- 左侧进入 **框选实验** 页面进行拖拽验证。
- 本实验与正式工程知识库数据完全隔离，仅使用合成测试图片。
- 端口固定 127.0.0.1:8520。
"""
)
st.page_link("pages/1_框选实验.py", label="进入框选实验页")
