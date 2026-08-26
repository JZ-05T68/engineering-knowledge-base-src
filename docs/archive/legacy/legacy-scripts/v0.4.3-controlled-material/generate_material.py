"""Phase A 受控测试材料生成：2 份具有典型工程文档结构的合成 PDF。

仅生成到 D:/ekb-isolated/v0.4.3-human-phase-a/test-material/，不导入 EKB、不进入 Git。
材料仅用于 pre-AI grounding workflow human test，不代表真实 AI engineering acceptance。
"""

from __future__ import annotations

from pathlib import Path

import fitz  # PyMuPDF

OUT = Path(__file__).resolve().parent

CJK = "china-s"  # PyMuPDF 内置简体中文字体，保证文本层可检索


def _title(page: fitz.Page, text: str, y: float = 72) -> None:
    page.insert_text((72, y), text, fontname=CJK, fontsize=18)


def _para(page: fitz.Page, lines: list[str], y0: float = 110, dy: float = 22) -> None:
    for index, line in enumerate(lines):
        page.insert_text((72, y0 + index * dy), line, fontname=CJK, fontsize=11)


def _figure(page: fitz.Page, caption: str) -> None:
    """画一个示意图区域（矩形+连线+标注），供图片区域证据测试使用。"""
    page.draw_rect(fitz.Rect(120, 420, 480, 560), color=(0, 0, 0), width=1.2)
    page.draw_rect(fitz.Rect(150, 450, 240, 510), color=(0, 0, 1), width=1)
    page.insert_text((165, 485), "电源模块", fontname=CJK, fontsize=10)
    page.draw_rect(fitz.Rect(330, 450, 450, 510), color=(1, 0, 0), width=1)
    page.insert_text((345, 485), "驱动输出级", fontname=CJK, fontsize=10)
    page.draw_line((240, 480), (330, 480), color=(0, 0, 0), width=1.5)
    page.insert_text((255, 470), "PWM", fontname=CJK, fontsize=9)
    page.insert_text((120, 590), caption, fontname=CJK, fontsize=10)


def build_datasheet() -> None:
    doc = fitz.open()
    page = doc.new_page()
    _title(page, "DZM-12 直流电机驱动模块 技术规格书（受控测试材料）")
    _para(page, [
        "文档编号：DZM-12-DS-001    版本：A0    发布日期：2026-01-15",
        "",
        "1. 产品概述",
        "DZM-12 是一款单通道直流有刷电机驱动模块，适用于 12V 供电的小型",
        "传动与执行机构。模块集成 H 桥功率级、电流采样与过温保护电路，",
        "通过 PWM 输入实现调速，方向由 DIR 引脚电平控制。",
        "",
        "2. 应用领域",
        "小型云台、电动夹爪、教学实验平台、轻载输送机构。",
    ])
    page = doc.new_page()
    _title(page, "3. 电气参数")
    _para(page, [
        "参数                 最小值    典型值    最大值    单位",
        "供电电压 VCC          10.8      12.0      13.2      V",
        "持续输出电流          —         2.0       3.0       A",
        "峰值输出电流(10ms)    —         —         5.0       A",
        "PWM 输入频率          1         20        50        kHz",
        "PWM 高电平阈值        2.6       —         5.5       V",
        "静态电流              —         8         15        mA",
        "过温保护阈值          —         105       —         ℃",
        "过温恢复迟滞          —         15        —         ℃",
        "",
        "注：持续电流 3A 需加装 25x25mm 散热片，环境温度不超过 40℃。",
    ])
    page = doc.new_page()
    _title(page, "4. 典型应用电路")
    _para(page, [
        "VCC 经 100uF 电解电容与 100nF 陶瓷电容并联滤波后接入模块。",
        "PWM 信号由控制器 GPIO 直接驱动，建议串联 1kΩ 电阻抑制振铃。",
        "电机两端建议并联 100nF 吸收电容以抑制换向火花干扰。",
    ])
    _figure(page, "图 4-1  DZM-12 典型应用连接示意图")
    page = doc.new_page()
    _title(page, "5. 保护特性")
    _para(page, [
        "5.1 过流保护：输出电流超过 5A 且持续 10ms 时关断输出，",
        "    故障解除后需重新拉低 PWM 至少 1ms 方可恢复。",
        "5.2 过温保护：结温超过 105℃ 关断输出，回落至 90℃ 自动恢复。",
        "5.3 欠压锁定：VCC 低于 9.5V 时输出保持关断状态。",
        "",
        "6. 封装与引脚",
        "引脚 1 VCC；引脚 2 GND；引脚 3 PWM；引脚 4 DIR；",
        "引脚 5 M+；引脚 6 M-。模块尺寸 32mm x 24mm x 8mm。",
    ])
    doc.save(OUT / "DZM-12_datasheet_controlled.pdf")
    doc.close()


def build_appnote() -> None:
    doc = fitz.open()
    page = doc.new_page()
    _title(page, "DZM-12 应用笔记：接线与调试（受控测试材料）")
    _para(page, [
        "文档编号：DZM-12-AN-001    版本：A0    发布日期：2026-02-01",
        "",
        "1. 适用范围",
        "本应用笔记配合 DZM-12 技术规格书使用，说明模块的典型接线方法、",
        "上电顺序与现场调试步骤，供集成与维护人员参考。",
    ])
    page = doc.new_page()
    _title(page, "2. 接线与上电顺序")
    _para(page, [
        "2.1 先连接 GND，再连接 VCC，最后接入 PWM/DIR 控制信号。",
        "2.2 上电顺序：控制电源 → 逻辑信号 → 主功率电源。",
        "2.3 断电顺序相反，避免 M+/M- 悬空时产生反灌电压。",
        "",
        "3. 调试步骤",
        "3.1 空载上电，确认静态电流约 8mA。",
        "3.2 输出 20kHz、占空比 20% PWM，电机应低速平稳启动。",
        "3.3 逐步提高占空比至 90%，观察电流不超过规格书限值 3A。",
        "3.4 切换 DIR 电平验证换向，换向前应先将占空比降至 0%。",
    ])
    page = doc.new_page()
    _title(page, "4. 常见问题与排查")
    _para(page, [
        "4.1 电机不转：检查 VCC 是否在 10.8V~13.2V；确认 PWM 高电平 ≥2.6V。",
        "4.2 运行中突然停机后自动恢复：多为过温保护动作，检查散热片安装。",
        "4.3 停机后需重新给 PWM 才恢复：为过流保护动作，排查堵转或短路。",
        "4.4 换向瞬间电流冲击大：确认换向前占空比已降至 0%。",
    ])
    _figure(page, "图 4-1  调试连接示意（示波器探头位置）")
    doc.save(OUT / "DZM-12_appnote_controlled.pdf")
    doc.close()


if __name__ == "__main__":
    build_datasheet()
    build_appnote()
    for pdf in sorted(OUT.glob("*.pdf")):
        print(pdf.name, pdf.stat().st_size, "bytes")
