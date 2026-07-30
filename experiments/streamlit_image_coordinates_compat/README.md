# streamlit-image-coordinates 0.4.0 兼容性实验

v0.3.0 实现前置门禁实验。与正式工程知识库数据完全隔离。

## 环境

- Python 3.11（独立 venv，不进入项目 requirements）
- streamlit==1.59.1
- streamlit-image-coordinates==0.4.0
- pillow（仅用于生成/读取测试图片）

## 重新生成测试图片

```bash
python generate_assets.py
```

## 运行自动测试

```bash
python -m unittest test_coordinate_mapping -v
```

## 启动实验应用

```bash
streamlit run app.py --server.address 127.0.0.1 --server.port 8520 --server.headless true
```

浏览器打开 http://127.0.0.1:8520 ，进入「框选实验」页，按页内清单手工验证。

## 目录

- `app.py` — 多页面入口
- `pages/1_框选实验.py` — 实验页（拖拽、宽度、tab、key 隔离、rerun 观察）
- `coordinate_mapping.py` — 显示坐标→原始 PNG 坐标纯函数 + 组件 key 规则
- `test_coordinate_mapping.py` — 纯函数自动测试（stdlib unittest）
- `generate_assets.py` — 合成测试图片生成（网格+刻度+标记矩形）
- `assets/` — portrait 800x1200 / landscape 1600x900 / rotated 1200x800
- `results.md` — 实验结果
